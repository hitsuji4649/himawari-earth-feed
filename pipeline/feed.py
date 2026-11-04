#!/usr/bin/env python3
"""Himawari Earth 配信 v2（GitHub Actions から10分ごとに実行）

1) 最新の高解像度画像:
   NOAA が公開しているひまわり9号の標準データ(HSD)から、Satpy のトゥルーカラー再現で
   4096px の画像を作り、2048px×4枚に分けて hi/HHMM_0..3.jpg に置く。
2) 過去24時間の履歴（時間の前後用）:
   気象庁Webのフルディスク・トゥルーカラー画像(601px)を 1024px に整えて hist/HHMM.jpg に置く。
   前回の配信分を自分の Pages から引き継ぎ、足りない分だけ気象庁から取る。
3) latest.txt（Udon が読む）:
   1行目 HIMAWARI2|高解像度の観測UTC|スロット|処理UTC
   2行目 履歴の観測時刻(YYYYMMDDHHMM)を新しい順にカンマ区切り

観測時刻は常に「観測開始の公称時刻」。日付は推測せず、ファイルの更新時刻やデータ名から確定する。
夜側は観測のまま暗い。昼の画像で埋めない。

使い方: python feed.py --out site --site-url https://USER.github.io/REPO/
依存: Pillow, satpy, pyspectral, pyorbital (高解像度を作らないなら Pillow だけで動く: --no-hires)
"""
import argparse
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

UA = "HimawariEarthVRC/2.0 (personal VRChat world feed)"

# ---- 画像の規格（シェーダーと共有。変えるときは HimawariObservedEarth.shader も変える） ----
# 正方形キャンバスに対する比率（左上原点）。高解像度4096・履歴1024で同じ比率。
DISK_CX, DISK_CY, DISK_R = 0.5, 2010 / 4096, 1990 / 4096
LABEL_BOX = (0.0, 0.928, 0.27, 1.0)      # 左下の角（円盤の外）に観測時刻を焼き込む
HI = 4096
LO = 1024

# ---- 気象庁Web画像(601px, 東経140.0°中心) ----
JMA = "https://www.data.jma.go.jp/mscweb/data/himawari/img/fd_/fd__trm_{hhmm}.jpg"
JMA_PAGE = "https://www.data.jma.go.jp/mscweb/data/himawari/list_fd_.html"
JMA_CX, JMA_CY, JMA_R = 300.30, 288.02, 287.87   # 海岸線との照合で推定
MAX_LAG = timedelta(minutes=90)
HIST_WINDOW = timedelta(hours=24)
JMA_NEW_PER_RUN = 40                             # 初回に気象庁へ一度に取りに行きすぎない

# ---- NOAA公開のHSD(東経140.7°, 2km格子5500px) ----
S3 = "https://noaa-himawari9.s3.amazonaws.com"
HSD_BANDS = ("B01", "B02", "B03", "B04", "B13")
HSD_CX = HSD_CY = 2750.0
HSD_R = 2717.08                                  # 赤道の縁までの画素数（area_extent から計算）


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


# ---------------- 履歴（気象庁） ----------------
def jma_valid(obs):
    """そのスロットの気象庁ファイルが obs の観測か（日付の確定）を Last-Modified で判定。"""
    try:
        _, lm = http(JMA.format(hhmm=obs.strftime("%H%M")), "HEAD")
    except Exception:
        return False
    return lm is not None and timedelta(0) <= lm - obs <= MAX_LAG


def jma_hist_image(obs):
    raw, lm = http(JMA.format(hhmm=obs.strftime("%H%M")))
    if lm is None or not (timedelta(0) <= lm - obs <= MAX_LAG):
        return None
    src = Image.open(BytesIO(raw)).convert("RGB")
    if src.size != (601, 601):
        raise ValueError(f"気象庁画像のサイズが想定外: {src.size}")
    return burn_label(place_disk(src, JMA_CX, JMA_CY, JMA_R, LO), obs, "JMA TCR 601px")


def read_previous(site_url):
    """前回配信の latest.txt を読む（無ければ空）。"""
    if not site_url:
        return None, []
    try:
        raw, _ = http(site_url + "latest.txt?nocache=" + str(int(time.time())))
        lines = raw.decode("utf-8").strip().split("\n")
        head = lines[0].split("|")
        hist = [datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                for s in (lines[1].split(",") if len(lines) > 1 and lines[1] else [])]
        return head, hist
    except Exception as e:
        log("前回の配信を読めません（初回なら正常）:", e)
        return None, []


def build_history(out, now, site_url, prev_hist):
    (out / "hist").mkdir(parents=True, exist_ok=True)
    have = {}
    # 1) 前回分を引き継ぐ
    for obs in prev_hist:
        if now - obs >= HIST_WINDOW:
            continue
        name = obs.strftime("%H%M")
        try:
            raw, _ = http(site_url + f"hist/{name}.jpg")
            (out / "hist" / f"{name}.jpg").write_bytes(raw)
            have[obs] = True
        except Exception as e:
            log("引き継ぎ失敗", name, e)
    # 2) 足りない分を気象庁から（新しい順）
    t = floor10(now)
    added = 0
    while now - t < HIST_WINDOW and added < JMA_NEW_PER_RUN:
        if t not in have and t.strftime("%H%M") not in {o.strftime("%H%M") for o in have}:
            if jma_valid(t):
                try:
                    img = jma_hist_image(t)
                    if img is not None:
                        img.save(out / "hist" / f"{t:%H%M}.jpg", quality=88, optimize=True)
                        have[t] = True
                        added += 1
                except Exception as e:
                    log("履歴取得失敗", t, e)
                time.sleep(0.3)
        t -= timedelta(minutes=10)
    log(f"履歴: {len(have)} 枚（今回追加 {added}）")
    return sorted(have.keys(), reverse=True)


# ---------------- 高解像度（NOAA HSD） ----------------
def s3_keys(prefix):
    raw, _ = http(f"{S3}/?list-type=2&prefix={prefix}&max-keys=1000")
    return re.findall(r"<Key>([^<]+)</Key>", raw.decode())


def newest_hsd_slot(now):
    t = floor10(now)
    for _ in range(9):
        keys = [k for k in s3_keys(f"AHI-L1b-FLDK/{t:%Y/%m/%d/%H%M}/")
                if any(f"_{b}_FLDK" in k for b in HSD_BANDS)]
        if len(keys) == 10 * len(HSD_BANDS):
            return t, keys
        t -= timedelta(minutes=10)
    return None, []


def hsd_rgb(keys, workdir):
    def get(k):
        p = Path(workdir) / k.split("/")[-1]
        raw, _ = http(f"{S3}/{k}", limit=400 * 1024 * 1024)
        p.write_bytes(raw)
        return str(p)
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        files = list(ex.map(get, keys))
    log(f"HSD取得 {len(files)}件 {time.time() - t0:.0f}s")
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


def write_hi(out, obs, rgb):
    (out / "hi").mkdir(parents=True, exist_ok=True)
    canvas = burn_label(place_disk(rgb, HSD_CX, HSD_CY, HSD_R, HI), obs, "Himawari-9 HSD (JMA via NOAA) / Satpy TCR")
    h = HI // 2
    for i, (x, y) in enumerate([(0, 0), (h, 0), (0, h), (h, h)]):   # 0=左上 1=右上 2=左下 3=右下
        canvas.crop((x, y, x + h, y + h)).save(out / "hi" / f"{obs:%H%M}_{i}.jpg", quality=90, optimize=True)


def carry_hi(out, site_url, slot):
    (out / "hi").mkdir(parents=True, exist_ok=True)
    for i in range(4):
        raw, _ = http(site_url + f"hi/{slot}_{i}.jpg")
        (out / "hi" / f"{slot}_{i}.jpg").write_bytes(raw)


INDEX = """<!doctype html><meta charset="utf-8"><title>Himawari Earth feed</title>
<body style="background:#000;color:#ccd;font-family:sans-serif;max-width:760px;margin:2em auto">
<h1>Himawari Earth feed (VRChat)</h1>
<p>VRChatワールド用の配信です。リアルタイム映像ではありません。時刻はいずれも観測開始の公称時刻。</p>
<p>高解像度の最新: <b>{hi}</b> ／ 履歴: {n}枚（最新 {h0}）</p>
<h2>出典</h2>
<p>高解像度: ひまわり9号 標準データ（気象庁が作成、NOAA Open Data Dissemination Program により公開）を Satpy の
true_color_reproduction で加工して作成。加工済みであり、元データそのものではありません。<br>
履歴: 気象庁「ひまわり フルディスク トゥルーカラー再現画像」（{jma}）を加工して作成。JMA / NOAA/NESDIS / CSU/CIRA。<br>
本サイトは気象庁・NOAA・テラキャスターとは関係のない個人の制作物です。</p></body>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--site-url", default="", help="自分の Pages のURL（末尾 /）。前回分の引き継ぎに使う")
    ap.add_argument("--no-hires", action="store_true")
    ap.add_argument("--now")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    site = args.site_url

    prev_head, prev_hist = read_previous(site)
    hist = build_history(out, now, site, prev_hist)

    hi_obs = None
    prev_hi = None
    if prev_head and len(prev_head) >= 3 and prev_head[0] == "HIMAWARI2" and prev_head[1] != "-":
        prev_hi = datetime.strptime(prev_head[1], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    if not args.no_hires:
        t, keys = newest_hsd_slot(now)
        if t is not None and t == prev_hi:
            log("高解像度: 新しい観測なし。前回分を引き継ぎ")
            try:
                carry_hi(out, site, t.strftime("%H%M"))
                hi_obs = t
            except Exception as e:
                log("引き継ぎ失敗", e)
        elif t is not None:
            with tempfile.TemporaryDirectory() as wd:
                try:
                    write_hi(out, t, hsd_rgb(keys, wd))
                    hi_obs = t
                except Exception as e:
                    log("高解像度の作成に失敗:", e)
        if hi_obs is None and prev_hi is not None and now - prev_hi < timedelta(hours=3):
            try:
                carry_hi(out, site, prev_hi.strftime("%H%M"))
                hi_obs = prev_hi
            except Exception as e:
                log("引き継ぎ失敗", e)
        # 直前の高解像度も1組残す（配信情報のキャッシュずれ対策）
        if hi_obs is not None and prev_hi is not None and prev_hi != hi_obs and now - prev_hi < timedelta(hours=3):
            try:
                carry_hi(out, site, prev_hi.strftime("%H%M"))
            except Exception:
                pass

    if not hist and hi_obs is None:
        log("配信できる観測がありません。サイトを更新しません。")
        return 2
    processed = datetime.now(timezone.utc)
    hi_s = f"{hi_obs:%Y-%m-%dT%H:%MZ}" if hi_obs else "-"
    slot_s = f"{hi_obs:%H%M}" if hi_obs else "-"
    (out / "latest.txt").write_text(
        f"HIMAWARI2|{hi_s}|{slot_s}|{processed:%Y-%m-%dT%H:%M:%SZ}\n"
        + ",".join(f"{o:%Y%m%d%H%M}" for o in hist) + "\n", encoding="utf-8")
    (out / "index.html").write_text(INDEX.format(
        hi=(f"{hi_obs:%Y-%m-%d %H:%M} UTC" if hi_obs else "なし"), n=len(hist),
        h0=(f"{hist[0]:%Y-%m-%d %H:%M} UTC" if hist else "-"), jma=JMA_PAGE), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    log(f"OK 高解像度={hi_s} 履歴={len(hist)}枚")
    return 0


if __name__ == "__main__":
    sys.exit(main())
