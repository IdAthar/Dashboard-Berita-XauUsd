#!/usr/bin/env python3
"""fed_speeches.py — kolektor pidato Federal Reserve Board (gubernur).

Mengambil halaman speeches tahun berjalan:
https://www.federalreserve.gov/newsevents/<YYYY>-speeches.htm
(tanpa key/login; wajib header User-Agent browser)
-> parse entri (tanggal, judul, pembicara, link) -> fetch tiap halaman
pidato (.htm) -> skor kata kunci hawkish/dovish -> tulis
data/fed_speeches.json + arsip harian data/fed_speeches_archive/.

FAKTA SUMBER (terverifikasi 2026-11-10):
- Board site HANYA memuat gubernur (saat ini 8 nama muncul di halaman
  2026: Powell, Warsh, Waller, Cook, Barr, Miran, Jefferson, Bowman —
  kolektor mengambil SIAPA PUN yang muncul di halaman, tidak hardcode
  daftar nama). 12 presiden regional hanya di situs bank masing-masing
  = Fase 2, bukan v1.
- Pidato yang hanya PDF dicatat "teks belum terambil", TIDAK ditebak
  isinya. Tanpa teks = tanpa skor.

SKOR (transparan, bukan LLM — classifier LLM terblokir key berbayar):
- Leksikon hawkish vs dovish dipublikasikan (lihat HAWKISH / DOVISH di
  bawah; cerminnya tampil di app, expander "Cara skor dihitung").
- Hitung frasa BERBEDA yang cocok (presence, bukan kemunculan berulang
  — pidato panjang tidak otomatis lebih ekstrem).
- raw = |hawkish| - |dovish|; skor = clamp(raw * 0,5, -2, +2).
  Jadi selisih bersih 1 frasa = 0,5 poin; >=4 frasa bersih = skor maks.
- Evidence = maks 3 frasa yang cocok (hawkish dulu).
- delta90 = skor - mean skor pembicara yang sama 90 hari SEBELUM pidato
  itu (tanpa pidato itu sendiri); alert bila |delta90| >= 1,0
  (aturan Fed Chirp, dikutip sebagai referensi). Tanpa pidato
  pembanding 90 hari -> delta None, tanpa alert.

Aturan main:
- Incremental: entri yang sudah terkolek dipakai ulang (teks tersimpan
  di JSON, skor dihitung ulang dari teks bila leksikon berubah).
  Entri "gagal ambil" dicoba lagi; PDF-only tetap "teks belum terambil".
- Bila fetch/validasi gagal, file last-good dipertahankan, exit 1.
- TIDAK PERNAH mengarang pidato, tanggal, atau skor.

Verifikasi 10 Nov 2026: pidato Waller "The Economic Outlook and Some
Thoughts on Labor Market Dynamism" (2026-11-10) -> skor +2,0 hawkish,
evidence pertama "50 basis point" (teks asli Fed: "a 50 basis point
hike in the policy rate should be considered" — singular "point",
bukan "points"; SPEC menulis "50 basis points", selisih satu huruf
dari teks asli, dicatat di sini apa adanya).

Penggunaan:
    python3 fed_speeches.py            # fetch + simpan
    python3 fed_speeches.py --check    # verifikasi live pidato Waller
                                       # 10 Nov + ringkasan, tanpa tulis
    python3 fed_speeches.py --dry-run  # fetch + validasi saja, tanpa tulis
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.federalreserve.gov"
OUT = os.path.join(HERE, "data", "fed_speeches.json")
ARCHIVE_DIR = os.path.join(HERE, "data", "fed_speeches_archive")
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
LEXICON_VERSION = 1
ALERT_THRESHOLD = 1.0  # |delta90| >= 1,0 (aturan Fed Chirp, referensi)

# Leksikon — urutan = urutan tampil evidence (paling spesifik dulu).
# "50 basis point" (singular) adalah wording asli teks Fed.
HAWKISH = [
    "50 basis point",
    "credibility at risk",
    "raise the policy rate",
    "raise interest rates",
    "rate hike",
    "hiking",
    "further tightening",
    "tighten monetary policy",
    "restrictive",
    "inflation remains elevated",
    "inflation is too high",
    "inflation has been too high",
    "inflation too high",
    "above our 2 percent goal",
    "above the 2 percent goal",
    "more work to do",
    "upside risks to inflation",
    "persistent inflation",
    "stronger policy move",
    "additional rate increases",
    "further rate increases",
    "rates need to rise",
]
DOVISH = [
    "cut the policy rate",
    "cut interest rates",
    "rate cut",
    "lower interest rates",
    "lower the policy rate",
    "ease monetary policy",
    "easing",
    "accommodative",
    "signs of disinflation",
    "disinflation",
    "inflation is falling",
    "inflation has fallen",
    "inflation moderating",
    "inflation has moderated",
    "avoid overtightening",
    "risk of overtightening",
    "risking a recession",
    "hold rates steady",
    "keep rates unchanged",
    "pause",
    "downside risks to growth",
    "risks to employment",
    "labor market softening",
    "weakness in the labor market",
    "unemployment rising",
    "patient approach",
    "be patient",
    "gradual normalization",
]

_ROLE_PREFIXES = ("Vice Chair for Supervision ", "Vice Chair ",
                  "Governor ", "Chairman ", "Chair ")


def index_url(year=None):
    y = year or datetime.now(timezone.utc).year
    return f"{BASE}/newsevents/{y}-speeches.htm"


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError(f"HTTP {r.status} untuk {url}")
        return r.read().decode("utf-8", "replace")


def _speaker_short(full):
    """'Governor Christopher J. Waller' -> 'Waller'."""
    name = (full or "").strip()
    for p in _ROLE_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    parts = [p for p in name.replace(".", " ").split() if p]
    return parts[-1] if parts else name


def parse_index(html):
    """Halaman index -> [{date, title, speaker_full, speaker, url, kind}].

    Tiap entri: <time>MM/DD/YYYY</time> lalu link judul
    <a href="/newsevents/speech/XXX.htm"><em>Judul</em></a> lalu
    <p class="news__speaker">Nama</p>. Link .pdf -> kind 'pdf'.
    Duplikat URL dibuang (entri ganda menunjuk halaman yang sama)."""
    entries = []
    seen = set()
    for m in re.finditer(r"<time>(\d{1,2})/(\d{1,2})/(\d{4})</time>", html):
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date = f"{yy:04d}-{mm:02d}-{dd:02d}"
        except ValueError:
            continue
        window = html[m.end():m.end() + 1500]
        lm = re.search(
            r'<a href="([^"]+)"><em>([^<]+)</em></a>', window)
        if not lm:
            continue
        href, title = lm.group(1), lm.group(2).strip()
        if href in seen:
            continue
        seen.add(href)
        sm = re.search(r'class="news__speaker">([^<]+)</p>', window)
        speaker_full = sm.group(1).strip() if sm else ""
        kind = "pdf" if href.lower().endswith(".pdf") else "htm"
        url = href if href.startswith("http") else BASE + href
        entries.append({"date": date, "title": title,
                        "speaker_full": speaker_full,
                        "speaker": _speaker_short(speaker_full),
                        "url": url, "kind": kind})
    if not entries:
        raise ValueError("tidak ada entri pidato yang bisa di-parse "
                         "(struktur halaman berubah?)")
    return entries


def extract_text(html):
    """Teks pidato dari halaman .htm -> (teks, mode).

    mode 'main': isi <div class="col-xs-12 col-sm-8 col-md-8"> setelah
    heading (tubuh pidato, tanpa navigasi/footer). 'fallback': seluruh
    halaman (bila struktur berubah)."""
    seg = html
    mode = "fallback"
    hd = html.find('<div class="heading col-xs-12 col-sm-8 col-md-8"')
    bs = html.find('<div class="col-xs-12 col-sm-8 col-md-8"',
                   hd if hd >= 0 else 0)
    if bs >= 0:
        gt = html.find(">", bs) + 1
        end = html.find('<div class="panel panel-attachments', gt)
        if end < 0:
            end = html.find('<div class="row footer', gt)
        if end > gt:
            seg = html[gt:end]
            mode = "main"
    txt = re.sub(r"<script.*?</script>", " ", seg, flags=re.S | re.I)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    import html as _html
    txt = _html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt, mode


def score_text(text):
    """(skor, hawk_hits, dove_hits, evidence) dari teks pidato."""
    low = " " + (text or "").lower() + " "
    hawk = [p for p in HAWKISH
            if re.search(r"\b" + re.escape(p) + r"\b", low)]
    dove = [p for p in DOVISH
            if re.search(r"\b" + re.escape(p) + r"\b", low)]
    raw = len(hawk) - len(dove)
    score = max(-2.0, min(2.0, raw * 0.5))
    evidence = (hawk + dove)[:3]
    return score, hawk, dove, evidence


def _label(score):
    if score is None:
        return "belum diskoring"
    if score >= 1.0:
        return "hawkish"
    if score <= -1.0:
        return "dovish"
    if score > 0:
        return "condong hawkish"
    if score < 0:
        return "condong dovish"
    return "netral"


def enrich(records):
    """Hitung skor (bila ada teks), delta90 + alert, ringkasan pembicara.

    records: list dict entri (dengan 'text' bila tersedia).
    Mengembalikan (speeches_desc, speakers)."""
    for r in records:
        text = r.get("text") or ""
        if r.get("text_status") == "ok" and text:
            score, hawk, dove, ev = score_text(text)
            r["score"] = score
            r["hawk_hits"] = hawk
            r["dove_hits"] = dove
            r["evidence"] = ev
            r["label"] = _label(score)
        else:
            r["score"] = None
            r["hawk_hits"] = []
            r["dove_hits"] = []
            r["evidence"] = []
            r["label"] = "belum diskoring"
    # delta90: mean 90 hari SEBELUM pidato itu (tanpa dirinya).
    asc = sorted((r for r in records if r.get("score") is not None),
                 key=lambda r: (r["date"], r["url"]))
    for r in asc:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        lo = d - timedelta(days=90)
        prior = [p["score"] for p in asc
                 if p["speaker"] == r["speaker"] and p is not r
                 and lo <= datetime.strptime(p["date"], "%Y-%m-%d").date() < d]
        if prior:
            mean_prior = sum(prior) / len(prior)
            r["delta90"] = round(r["score"] - mean_prior, 2)
            r["alert"] = abs(r["delta90"]) >= ALERT_THRESHOLD
        else:
            r["delta90"] = None
            r["alert"] = False
    # Ringkasan per pembicara vs tanggal pidato terbaru di dataset.
    scored = [r for r in asc]
    speakers = {}
    if scored:
        ref = max(datetime.strptime(r["date"], "%Y-%m-%d").date()
                  for r in scored)
        lo = ref - timedelta(days=90)
        for r in scored:
            sp = r["speaker"]
            speakers.setdefault(sp, {"speaker": sp,
                                     "speaker_full": r.get("speaker_full", ""),
                                     "scores90": [], "latest": None})
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            if d > lo:
                speakers[sp]["scores90"].append(r["score"])
            cur = speakers[sp]["latest"]
            if cur is None or r["date"] > cur["date"]:
                speakers[sp]["latest"] = {"date": r["date"],
                                          "title": r["title"],
                                          "score": r["score"]}
        for sp, s in speakers.items():
            vals = s.pop("scores90")
            s["n90"] = len(vals)
            s["mean90"] = (round(sum(vals) / len(vals), 2)
                           if vals else None)
    speeches = sorted(records, key=lambda r: (r["date"], r["url"]),
                      reverse=True)
    return speeches, speakers


def collect(existing=None):
    """Fetch index + pidato baru -> dict siap tulis."""
    old = {}
    if existing:
        for r in existing.get("speeches", []):
            old[r.get("url")] = r
    index_html = fetch(index_url())
    entries = parse_index(index_html)
    records = []
    for e in entries:
        prev = old.get(e["url"])
        if e["kind"] == "pdf":
            rec = dict(e)
            rec["text_status"] = "teks belum terambil"
            rec["text"] = ""
            rec["extract"] = None
            records.append(rec)
            continue
        if prev and prev.get("text_status") == "ok" and prev.get("text"):
            rec = dict(prev)
            rec.update({k: e[k] for k in
                        ("date", "title", "speaker_full", "speaker", "url",
                         "kind")})
            records.append(rec)
            continue
        try:
            page = fetch(e["url"])
            text, mode = extract_text(page)
            if len(text) < 200:
                raise ValueError("teks hasil ekstrak terlalu pendek "
                                 f"({len(text)} karakter)")
            rec = dict(e)
            rec["text_status"] = "ok"
            rec["text"] = text
            rec["extract"] = mode
            records.append(rec)
        except Exception as err:  # noqa: BLE001 — satu pidato gagal
            rec = dict(e)        # tidak menggagalkan koleksi
            rec["text_status"] = "gagal ambil"
            rec["text"] = ""
            rec["extract"] = None
            rec["fetch_error"] = str(err)
            records.append(rec)
        time.sleep(0.2)
    speeches, speakers = enrich(records)
    return {"fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": ("Board of Governors of the Federal Reserve System"
                       " — Speeches"),
            "index_url": index_url(),
            "lexicon_version": LEXICON_VERSION,
            "note_skor": ("Skor kata kunci transparan, bukan penilaian "
                          "LLM. Tanpa teks = tanpa skor."),
            "speeches": speeches,
            "speakers": speakers}


def validate(parsed):
    if not isinstance(parsed, dict) or not parsed.get("speeches"):
        raise ValueError("hasil parse kosong")
    return True


def summary(parsed):
    sp = parsed["speeches"]
    scored = [s for s in sp if s.get("score") is not None]
    untext = [s for s in sp if s.get("text_status") != "ok"]
    out = [f"pidato: {len(sp)} ({len(scored)} terskor, "
           f"{len(untext)} tanpa teks)",
           f"fetched_at: {parsed.get('fetched_at')}",
           f"pembicara: {len(parsed.get('speakers', {}))}"]
    for s in sp[:5]:
        sc = s.get("score")
        sc_txt = f"{sc:+.1f} {s.get('label')}" if sc is not None else "—"
        out.append(f"  {s['date']} {s['speaker']}: {sc_txt} — "
                   f"{s['title'][:60]}")
    return "\n".join(out)


def check_waller():
    """Verifikasi live: pidato Waller 10 Nov 2026 harus hawkish dengan
    frasa bukti '50 basis point' (wording asli Fed; SPEC menulis
    '50 basis points'). Return (ok, pesan)."""
    entries = parse_index(fetch(index_url()))
    target = [e for e in entries
              if e["date"] == "2026-11-10"
              and e["speaker"] == "Waller"]
    if not target:
        return False, "pidato Waller 2026-11-10 tidak ditemukan di index"
    e = target[0]
    text, mode = extract_text(fetch(e["url"]))
    score, hawk, dove, ev = score_text(text)
    has_50bp = any(re.search(r"50 basis points?", p) for p in hawk)
    msg = (f"Waller 2026-11-10: '{e['title']}' skor {score:+.1f} "
           f"({_label(score)}), hawk={hawk}, dove={dove}, "
           f"evidence={ev}, extract={mode}")
    if score <= 0:
        return False, msg + " — GAGAL: skor tidak hawkish"
    if not has_50bp:
        return False, msg + " — GAGAL: frasa '50 basis point' tidak ada"
    return True, msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.check:
        try:
            ok, msg = check_waller()
        except Exception as e:  # noqa: BLE001
            print(f"fed_speeches --check GAGAL: {e}", file=sys.stderr)
            return 1
        print(msg)
        print("check:", "OK" if ok else "GAGAL")
        return 0 if ok else 1
    existing = None
    try:
        with open(OUT) as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = None
    try:
        parsed = collect(existing)
        validate(parsed)
    except Exception as e:  # noqa: BLE001 — laporkan apa adanya
        print(f"fed_speeches: GAGAL fetch/validasi: {e}", file=sys.stderr)
        print("fed_speeches: file last-good dipertahankan.", file=sys.stderr)
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
    with open(os.path.join(ARCHIVE_DIR,
                           f"fed_speeches_{today}.json"), "w") as f:
        json.dump(parsed, f, ensure_ascii=False)
    print(summary(parsed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
