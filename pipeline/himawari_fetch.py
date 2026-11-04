#!/usr/bin/env python3
"""気象庁のひまわりフルディスク・トゥルーカラー再現画像(TCR)を取得し、
VRChat用に整形して静的サイト(GitHub Pages)向けに書き出す。

- 気象庁のファイル名は時刻(HHMM, UTC)のみで日付がない。24時間で上書きされる。
  そこで HTTP の Last-Modified と照合し「その時刻の観測から90分以内に更新された
  ファイル」だけを今日の観測として採用する。日付を推測で付けることはしない。
- 観測時刻は、ファイル名の HHMM（フルディスク観測の開始時刻・公称）。
- 夜側は暗いまま。過去の昼画像で埋めることはしない。

使い方:  python himawari_fetch.py --out site
依存: Pillow
"""
import argparse
import email.utils
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = "https://www.data.jma.go.jp/mscweb/data/himawari/img/fd_/fd__trm_{hhmm}.jpg"
SOURCE_PAGE = "https://www.data.jma.go.jp/mscweb/data/himawari/list_fd_.html"
UA = "HimawariEarthVRC/1.0 (personal VRChat world; fetches 1 image per 10 min)"

# 601x601 の気象庁画像での地球円盤（海岸線データとの照合で推定、単位: 画素、左上原点、画素中心=整数）
SRC_CX, SRC_CY, SRC_R = 300.30, 288.02, 287.87
SRC_SIZE = 601

# 出力テクスチャの規格（シェーダーと共有。変えるときは両方変える）
OUT = 1024
OUT_CX, OUT_CY, OUT_R = 512.0, 464.0, 448.0   # 上原点の画素座標
LABEL_TOP = 920                                # ここから下がラベル帯（104px）

MAX_AGE_AFTER_OBS = timedelta(minutes=90)      # 観測→更新がこれ以内なら当日分とみなす
SEARCH_BACK_SLOTS = 24                         # 最大4時間さかのぼって探す
KEEP_SLOTS = 3                                 # サイトに残す直近スロット数（キャッシュずれ対策）


def http(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read(8 * 1024 * 1024 + 1) if method == "GET" else b""
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("too large")
        lm = r.headers.get("Last-Modified")
        return body, (email.utils.parsedate_to_datetime(lm) if lm else None)


def floor10(t):
    return t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)


def find_recent_slots(now, want):
    """新しい順に、日付が確定できた観測スロットを最大 want 個返す。"""
    found = []
    t = floor10(now)
    for _ in range(SEARCH_BACK_SLOTS):
        hhmm = t.strftime("%H%M")
        try:
            _, lm = http(BASE.format(hhmm=hhmm), "HEAD")
        except Exception as e:  # 404 等
            print(f"  {hhmm}: HEAD失敗 {e}", file=sys.stderr)
            lm = None
        if lm is not None and timedelta(0) <= lm - t <= MAX_AGE_AFTER_OBS:
            found.append((t, hhmm))
            if len(found) >= want:
                break
        else:
            print(f"  {hhmm}: 未配信または前日分 (Last-Modified={lm})", file=sys.stderr)
        t -= timedelta(minutes=10)
        time.sleep(0.3)
    return found


def font(size):
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def build_texture(raw, obs):
    src = Image.open(BytesIO(raw)).convert("RGB")
    if src.size != (SRC_SIZE, SRC_SIZE):
        raise ValueError(f"想定外の画像サイズ {src.size}（気象庁側の仕様変更の可能性。円盤位置を再推定してください）")
    s = OUT_R / SRC_R
    # 出力(x,y)→入力: x_in = (x - OUT_CX)/s + SRC_CX   （PILは画素中心+0.5の連続座標）
    a, c = 1 / s, (SRC_CX + 0.5) - (OUT_CX) / s
    e, f = 1 / s, (SRC_CY + 0.5) - (OUT_CY) / s
    disk = src.transform((OUT, LABEL_TOP), Image.AFFINE, (a, 0, c, 0, e, f), resample=Image.BICUBIC)
    canvas = Image.new("RGB", (OUT, OUT), (0, 0, 0))
    canvas.paste(disk, (0, 0))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, LABEL_TOP, OUT, OUT), fill=(0, 0, 0))
    jst = obs + timedelta(hours=9)
    line1 = f"OBSERVED {obs:%Y-%m-%d %H:%M} UTC  /  {jst:%Y-%m-%d %H:%M} JST"
    line2 = "Himawari True Color Reproduction  -  JMA / NOAA/NESDIS / CSU/CIRA  (processed)"
    d.text((OUT / 2, LABEL_TOP + 34), line1, fill=(235, 240, 255), font=font(34), anchor="mm")
    d.text((OUT / 2, LABEL_TOP + 80), line2, fill=(150, 160, 185), font=font(20), anchor="mm")
    return canvas


INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>Himawari Earth feed</title>
<body style="background:#000;color:#ccd;font-family:sans-serif;max-width:720px;margin:2em auto">
<h1>Himawari Earth feed (VRChat)</h1>
<p>VRChatワールド用の配信です。最新観測: <b>{obs_utc} UTC / {obs_jst} JST</b>（観測開始の公称時刻）。リアルタイム映像ではありません。</p>
<img src="latest.jpg" width="512" alt="latest">
<h2>出典</h2>
<p>気象庁「ひまわり フルディスク トゥルーカラー再現画像」（{page}）を加工して作成。<br>
JMA / NOAA/NESDIS / CSU/CIRA。TCR imagery: developed by JMA/MSC with the NOAA/NESDIS/STAR GOES-R Algorithm Working Group imagery team; software provided by CIRA.<br>
本サイトは気象庁・テラキャスターとは関係のない個人の制作物です。</p>
</body>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--now", help="テスト用: 現在時刻(ISO, UTC)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    slots = find_recent_slots(now, KEEP_SLOTS)
    if not slots:
        print("確定できる観測が見つかりません。サイトを更新しません。", file=sys.stderr)
        return 2

    out = Path(args.out)
    (out / "slot").mkdir(parents=True, exist_ok=True)
    made = []
    for obs, hhmm in slots:
        raw, lm = http(BASE.format(hhmm=hhmm))
        if lm is None or not (timedelta(0) <= lm - obs <= MAX_AGE_AFTER_OBS):
            print(f"  {hhmm}: 取得中に差し替わった可能性。スキップ", file=sys.stderr)
            continue
        tex = build_texture(raw, obs)
        tex.save(out / "slot" / f"{hhmm}.jpg", quality=92, optimize=True)
        made.append((obs, hhmm))
        time.sleep(0.5)
    if not made:
        return 2

    obs, hhmm = made[0]
    Image.open(out / "slot" / f"{hhmm}.jpg").save(out / "latest.jpg", quality=92)
    processed = datetime.now(timezone.utc)
    # Udon が読む1行テキスト: 形式|観測UTC|スロット|処理UTC
    (out / "latest.txt").write_text(
        f"HIMAWARI1|{obs:%Y-%m-%dT%H:%MZ}|{hhmm}|{processed:%Y-%m-%dT%H:%M:%SZ}\n", encoding="utf-8")
    (out / "latest.json").write_text(json.dumps({
        "observed_utc": f"{obs:%Y-%m-%dT%H:%M:00Z}",
        "observed_note": "full-disk scan start (nominal). Image file name time.",
        "slot": hhmm,
        "slots": [h for _, h in made],
        "processed_utc": processed.isoformat(timespec="seconds"),
        "texture": {"size": OUT, "disk_center_px": [OUT_CX, OUT_CY], "disk_radius_px": OUT_R,
                    "projection_sub_lon_deg": 140.0},
        "source": SOURCE_PAGE,
    }, indent=2), encoding="utf-8")
    jst = obs + timedelta(hours=9)
    (out / "index.html").write_text(INDEX_HTML.format(obs_utc=f"{obs:%Y-%m-%d %H:%M}",
                                    obs_jst=f"{jst:%Y-%m-%d %H:%M}", page=SOURCE_PAGE), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    print(f"OK 観測 {obs:%Y-%m-%d %H:%M} UTC  スロット {[h for _, h in made]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
