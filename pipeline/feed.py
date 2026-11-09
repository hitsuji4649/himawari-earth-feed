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
HSD_BANDS = ("B01", "B02", "B03", "B04", "B07", "B13", "B15")   # B07/B13/B15 は夜の雲の見分け用
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


# ---------------- 夜側（赤外の雲を月明かり相当で＋街明かりの地図） ----------------
# 夜の可視画像は真っ暗なので、その時刻のひまわり赤外観測(B13, 10.4µm)から雲の濃さを推定し、
# 月の高さと満ち欠けに応じた明るさで描く（見やすく強調）。街明かりは NASA Black Marble 2016（年平均の地図）を
# 雲の濃さで減光して重ねる。どちらも「観測そのもの」ではないので、受け手側の表示で区別する。
BM_URL = "https://assets.science.nasa.gov/content/dam/science/esd/eo/images/imagerecords/144000/144897/"
BM_FILES = {"global": "BlackMarble_2016_3km_gray.jpg",       # 13500x6750, 経度-180..180, 緯度90..-90
            "D1": "BlackMarble_2016_D1_gray.jpg",            # 21600x21600, 経度90..180, 緯度90..0 (500m)
            "D2": "BlackMarble_2016_D2_gray.jpg"}            # 21600x21600, 経度90..180, 緯度0..-90
BM_DIR = Path(os.environ.get("HIMAWARI_BM_DIR", Path.home() / ".cache" / "himawari_bm"))
NIGHT_MOON_GAIN = 0.32       # 満月が真上のときの雲の明るさ（表示値。実際より強調）
NIGHT_BASE = 0.012           # 月がなくても星明かりでごくわずかに
LIGHTS_GAIN = 0.75           # 都市の中心の明るさ（表示値）
LIGHTS_FLOOR = 30.0          # 地図の値でこれ以下は暗い地面とみなす
LIGHTS_GAMMA = 2.2           # 大きいほど郊外の薄い光が沈み、芯だけが光る
LIGHTS_HALO_CUT = 0.55       # 元の地図に含まれる光のにじみを差し引く割合
LIGHTS_DIM = (1.0, 0.62, 0.30)    # 暗めの明かり（ナトリウム灯寄りのオレンジ）
LIGHTS_CORE = (1.0, 0.88, 0.70)   # 明るい中心部（LED寄りの白）
MOON_COLOR = (0.78, 0.84, 0.95)
THIN_CLOUD_DIM = 0.8         # 薄い巻雲の下で街明かりを減らす割合
# 雲の下の街明かり。NOAA-20 VIIRS 夜間画像（Day/Night Band）と5晩比べて決めた（2026-11-01〜07、日本付近）:
#   赤外で雲と判定される所でも、街明かりはたいてい見えている（ぼやけて広がる）。暗くなるのは最も厚い雲の下だけ。
LIGHTS_THICK = (0.8, 1.0, 0.6)    # 雲の判定値 0.8→1.0 で、明かりを最大 60% 減らす
LIGHTS_DIFFUSE = (0.55, 0.95)     # 雲の判定値 0.55→0.95 で、明かりを「点」から「雲ににじんだ光」へ移す
LIGHTS_DIFFUSE_KM = 4.0           # にじみの広がり（ガウスの幅, km）
# くっきり処理（センサーのぼけを戻すアンシャープ）。データにない模様は足さない: 結果を周囲3x3の明るさの範囲に収める
SHARPEN = {0.5: (1.2, 1.0), 1.0: (1.0, 0.6)}   # 格子km: (ぼけの幅[画素], 強さ)


def sharpen(arr, km):
    """arr: (H,W,3) 0..1。明るさだけをくっきりさせ、輪郭のふちに白黒の縁取り（ハロー）が出ないよう抑える。"""
    import numpy as np
    from scipy import ndimage
    if km not in SHARPEN:
        return arr
    sigma, amount = SHARPEN[km]
    t0 = time.time()
    Y = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]).astype(np.float32)
    Ys = Y + amount * (Y - ndimage.gaussian_filter(Y, sigma))
    lo = ndimage.minimum_filter(Y, size=3) - 0.01
    hi = ndimage.maximum_filter(Y, size=3) + 0.01
    Ys = np.clip(Ys, lo, hi)
    out = np.clip(arr + (Ys - Y)[..., None], 0, 1)
    log(f"くっきり処理 {km}km {time.time() - t0:.0f}s")
    return out


def bm_path(key):
    BM_DIR.mkdir(parents=True, exist_ok=True)
    p = BM_DIR / BM_FILES[key]
    if not p.exists():
        raw, _ = http(BM_URL + BM_FILES[key], limit=200 * 1024 * 1024)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(raw)
        tmp.rename(p)
    return p


class Lights:
    """街明かりの地図（0..255）。res_km に合わせて地図を選ぶ: 2km以上=3km全球、1km=500m地図を1kmに縮小、0.5km=500m。
    日本付近（90°E〜180°）以外は 3km 全球のみ。地図は一度だけ読む。"""
    def __init__(self, res_km):
        import numpy as np
        Image.MAX_IMAGE_PIXELS = None
        self.g = np.asarray(Image.open(bm_path("global")).convert("L"))
        self.t = {}
        if res_km < 2:
            for key in ("D1", "D2"):
                try:
                    im = Image.open(bm_path(key)).convert("L")
                    if res_km >= 1:
                        im = im.reduce(2)                     # 1km 格子用に平均して縮小（ちらつき防止）
                    self.t[key] = np.asarray(im)
                except Exception as e:
                    log("街明かり 500m 地図を使えません（3kmで代用）:", key, e)

    def at(self, lon, lat):
        import numpy as np
        out = np.zeros(lon.shape, np.float32)
        ok = np.isfinite(lon) & np.isfinite(lat)
        h, w = self.g.shape
        x = np.clip(((lon + 180) / 360 * w).astype(np.int32), 0, w - 1)
        y = np.clip(((90 - lat) / 180 * h).astype(np.int32), 0, h - 1)
        out[ok] = self.g[y[ok], x[ok]]
        for key, la0 in (("D1", 90.0), ("D2", 0.0)):
            t = self.t.get(key)
            if t is None:
                continue
            m = ok & (lon >= 90) & (lat <= la0) & (lat > la0 - 90)
            if not m.any():
                continue
            n = t.shape[0]
            xi = np.clip(((lon[m] - 90) / 90 * n).astype(np.int32), 0, n - 1)
            yi = np.clip(((la0 - lat[m]) / 90 * n).astype(np.int32), 0, n - 1)
            out[m] = t[yi, xi]
        return out


def moon_state(t):
    """月の赤経・赤緯[rad]と、照らされている割合（0..1）。低精度（約0.5°）。"""
    import math
    d = (t - datetime(1999, 12, 31, tzinfo=timezone.utc)).total_seconds() / 86400.0
    rad = math.radians
    N = rad((125.1228 - 0.0529538083 * d) % 360); i = rad(5.1454); w = rad((318.0634 + 0.1643573223 * d) % 360)
    e = 0.054900; M = rad((115.3654 + 13.0649929509 * d) % 360)
    E = M + e * math.sin(M) * (1 + e * math.cos(M))
    for _ in range(5):
        E = E - (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    xv = math.cos(E) - e; yv = math.sqrt(1 - e * e) * math.sin(E)
    v = math.atan2(yv, xv)
    xh = math.cos(N) * math.cos(v + w) - math.sin(N) * math.sin(v + w) * math.cos(i)
    yh = math.sin(N) * math.cos(v + w) + math.cos(N) * math.sin(v + w) * math.cos(i)
    zh = math.sin(v + w) * math.sin(i)
    lon_m = math.atan2(yh, xh); lat_m = math.atan2(zh, math.hypot(xh, yh))
    Ms = rad((356.0470 + 0.9856002585 * d) % 360); ws = rad((282.9404 + 4.70935e-5 * d) % 360)
    lon_s = (Ms + ws + rad(1.915) * math.sin(Ms) + rad(0.020) * math.sin(2 * Ms))
    eps = rad(23.4393 - 3.563e-7 * d)
    x = math.cos(lon_m) * math.cos(lat_m); y = math.sin(lon_m) * math.cos(lat_m); z = math.sin(lat_m)
    ye = y * math.cos(eps) - z * math.sin(eps); ze = y * math.sin(eps) + z * math.cos(eps)
    ra = math.atan2(ye, x); dec = math.asin(ze)
    elong = math.acos(math.cos(lat_m) * math.cos(lon_m - lon_s))
    return ra, dec, (1 - math.cos(elong)) / 2


def gmst_rad(t):
    import math
    d = (t - J2000).total_seconds() / 86400.0
    return math.radians((280.46061837 + 360.98564736629 * d) % 360)


def cloud_from_bt(bt, lat, cell):
    """赤外の輝度温度[K]から雲の濃さ 0..1 を推定。cell=約100kmの画素数。
    晴れた地表の温度を、まわり約900kmの暖かいほう(95%)と緯度ごとの目安の大きいほうで見積もり、それより冷たいほど雲。"""
    import numpy as np
    from scipy import ndimage
    H, W = bt.shape
    b = np.where(np.isfinite(bt), bt, np.nan)
    hb, wb = H // cell, W // cell
    if hb >= 3 and wb >= 3:
        blk = b[:hb * cell, :wb * cell].reshape(hb, cell, wb, cell).transpose(0, 2, 1, 3).reshape(hb, wb, -1)
        p95 = np.nanpercentile(blk, 95, axis=-1)
        p95 = np.where(np.isfinite(p95), p95, np.nanmedian(p95))
        p95 = ndimage.maximum_filter(p95, size=9, mode="nearest")
        clear = ndimage.zoom(p95, (H / hb, W / wb), order=1)[:H, :W]
        if clear.shape != bt.shape:
            clear = np.pad(clear, ((0, H - clear.shape[0]), (0, W - clear.shape[1])), mode="edge")
    else:
        clear = np.full(bt.shape, np.nanpercentile(b, 95))
    climo = 300.0 - 25.0 * (np.nan_to_num(np.abs(lat), nan=0) / 60.0) ** 2
    clear = np.maximum(clear, climo - 25.0)
    c = np.clip((clear - b - 3.0) / 25.0, 0, 1) ** 0.8
    return np.nan_to_num(c).astype(np.float32)


def night_clouds(bts, lat_c, cell, up):
    """夜の雲を3つの赤外で見分ける（気象庁などの「夜の雲」判別と同じ考え方）。
    戻り: show（月明かりで見える雲の濃さ）, block（街明かりを消す割合）, thin（薄い巻雲の割合）"""
    import numpy as np
    from scipy import ndimage
    def smooth(b):
        b = np.where(np.isfinite(b), b, np.nanmean(b))
        return ndimage.gaussian_filter(b, sigma=0.6 * up).astype(np.float32) if up > 1 else b.astype(np.float32)
    b13 = smooth(bts["B13"])
    cold = cloud_from_bt(b13, lat_c, cell)                          # 冷たいほど（高い雲ほど）大きい
    if "B15" in bts and "B07" in bts:
        b15 = smooth(bts["B15"]); b07 = smooth(bts["B07"])
        ci = np.clip((b13 - b15 - 1.0) / 3.0, 0, 1)                # 11-12µm 差が大きい = 薄い氷の雲（巻雲）
        low = np.clip((b13 - b07 - 1.5) / 3.0, 0, 1)               # 3.9µm が冷たく出る = 低い水の雲・霧
        low *= np.clip((b13 - 250.0) / 15.0, 0, 1)                 # 冷たい雲の上では判定しない
        del b15, b07
    else:
        ci = np.zeros_like(cold); low = np.zeros_like(cold)
    thin = cold * ci                                                # 冷たいが薄い雲
    thick_cold = cold * (1 - ci)
    block = np.clip(np.maximum(thick_cold, low) + THIN_CLOUD_DIM * thin, 0, 1)
    show = np.clip(np.maximum(cold, low), 0, 1)
    return show, block, thin


def add_night(arr, bts, area, t_obs, cell, res_km, split_lights=False, rows=400):
    """arr: 表示用RGB (H,W,3) 0..1。夜側に雲（月明かり相当）と街明かりを足して返す。
    bt は res_km の格子に「そのまま拡大」された赤外（2km）。四角いむらが出ないよう、なめらかにしてから使う。"""
    import numpy as np
    from scipy import ndimage
    from pyorbital.astronomy import sun_zenith_angle
    t0 = time.time()
    arr = np.asarray(arr, np.float32)
    tmid = t_obs + timedelta(minutes=5)
    H, W = bts["B13"].shape
    up = max(1.0, 2.0 / res_km)                    # 赤外の1画素がこの格子の何画素か
    lat_c = np.asarray(area.get_lonlats(dtype=np.float32, data_slice=(slice(None), slice(W // 2, W // 2 + 1)))[1])
    lat_c = np.where(np.abs(lat_c) > 1e3, np.nan, lat_c).reshape(H, 1)     # 行ごとのおおよその緯度
    cloud, block, thin = night_clouds(bts, lat_c, cell, up)
    ra, dec, illum = moon_state(tmid)
    g0 = gmst_rad(tmid)
    # 1) 夜の重み・月の高さ・街明かりの地図を、行ごとに求める
    night = np.zeros((H, W), np.float32)
    moon = np.zeros((H, W), np.float32)
    lmap = np.zeros((H, W), np.float32)
    lights = None
    for r0 in range(0, H, rows):
        r1 = min(H, r0 + rows)
        lon, lat = area.get_lonlats(dtype=np.float32, data_slice=(slice(r0, r1), slice(None)))
        lon = np.where(np.abs(lon) > 1e3, np.nan, lon).astype(np.float32)
        lat = np.where(np.abs(lat) > 1e3, np.nan, lat).astype(np.float32)
        okk = np.isfinite(lon)
        if not okk.any():
            continue
        sza = sun_zenith_angle(tmid.replace(tzinfo=None), np.nan_to_num(lon), np.nan_to_num(lat))
        nw = np.clip((sza - 90.0) / 8.0, 0, 1).astype(np.float32)
        nw[~okk] = 0
        night[r0:r1] = nw
        if nw.max() <= 0:
            continue
        if lights is None:
            lights = Lights(res_km)
        Hh = g0 + np.radians(np.nan_to_num(lon)) - ra
        la = np.radians(np.nan_to_num(lat))
        sin_alt = (np.sin(la) * np.sin(dec) + np.cos(la) * np.cos(dec) * np.cos(Hh)).astype(np.float32)
        moon[r0:r1] = NIGHT_MOON_GAIN * illum * np.clip(sin_alt, 0, 1) ** 0.7 + NIGHT_BASE
        lmap[r0:r1] = lights.at(lon, lat)
    if night.max() <= 0:
        log("夜側なし")
        return (arr, None) if split_lights else arr
    del lights
    # 2) 街明かり: 地図のにじみを差し引き、暗い部分を沈めて芯を残す（約1.5kmのぼかしとの差）
    halo = ndimage.gaussian_filter(lmap, sigma=max(1.0, 1.5 / res_km))
    sharp = np.clip((lmap - LIGHTS_HALO_CUT * halo) / (1 - LIGHTS_HALO_CUT), 0, 255)
    v = np.clip((sharp - LIGHTS_FLOOR) / (255.0 - LIGHTS_FLOOR), 0, 1)
    li = LIGHTS_GAIN * v ** LIGHTS_GAMMA
    # 雲: 雲越しの明かりはぼやけて広がる（雲に散らされる）。最も厚い雲の下だけ暗くなる
    def sstep(x, a, b):
        t = np.clip((x - a) / (b - a), 0, 1)
        return t * t * (3 - 2 * t)
    t0_, t1_, tmax = LIGHTS_THICK
    li = li * (1 - tmax * sstep(block, t0_, t1_))
    dfz = sstep(block, *LIGHTS_DIFFUSE)
    glow = ndimage.gaussian_filter(li * dfz, sigma=max(1.0, LIGHTS_DIFFUSE_KM / res_km))
    li = li * (1 - dfz)
    del lmap, halo, sharp
    # 3) 色: 明るい中心ほど白（LED）、暗いほどオレンジ（ナトリウム灯）
    k = np.clip((v - 0.25) / 0.6, 0, 1)[..., None]
    col = np.array(LIGHTS_DIM, np.float32) * (1 - k) + np.array(LIGHTS_CORE, np.float32) * k
    clouds = (cloud * moon)[..., None] * np.array(MOON_COLOR, np.float32)
    log(f"夜側 合成 {time.time() - t0:.0f}s 月の照らされた割合 {illum:.2f}")
    if split_lights:
        # 街明かりは別の画像で渡す（受け手側で細かい光の点として描く）。値 = 明るさ/LIGHTS_GAIN
        # 赤 = 点として描く街明かり、緑 = 薄い雲ににじんだ光（なめらかに描く）
        return np.clip(arr + night[..., None] * clouds, 0, 1), np.stack([np.clip(night * li / LIGHTS_GAIN, 0, 1), np.clip(night * glow / LIGHTS_GAIN, 0, 1)], -1)
    return np.clip(arr + night[..., None] * (clouds + (li + glow)[..., None] * col), 0, 1)


def hsd_rgb(files):
    t0 = time.time()
    import dask
    import numpy as np
    from satpy import Scene
    from satpy.writers import get_enhanced_image
    dask.config.set(num_workers=4)
    scn = Scene(reader="ahi_hsd", filenames=files)
    comp = "true_color_reproduction"
    scn.load([comp, "B07", "B13", "B15"], generate=False)
    a1 = scn.coarsest_area()
    f = a1.shape[0] // 5500
    lr = scn.resample(a1.aggregate(x=f, y=f) if f > 1 else a1, resampler="native")
    arr = np.asarray(get_enhanced_image(lr[comp]).data.values, np.float32)[:3]
    arr = np.clip(np.nan_to_num(arr), 0, 1).transpose(1, 2, 0)
    if arr.shape[:2] != (5500, 5500):
        raise ValueError(f"想定外の格子 {arr.shape}")
    try:
        bts = {b: np.asarray(lr[b].values, np.float32) for b in ("B07", "B13", "B15")}
        area, st = lr[comp].attrs["area"], lr[comp].attrs["start_time"].replace(tzinfo=timezone.utc)
        del lr, scn
        import gc; gc.collect()
        arr = add_night(arr, bts, area, st, 50, 2.0)
    except Exception as e:
        log("夜側の合成に失敗（夜側は観測のまま）:", e)
    log(f"合成 {time.time() - t0:.0f}s")
    return Image.fromarray((arr * 255 + 0.5).astype("uint8"))


def hsd_region(files, box, finest, size, split_lights=False):
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
    scn.load([comp, "B07", "B13", "B15"], generate=False)
    h = scn["B01"].attrs["area"].crs.to_dict().get("h", 35785863.0)
    x0, y0, x1, y1 = [v * h for v in box]
    m = 0.02 * max(x1 - x0, y1 - y0)
    scn2 = scn.crop(xy_bbox=(x0 - m, y0 - m, x1 + m, y1 + m))
    area = scn2.finest_area() if finest else scn2["B01"].attrs["area"]
    lr = scn2.resample(area, resampler="native")
    data = lr[comp]
    arr = np.asarray(get_enhanced_image(data).data.values, np.float32)[:3]
    arr = np.clip(np.nan_to_num(arr), 0, 1).transpose(1, 2, 0)
    arr = sharpen(arr, 0.5 if finest else 1.0)
    lights = None
    try:
        km = 0.5 if finest else 1.0
        bts = {b: np.asarray(lr[b].values, np.float32) for b in ("B07", "B13", "B15")}
        res = add_night(arr, bts, data.attrs["area"], data.attrs["start_time"].replace(tzinfo=timezone.utc), int(100 / km), km, split_lights)
        if split_lights:
            arr, lights = res
        else:
            arr = res
        del bts
    except Exception as e:
        log("夜側の合成に失敗（夜側は観測のまま）:", e)
    img = Image.fromarray((arr * 255 + 0.5).astype("uint8"))
    ar = data.attrs["area"]
    xl, yl, xu, yu = ar.area_extent
    W, H = ar.width, ar.height
    dx, dy = (xu - xl) / W, (yu - yl) / H
    if not (xl <= x0 and x1 <= xu and yl <= y0 and y1 <= yu):
        raise ValueError(f"切り出し範囲が足りない {ar.area_extent} {(x0, y0, x1, y1)}")
    aff = ((x1 - x0) / size / dx, 0, (x0 - xl) / dx, 0, (y1 - y0) / size / dy, (yu - y1) / dy)
    out = img.transform((size, size), Image.AFFINE, aff, resample=Image.BICUBIC)
    log(f"部分合成 {'0.5km' if finest else '1km'} {img.size} -> {size} {time.time() - t0:.0f}s")
    if not split_lights:
        return out
    # 街明かりの明るさの地図（受け手が sRGB として読むので、1/2.2 乗して 8bit に）
    if lights is None:
        lights = np.zeros(arr.shape[:2] + (2,), np.float32)
    rgb = np.zeros(arr.shape[:2] + (3,), np.float32)
    rgb[..., :2] = lights
    limg = Image.fromarray((np.clip(rgb, 0, 1) ** (1 / 2.2) * 255 + 0.5).astype("uint8"))
    lout = limg.transform((size, size), Image.AFFINE, aff, resample=Image.BILINEAR)
    return out, lout


def hsd_detail(files):
    """真下まわりを 1km 格子で（低い高度用）。"""
    e = float(__import__("numpy").arcsin(6378137.0 / 42164160.0))
    return hsd_region(files, tuple(v * e for v in DETAIL_BOX), False, HI)


def hsd_japan(files):
    """日本まわりを 0.5km 格子で（ISS高度モード用）。戻り: (画像, 街明かりの明るさの地図)"""
    return hsd_region(files, JAPAN_BOX, True, HI, split_lights=True)


def save_tiles(canvas, folder, stem, exact=False):
    """canvas(HI四方) を、継ぎ目用の重なり付き 2048px×4枚に分けて保存。0=左上 1=右上 2=左下 3=右下"""
    import numpy as np
    a = np.pad(np.asarray(canvas), ((PAD, PAD), (PAD, PAD), (0, 0)), mode="edge")
    h = HI // 2
    for i, (x, y) in enumerate([(0, 0), (h, 0), (0, h), (h, h)]):
        opt = {"quality": 95, "subsampling": 0} if exact else {"quality": 90}   # exact: 色の段ごとに別の値を入れた画像
        Image.fromarray(a[y:y + h + 2 * PAD, x:x + h + 2 * PAD]).save(folder / f"{stem}_{i}.jpg", optimize=True, **opt)


LAYERS = (("hi", None), ("dt", "D"), ("jp", "J"), ("jl", "L"))
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
                            "Himawari-9 HSD/Satpy TCR; night: IR+lights")
        save_tiles(canvas, dest, "hi")
        del canvas
        flags = ""
        try:
            save_tiles(hsd_detail(files), dest, "dt")
            flags += "D"
        except Exception as e:
            log("dt の作成に失敗（その層なしで配信）:", e)
        try:
            jp, jl = hsd_japan(files)
            save_tiles(jp, dest, "jp")
            flags += "J"
            save_tiles(jl, dest, "jl", exact=True)
            flags += "L"
        except Exception as e:
            log("jp/jl の作成に失敗（その層なしで配信）:", e)
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

    def carry_latest(t, flags, layers=("hi", "dt", "jp", "jl")):
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
