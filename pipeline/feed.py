#!/usr/bin/env python3
"""Himawari Earth 配信 v3（GitHub Actions から10分ごとに実行）

ひまわり9号の標準データ(HSD, NOAA公開)から、Satpy のトゥルーカラー再現で高解像度一式を作る。
一式 = 全体 hi（4032px を重なり付き 2048px×4枚）＋真下まわり1km dt（4枚）＋日本まわり0.5km jp（4枚）。

  最新      : hi|dt|jp/HHMM_k.jpg（観測スロット名。直前のスロットの hi も残す）
  あさひ・まひる・ゆうひ:
              ひまわり直下（東経140.7°）で太陽の時角が -60°・0°・+60° になる観測（季節で毎日計算）。
              s/{名前}{世代}_{hi|dt|jp}_k.jpg。世代は観測日(JST)の通し番号 mod 4（URLを日ごとに変えて古いキャッシュを避ける）
              Actions のキャッシュ（--store）で回をまたいで保持し、足りない分は1回に1つだけ過去データから作る。

latest.txt（Udon が読む）:
  1行目 HIMAWARI3|最新の観測UTC|スロット|処理UTC|印（D=真下まわりあり, J=日本まわりあり, 無ければ -）
  2〜4行目 SET|名前|観測UTC|世代|印 （まだ無ければ SET|名前|-|-|-）

観測時刻は常に「観測開始の公称時刻」。夜側は観測のまま暗い。昼の画像で埋めない。
過去の観測（あさひ等）は、受け手側で必ず「最新ではない」と示す。

使い方: python feed.py --out site --store store --site-url https://USER.github.io/REPO/
依存: Pillow, satpy, pyspectral, pyorbital
"""
import argparse
import json
import math
import shutil
import subprocess
import email.utils
import glob
import os
import re
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

UA = "HimawariEarthVRC/3.0 (personal VRChat world feed)"

# ---- 画像の規格（シェーダーと共有。変えるときは HimawariObservedEarth.shader も変える） ----
# 正方形キャンバスに対する比率（左上原点）。
DISK_CX, DISK_CY, DISK_R = 0.5, 2010 / 4096, 1990 / 4096
LABEL_BOX = (0.0, 0.928, 0.27, 1.0)      # 左下の角（円盤の外）に観測時刻を焼き込む
HI = 4032           # 高解像度キャンバス（2016px×2 に分け、各タイルに隣の端を PAD 画素ずつ足して 2048px にする）
PAD = 16            # タイルの継ぎ目対策の重なり幅
# 真下まわりの詳細（1km格子）: 走査角で ±DETAIL_FRAC×(地球の縁) の正方形。高度400kmで見える範囲をおおう
DETAIL_FRAC = 0.40
DETAIL_BOX = (-DETAIL_FRAC, -DETAIL_FRAC, DETAIL_FRAC, DETAIL_FRAC)   # 地球の縁の走査角を1とした (x0, y0, x1, y1)
# 日本まわり（0.5km格子）: 走査角[rad]の長方形 (x0, y0, x1, y1)。与那国島〜宗谷岬・知床をおおう
JAPAN_BOX = (-0.050, 0.066, 0.012, 0.122)

# ---- NOAA公開のHSD(東経140.7°, 2km格子5500px) ----
S3 = "https://noaa-himawari9.s3.amazonaws.com"
HSD_BANDS = ("B01", "B02", "B03", "B04", "B13")
HSD_CX = HSD_CY = 2750.0
HSD_R = 2717.08                                  # 赤道の縁までの画素数（area_extent から計算）
HSD_PIX = 0.15185208 / HSD_R                     # 2km格子1画素の走査角[rad]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, method="GET", limit=64 * 1024 * 1024):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(limit + 1) if method == "GET" else b""
        if len(body) > limit:
            raise ValueError("too large")
        lm = r.headers.get("Last-Modified")
        return body, (email.utils.parsedate_to_datetime(lm) if lm else None)


def floor10(t):
    return t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def place_disk(src, cx, cy, r, size):
    """src の円盤（中心cx,cy・赤道半径r、画素中心=整数）を、規格どおり size 四方へ置く。"""
    s = DISK_R * size / r
    a = 1 / s
    c = (cx + 0.5) - DISK_CX * size / s
    f = (cy + 0.5) - DISK_CY * size / s
    return src.transform((size, size), Image.AFFINE, (a, 0, c, 0, a, f), resample=Image.BICUBIC)


def burn_label(img, obs, source):
    size = img.size[0]
    x0, y0, x1, y1 = [int(v * size) for v in LABEL_BOX]
    d = ImageDraw.Draw(img)
    d.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))
    k = size / 1024
    jst = obs + timedelta(hours=9)
    d.text((x0 + 8 * k, y0 + 4 * k), f"{obs:%Y-%m-%d %H:%M} UTC", fill=(235, 240, 255), font=font(int(22 * k)))
    d.text((x0 + 8 * k, y0 + 30 * k), f"{jst:%m/%d %H:%M} JST", fill=(235, 240, 255), font=font(int(22 * k)))
    d.text((x0 + 8 * k, y0 + 56 * k), source, fill=(140, 150, 175), font=font(int(12 * k)))
    return img


# ---------------- 高解像度（NOAA HSD） ----------------
def s3_keys(prefix):
    raw, _ = http(f"{S3}/?list-type=2&prefix={prefix}&max-keys=1000")
    return re.findall(r"<Key>([^<]+)</Key>", raw.decode())


def hsd_download(keys, workdir):
    def get(k):
        p = Path(workdir) / k.split("/")[-1]
        raw, _ = http(f"{S3}/{k}", limit=400 * 1024 * 1024)
        p.write_bytes(raw)
        return str(p)
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        files = list(ex.map(get, keys))
    log(f"HSD取得 {len(files)}件 {time.time() - t0:.0f}s")
    return files


def hsd_rgb(files):
    t0 = time.time()
    import dask
    import numpy as np
    from satpy import Scene
    from satpy.writers import get_enhanced_image
    dask.config.set(num_workers=4)
    scn = Scene(reader="ahi_hsd", filenames=files)
    comp = "true_color_reproduction"
    scn.load([comp], generate=False)
    a1 = scn.coarsest_area()
    f = a1.shape[0] // 5500
    lr = scn.resample(a1.aggregate(x=f, y=f) if f > 1 else a1, resampler="native")
    arr = np.asarray(get_enhanced_image(lr[comp]).data.values)[:3]
    arr = np.clip(np.nan_to_num(arr), 0, 1).transpose(1, 2, 0)
    if arr.shape[:2] != (5500, 5500):
        raise ValueError(f"想定外の格子 {arr.shape}")
    log(f"合成 {time.time() - t0:.0f}s")
    return Image.fromarray((arr * 255 + 0.5).astype("uint8"))


def hsd_region(files, box, finest, size):
    """走査角の長方形 box=(x0,y0,x1,y1)[rad] を size 四方の画像にする。
    finest=True なら 0.5km 格子（B03 で鮮鋭化）、False なら 1km 格子で合成する。
    切り出しは格子に合わせて丸められるので、最後に area の座標から正確に切り直す。"""
    t0 = time.time()
    import dask
    import numpy as np
    from satpy import Scene
    from satpy.writers import get_enhanced_image
    dask.config.set(num_workers=2)
    # 必要な帯（10分割のセグメント）のファイルだけ読む（全球を高解像度で読むとメモリが足りない）
    ext = 2750 * HSD_PIX
    segs = {min(10, max(1, int((ext - y) / (2 * ext) * 10) + 1)) for y in (box[1] - 0.004, box[3] + 0.004)}
    segs = set(range(min(segs), max(segs) + 1))
    files = [f for f in files if int(re.search(r"_S(\d\d)10", f).group(1)) in segs]
    scn = Scene(reader="ahi_hsd", filenames=files)
    comp = "true_color_reproduction"
    scn.load([comp], generate=False)
    h = scn["B01"].attrs["area"].crs.to_dict().get("h", 35785863.0)
    x0, y0, x1, y1 = [v * h for v in box]
    m = 0.02 * max(x1 - x0, y1 - y0)
    scn2 = scn.crop(xy_bbox=(x0 - m, y0 - m, x1 + m, y1 + m))
    area = scn2.finest_area() if finest else scn2["B01"].attrs["area"]
    lr = scn2.resample(area, resampler="native")
    data = lr[comp]
    arr = np.asarray(get_enhanced_image(data).data.values)[:3]
    arr = np.clip(np.nan_to_num(arr), 0, 1).transpose(1, 2, 0)
    img = Image.fromarray((arr * 255 + 0.5).astype("uint8"))
    ar = data.attrs["area"]
    xl, yl, xu, yu = ar.area_extent
    W, H = ar.width, ar.height
    dx, dy = (xu - xl) / W, (yu - yl) / H
    if not (xl <= x0 and x1 <= xu and yl <= y0 and y1 <= yu):
        raise ValueError(f"切り出し範囲が足りない {ar.area_extent} {(x0, y0, x1, y1)}")
    out = img.transform((size, size), Image.AFFINE,
                        ((x1 - x0) / size / dx, 0, (x0 - xl) / dx, 0, (y1 - y0) / size / dy, (yu - y1) / dy),
                        resample=Image.BICUBIC)
    log(f"部分合成 {'0.5km' if finest else '1km'} {img.size} -> {size} {time.time() - t0:.0f}s")
    return out


def hsd_detail(files):
    """真下まわりを 1km 格子で（低い高度用）。"""
    e = float(__import__("numpy").arcsin(6378137.0 / 42164160.0))
    return hsd_region(files, tuple(v * e for v in DETAIL_BOX), False, HI)


def hsd_japan(files):
    """日本まわりを 0.5km 格子で（ISS高度モード用）。"""
    return hsd_region(files, JAPAN_BOX, True, HI)


def save_tiles(canvas, folder, stem):
    """canvas(HI四方) を、継ぎ目用の重なり付き 2048px×4枚に分けて保存。0=左上 1=右上 2=左下 3=右下"""
    import numpy as np
    a = np.pad(np.asarray(canvas), ((PAD, PAD), (PAD, PAD), (0, 0)), mode="edge")
    h = HI // 2
    for i, (x, y) in enumerate([(0, 0), (h, 0), (0, h), (h, h)]):
        Image.fromarray(a[y:y + h + 2 * PAD, x:x + h + 2 * PAD]).save(folder / f"{stem}_{i}.jpg", quality=90, optimize=True)


LAYERS = (("hi", None), ("dt", "D"), ("jp", "J"))
SETS = (("asahi", -60.0), ("mahiru", 0.0), ("yuhi", 60.0))   # 名前と、ひまわり直下での太陽の時角[度]
SUB_LON = 140.7
JST = timedelta(hours=9)


def slot_keys(t):
    """そのスロットの HSD がそろっていればキー一覧、なければ []"""
    keys = [k for k in s3_keys(f"AHI-L1b-FLDK/{t:%Y/%m/%d/%H%M}/") if any(f"_{b}_FLDK" in k for b in HSD_BANDS)]
    return keys if len(keys) == 10 * len(HSD_BANDS) else []


def make_set(keys, obs, dest):
    """HSD から一式を作って dest/{hi,dt,jp}_k.jpg に置き、作れた印を返す。hi が作れなければ例外。
    Satpy/dask のメモリを確実に返すため、別プロセスで作る。"""
    dest.mkdir(parents=True, exist_ok=True)
    job = dest / "job.json"
    job.write_text(json.dumps({"keys": keys, "obs": f"{obs:%Y-%m-%dT%H:%MZ}"}), encoding="utf-8")
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--make-set", str(dest)])
    job.unlink(missing_ok=True)
    done = dest / "flags.txt"
    if r.returncode != 0 or not done.exists():
        raise RuntimeError(f"一式の作成に失敗（終了コード {r.returncode}）")
    flags = done.read_text(encoding="utf-8").strip()
    done.unlink()
    return flags


def make_set_child(dest):
    job = json.loads((dest / "job.json").read_text(encoding="utf-8"))
    keys = job["keys"]
    obs = datetime.strptime(job["obs"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as wd:
        files = hsd_download(keys, wd)
        canvas = burn_label(place_disk(hsd_rgb(files), HSD_CX, HSD_CY, HSD_R, HI), obs,
                            "Himawari-9 HSD (JMA via NOAA) / Satpy TCR")
        save_tiles(canvas, dest, "hi")
        del canvas
        flags = ""
        for layer, f, make in (("dt", "D", hsd_detail), ("jp", "J", hsd_japan)):
            try:
                save_tiles(make(files), dest, layer)
                flags += f
            except Exception as e:
                log(f"{layer} の作成に失敗（その層なしで配信）:", e)
    (dest / "flags.txt").write_text(flags, encoding="utf-8")
    return 0


def copy_set(src, src_names, dst, dst_names, flags):
    """src_names(layer,k)->ファイル名 の一式を dst へ dst_names の名前で写す"""
    dst.mkdir(parents=True, exist_ok=True)
    for layer, f in LAYERS:
        if f is None or f in flags:
            for k in range(4):
                d = dst / dst_names(layer, k)
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / src_names(layer, k), d)


# ---------------- 太陽の時角（低精度。空の計算 HimawariSky.cs と同じ式） ----------------
J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)


def sun_hour_angle(t, lon=SUB_LON):
    d = (t - J2000).total_seconds() / 86400.0
    L = math.radians((280.460 + 0.9856474 * d) % 360)
    g = math.radians((357.528 + 0.9856003 * d) % 360)
    lam = L + math.radians(1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * d)
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)))
    gmst = (280.46061837 + 360.98564736629 * d) % 360
    return (gmst + lon - ra + 180) % 360 - 180


def target_slot(hdeg, newest):
    """時角が hdeg になる観測（走査の中ほど=開始+5分で合わせる）のうち、newest 以前で最新のスロット"""
    best = None
    for back in range(3):
        day = (newest - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        t = day + timedelta(hours=12 - SUB_LON / 15 + hdeg / 15)
        for _ in range(4):
            dh = (hdeg - sun_hour_angle(t) + 180) % 360 - 180
            t += timedelta(days=dh / 360.98)
        s = floor10(t - timedelta(minutes=5))
        if t - timedelta(minutes=5) - s >= timedelta(minutes=5):
            s += timedelta(minutes=10)
        if s <= newest and (best is None or s > best):
            best = s
    return best


def gen_of(obs):
    return (obs + JST).toordinal() % 4


def load_store(store):
    try:
        return json.loads((store / "sets.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


INDEX = """<!doctype html><meta charset="utf-8"><title>Himawari Earth feed</title>
<body style="background:#000;color:#ccd;font-family:sans-serif;max-width:760px;margin:2em auto">
<h1>Himawari Earth feed (VRChat)</h1>
<p>VRChatワールド用の配信です。リアルタイム映像ではありません。時刻はいずれも観測開始の公称時刻(UTC)。</p>
<p>最新: <b>{hi}</b><br>{sets}</p>
<h2>出典</h2>
<p>ひまわり9号 標準データ（気象庁が作成、NOAA Open Data Dissemination Program により公開）を Satpy の
true_color_reproduction で加工して作成。加工済みであり、元データそのものではありません。<br>
本サイトは気象庁・NOAA・テラキャスターとは関係のない個人の制作物です。</p></body>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--store", default="store", help="あさひ・まひる・ゆうひ を回をまたいで置く場所（Actions のキャッシュ）")
    ap.add_argument("--site-url", default="", help="自分の Pages のURL（末尾 /）。最新の引き継ぎに使う")
    ap.add_argument("--now")
    ap.add_argument("--max-backfill", type=int, default=1)
    ap.add_argument("--make-set", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.make_set:
        return make_set_child(Path(args.make_set))
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out, store, site = Path(args.out), Path(args.store), args.site_url
    out.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)

    prev_head = None
    if site:
        try:
            raw, _ = http(site + "latest.txt?nocache=" + str(int(time.time())))
            prev_head = raw.decode("utf-8").split("\n")[0].strip().split("|")
        except Exception as e:
            log("前回の配信を読めません（初回なら正常）:", e)
    prev_hi, prev_flags = None, ""
    if prev_head and len(prev_head) >= 5 and prev_head[0] in ("HIMAWARI2", "HIMAWARI3") and prev_head[1] != "-":
        prev_hi = datetime.strptime(prev_head[1], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        prev_flags = prev_head[4]

    slot_name = lambda layer, k, t: f"{layer}/{t:%H%M}_{k}.jpg"

    def carry_latest(t, flags, layers=("hi", "dt", "jp")):
        got = ""
        for layer, f in LAYERS:
            if layer in layers and (f is None or f in flags):
                (out / layer).mkdir(parents=True, exist_ok=True)
                for k in range(4):
                    raw, _ = http(site + slot_name(layer, k, t))
                    (out / slot_name(layer, k, t)).write_bytes(raw)
                got += f or ""
        return got

    # ---- 最新 ----
    newest, keys = None, []
    t = floor10(now)
    for _ in range(9):
        keys = slot_keys(t)
        if keys:
            newest = t
            break
        t -= timedelta(minutes=10)
    hi_obs, flags = None, ""
    fresh = None                     # 今回作った一式の置き場
    tmp = Path(tempfile.mkdtemp())
    if newest is not None and newest == prev_hi:
        log("最新: 新しい観測なし。前回分を引き継ぎ")
        try:
            flags = carry_latest(newest, prev_flags)
            hi_obs = newest
        except Exception as e:
            log("引き継ぎ失敗", e)
    elif newest is not None:
        try:
            flags = make_set(keys, newest, tmp / "latest")
            fresh = tmp / "latest"
            copy_set(fresh, lambda l, k: f"{l}_{k}.jpg", out, lambda l, k: slot_name(l, k, newest), flags)
            hi_obs = newest
        except Exception as e:
            log("最新の作成に失敗:", e)
    if hi_obs is None and prev_hi is not None and now - prev_hi < timedelta(hours=3):
        try:
            flags = carry_latest(prev_hi, prev_flags)
            hi_obs = prev_hi
        except Exception as e:
            log("引き継ぎ失敗", e)
    # 直前の観測の全体画像も残す（latest.txt のキャッシュずれ対策）
    if hi_obs is not None and prev_hi is not None and prev_hi != hi_obs and now - prev_hi < timedelta(hours=3):
        try:
            carry_latest(prev_hi, prev_flags, layers=("hi",))
        except Exception:
            pass

    # ---- あさひ・まひる・ゆうひ ----
    sets = load_store(store)
    backfills = 0
    if newest is not None:
        todo = []
        for name, hdeg in SETS:
            tgt = target_slot(hdeg, newest)
            cur = sets.get(name, {}).get("obs")
            if tgt is None or cur == f"{tgt:%Y-%m-%dT%H:%MZ}":
                continue
            todo.append((tgt, name))
        for tgt, name in sorted(todo, reverse=True):          # 新しい観測から
            dest = store / name
            try:
                if fresh is not None and tgt == newest:
                    if dest.exists():
                        shutil.rmtree(dest)
                    copy_set(fresh, lambda l, k: f"{l}_{k}.jpg", dest, lambda l, k: f"{l}_{k}.jpg", flags)
                    sf = flags
                elif backfills < args.max_backfill:
                    backfills += 1
                    k2 = slot_keys(tgt)
                    if not k2:
                        log(f"{name}: {tgt} のデータがそろっていない")
                        continue
                    if dest.exists():
                        shutil.rmtree(dest)
                    log(f"{name}: {tgt:%Y-%m-%d %H:%M} を過去データから作成")
                    sf = make_set(k2, tgt, dest)
                else:
                    continue
                sets[name] = {"obs": f"{tgt:%Y-%m-%dT%H:%MZ}", "flags": sf}
                (store / "sets.json").write_text(json.dumps(sets), encoding="utf-8")
            except Exception as e:
                log(f"{name} の作成に失敗:", e)
                sets.pop(name, None)
                (store / "sets.json").write_text(json.dumps(sets), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)

    set_lines = []
    (out / "s").mkdir(parents=True, exist_ok=True)
    for name, _ in SETS:
        info = sets.get(name)
        if info and (store / name / "hi_0.jpg").exists():
            obs = datetime.strptime(info["obs"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            g = gen_of(obs)
            copy_set(store / name, lambda l, k: f"{l}_{k}.jpg", out / "s", lambda l, k: f"{name}{g}_{l}_{k}.jpg", info["flags"])
            set_lines.append(f"SET|{name}|{info['obs']}|{g}|{info['flags'] or '-'}")
        else:
            set_lines.append(f"SET|{name}|-|-|-")

    if hi_obs is None and all(l.endswith("|-|-|-") for l in set_lines):
        log("配信できる観測がありません。サイトを更新しません。")
        return 2
    processed = datetime.now(timezone.utc)
    hi_s = f"{hi_obs:%Y-%m-%dT%H:%MZ}" if hi_obs else "-"
    slot_s = f"{hi_obs:%H%M}" if hi_obs else "-"
    (out / "latest.txt").write_text(
        f"HIMAWARI3|{hi_s}|{slot_s}|{processed:%Y-%m-%dT%H:%M:%SZ}|{flags or '-'}\n" + "\n".join(set_lines) + "\n",
        encoding="utf-8")
    (out / "index.html").write_text(INDEX.format(
        hi=(f"{hi_obs:%Y-%m-%d %H:%M} UTC" if hi_obs else "なし"),
        sets="<br>".join(l.replace("SET|", "") for l in set_lines)), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    log(f"OK 最新={hi_s} " + " ".join(set_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
