#!/usr/bin/env python3
"""cleveland_nowcast.py — collector nowcast inflasi Cleveland Fed.

Mengambil JSON publik Federal Reserve Bank of Cleveland:
https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json?sc_lang=en
(tanpa key/login; wajib header User-Agent browser — fetcher generik kena 403)
ke data/cleveland_nowcast.json + arsip harian data/cleveland_archive/cleveland_YYYYMMDD.json.

Isi file JSON sumber: list ~161 frame bulanan (2013-07 -> bulan berjalan).
Tiap frame: chart.subcaption = target month (mis. '2026-10'),
categories[0].category = label tanggal harian MM/DD,
dataset 8 seri: CPI / Core CPI / PCE / Core PCE + 4 'Actual ...'.
Nilai = MoM % (0.2421 = 0,24%). File ini point-in-time: tiap tanggal
adalah vintage harian, dan nilai TERAKHIR yang tidak kosong di sebuah
frame adalah final pre-release nowcast untuk bulan target itu.

Yang disimpan (ringkas, bukan raw 7,7 MB): untuk tiap frame + tiap seri
(CPI, Core CPI, PCE, Core PCE): nowcast final + tanggal vintage-nya +
nilai pertama + tanggal pertama + arah pergerakan (naik/turun/stabil,
ambang 0,02 pp). Arsip harian menyimpan hasil parse yang sama.

Aturan main:
- Validasi: JSON harus list; tiap frame harus punya subcaption,
  categories, dan 8 seri bernama (dicari by seriesname, bukan posisi
  index — format FusionCharts ini rapuh). Bila fetch/validasi gagal,
  file last-good dipertahankan dan proses exit 1 (cron melapor).
- Nowcast = MODEL ESTIMATE dari data yang sudah terobservasi, bukan
  konsensus dan bukan actual. Tanggal vintage selalu ikut tampil di
  aplikasi supaya angka tidak dibaca sebagai nilai tetap.
- Verifikasi 10 Nov 2026: frame 2026-10 vintage terakhir 11/05:
  CPI +0,2421%, Core CPI +0,2114%, PCE +0,2652%, Core PCE +0,2565%.

Penggunaan:
    python3 cleveland_nowcast.py            # fetch + simpan
    python3 cleveland_nowcast.py --check    # fetch, validasi, cetak ringkasan
    python3 cleveland_nowcast.py --dry-run  # fetch + validasi saja, tanpa tulis
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
URL = ("https://www.clevelandfed.org/-/media/files/webcharts/"
       "inflationnowcasting/nowcast_month.json?sc_lang=en")
OUT = os.path.join(HERE, "data", "cleveland_nowcast.json")
ARCHIVE_DIR = os.path.join(HERE, "data", "cleveland_archive")
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}

SERIES = ("CPI Inflation", "Core CPI Inflation",
          "PCE Inflation", "Core PCE Inflation")
SERIES_SHORT = {"CPI Inflation": "CPI", "Core CPI Inflation": "Core CPI",
                "PCE Inflation": "PCE", "Core PCE Inflation": "Core PCE"}
# Arah pergerakan jalur nowcast: |akhir - awal| < ambang -> stabil.
DIRECTION_EPS = 0.02  # dalam pp (0,02%)


def fetch(timeout=30):
    req = urllib.request.Request(URL, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f"HTTP {r.status}")
        return json.load(r)


def _resolve_date(target_month, label):
    """'MM/DD' -> ISO date. Label bisa di bulan target atau bulan berikut
    (frame 2026-10 berisi 10/01 -> 11/09: rilis ~10 Nov)."""
    try:
        mm, dd = label.split("/")
        m, d = int(mm), int(dd)
    except (ValueError, AttributeError):
        return None
    y, tm = int(target_month[:4]), int(target_month[5:7])
    if m == tm:
        yy = y
    elif m == tm % 12 + 1:
        yy, m2 = (y + 1, 1) if tm == 12 else (y, tm + 1)
        m = m2
    else:
        return None
    try:
        return f"{yy:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return None


def _parse_series(frame, series_name):
    """(nowcast, vintage_iso, first_value, first_iso, direction) atau None.

    nowcast = nilai terakhir tidak kosong; vintage = tanggalnya."""
    target = frame["chart"].get("subcaption") or ""
    cats = [c.get("label", "") for c in
            frame.get("categories", [{}])[0].get("category", [])]
    ds = next((s for s in frame.get("dataset", [])
               if s.get("seriesname") == series_name), None)
    if ds is None:
        return None
    vals = []
    for i, pt in enumerate(ds.get("data", [])):
        v = (pt or {}).get("value")
        if v in ("", None):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        date = _resolve_date(target, cats[i]) if i < len(cats) else None
        vals.append((date, fv))
    if not vals:
        return None
    (first_date, first_val) = vals[0]
    (last_date, last_val) = vals[-1]
    diff = last_val - first_val
    direction = ("naik" if diff >= DIRECTION_EPS else
                 "turun" if diff <= -DIRECTION_EPS else "stabil")
    return {"nowcast": last_val, "vintage": last_date,
            "first_value": first_val, "first_date": first_date,
            "direction": direction}


def parse(raw):
    """Raw JSON Cleveland -> dict ringkas {fetched_at, frames}.

    frames: {target_month: {series_short: {...}, ...}, ...}.
    Frame tanpa satu pun seri yang ter-parse dilewati (bukan gagal total);
    bila TIDAK ADA frame yang ter-parse -> ValueError."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("JSON bukan list atau kosong")
    frames = {}
    for frame in raw:
        if not isinstance(frame, dict):
            continue
        sub = (frame.get("chart") or {}).get("subcaption") or ""
        try:
            y, m = sub.split("-")
            target = f"{int(y):04d}-{int(m):02d}"
        except (ValueError, AttributeError):
            continue
        entry = {}
        for name in SERIES:
            got = _parse_series(frame, name)
            if got:
                entry[SERIES_SHORT[name]] = got
        if entry:
            frames[target] = entry
    if not frames:
        raise ValueError("tidak ada frame yang bisa di-parse "
                         "(struktur berubah?)")
    return {"fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "Federal Reserve Bank of Cleveland, Inflation Nowcasting",
            "url": URL,
            "frames": frames}


def validate(parsed):
    if not isinstance(parsed, dict) or not parsed.get("frames"):
        raise ValueError("hasil parse kosong")
    return True


def summary(parsed):
    frames = parsed["frames"]
    months = sorted(frames)
    out = [f"frames: {len(months)} ({months[0]} .. {months[-1]})",
           f"fetched_at: {parsed.get('fetched_at')}"]
    for m in months[-2:]:
        parts = []
        for s in ("CPI", "Core CPI", "PCE", "Core PCE"):
            e = frames[m].get(s)
            if e:
                parts.append(f"{s} {e['nowcast']:.2f}% "
                             f"(vintage {e['vintage']}, {e['direction']})")
        out.append(f"{m}: " + " · ".join(parts))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        raw = fetch()
        parsed = parse(raw)
        validate(parsed)
    except Exception as e:  # noqa: BLE001 — laporkan apa adanya
        print(f"cleveland_nowcast: GAGAL fetch/validasi: {e}", file=sys.stderr)
        print("cleveland_nowcast: file last-good dipertahankan.", file=sys.stderr)
        return 1
    if args.dry_run:
        print(summary(parsed))
        print("dry-run: tidak ada file ditulis.")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(parsed, f, ensure_ascii=False)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(os.path.join(ARCHIVE_DIR, f"cleveland_{today}.json"), "w") as f:
        json.dump(parsed, f, ensure_ascii=False)
    print(summary(parsed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
