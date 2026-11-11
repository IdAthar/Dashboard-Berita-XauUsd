#!/usr/bin/env python3
"""
Dashboard Berita XAUUSD — versi Streamlit.

Port dari news_dashboard.py (logika kartu, konversi UTC->WITA, filter impact,
kolom Forecast/Previous/Actual, box respon tipikal dari DB) ke Streamlit,
dengan dua peningkatan:
  1. Actual berwarna SEMANTIK: hijau = kabar baik untuk ekonomi AS,
     putih = sesuai ekspektasi, merah = kabar buruk. Logika DIBALIK untuk
     Unemployment Claims (angka lebih rendah = hijau) dan data inflasi
     (lebih panas dari forecast = merah).
  2. Pengisi minggu depan via Finnhub (opsional): bila st.secrets punya
     FINNHUB_API_KEY, 2 minggu ke depan diambil dari Finnhub economic
     calendar. Tanpa key -> catatan jujur, TIDAK mengarang data.

Data:
  - Feed ForexFactory XML (minggu berjalan, publik, tanpa key)
  - news_events.db (actual terverifikasi + respon tipikal GC=F) — file DB
    ikut di-push ke repo Streamlit Cloud.

Jalankan lokal :  streamlit run streamlit_app.py
Deploy         :  push streamlit_app.py + requirements.txt + news_events.db
                  ke GitHub, hubungkan ke Streamlit Community Cloud.
                  Tambahkan FINNHUB_API_KEY di Settings -> Secrets bila ada.

Hanya butuh paket `streamlit`; sisanya stdlib.

Tambahan 7 Nov 2026:
  - Seksi "Minggu Depan — konsensus WSJ" (week_ahead.json, ditulis manual
    seminggu sekali dari WSJ Data Week Ahead) + override forecast kartu
    minggu depan dengan median WSJ. y/y tidak di-override (metrik beda).
  - Tiga dialog analisa: Harian (event hari ini + pengingat snapshot kolam),
    Mingguan (sudah rilis minggu ini + tabel WSJ minggu depan),
    Bulanan (minggu demi minggu + alasan mingguan dari monthly_notes.json).
  - db_title_for disinkronkan dengan news_dashboard.py: CPI headline vs
    core dibedakan, ditambah ISM, S&P PMI, UMich, JOLTS, Earnings, PCE, GDP.

Tambahan 9 Nov 2026 (Analisa Fundamental v2, SPEC_Analisa_Fundamental_v2.md):
  - Tiga dialog ditulis ulang jadi analisa: pra-rilis (konsensus, skenario
    kanal USD) + pasca-rilis (actual vs forecast, respon ±1 jam terukur)
    + narasi makro dari weekly_narrative.json + bacaan gabungan
    (pro-USD vs kontra-USD, kejutan terbesar via z-score).
  - Tier kekuatan sinyal dari DB (db_strength): BERNILAI TINGGI bila N>=2
    dan mean|ret60| >= 0,40% — diukur dari BESARAN gerak, bukan arah
    (arah belum bisa diklaim dengan N sekecil ini); N selalu ditampilkan.
  - Hari tanpa event menampilkan preview event berikut, bukan kosong.
"""

# Cross-check xaudaily (9 Nov 2026): dialog Mingguan menampilkan kotak
# "Cross-check xaudaily" (tick COMEX, odds FOMC Polymarket, NFP/UMich vs
# DB, CPI y/y + estimasi model berlabel belum tervalidasi); penutup
# Bulanan menampilkan SPDR + arus bank sentral. Nilai xaudaily = revisi
# terbaru; FRED vintage tetap otoritas first print.

# Nowcast Cleveland Fed v3 (10 Nov 2026): dialog Harian menampilkan kotak
# "Nowcast Cleveland Fed" pra-rilis untuk CPI/PCE (nowcast + vintage +
# selisih vs konsensus WSJ dalam bp + jalur nowcast + 3 skenario) dan
# perbandingan tiga arah pasca-rilis (actual vs WSJ vs nowcast final).
# Nowcast = model estimate (bukan konsensus, bukan actual); WSJ tetap
# otoritas konsensus, FRED tetap otoritas actual first-print.

# Fed Speaker Tracker v1 (10 Nov 2026): tab baru "Suara Fed" — kolektor
# fed_speeches.py (pidato Board of Governors, federalreserve.gov) + skor
# kata kunci transparan -2..+2 (bukan LLM) + delta vs mean 90h + alert
# |delta| >= 1,0. Hanya gubernur; presiden regional = Fase 2.
import html
import json
import os
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import streamlit as st

# ----------------------------------------------------------------------------
# Konfigurasi halaman (harus jadi perintah Streamlit pertama)
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="📰 News XAUUSD — Kalender & Dampak",
    page_icon="📰",
    layout="centered",
)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "news_events.db")
LOCAL_CAL_PATH = os.path.join(HERE, "news_calendar.json")
WEEK_AHEAD_PATH = os.path.join(HERE, "week_ahead.json")
MONTHLY_NOTES_PATH = os.path.join(HERE, "monthly_notes.json")
WEEKLY_NARRATIVE_PATH = os.path.join(HERE, "weekly_narrative.json")
# Cross-check xaudaily.com (CC BY 4.0 — atribusi wajib di mana pun
# datanya tampil). ?src= dipertahankan (syarat kanal mereka).
# Nilai xaudaily = REVISI terbaru, bukan first print — FRED vintage
# tetap otoritas first print di DB; beda = revisi, bukan kesalahan.
XAUDAILY_URL = "https://xaudaily.com/readings.en.json?src=muse-newsapp"
XAUDAILY_PATH = os.path.join(HERE, "data", "xaudaily_readings.json")
# Nowcast Cleveland Fed (tanpa key/login; wajib User-Agent browser —
# fetcher generik kena 403). File JSON sumber ~7,7 MB, 161 frame bulanan;
# kolektor cleveland_nowcast.py menyimpan hasil parse ringkasnya.
CLEVELAND_URL = ("https://www.clevelandfed.org/-/media/files/webcharts/"
                 "inflationnowcasting/nowcast_month.json?sc_lang=en")
CLEVELAND_PATH = os.path.join(HERE, "data", "cleveland_nowcast.json")
CLEVELAND_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# Fed Speaker Tracker v1 (10 Nov 2026): pidato Board of Governors
# (federalreserve.gov, teks penuh). Kolektor fed_speeches.py menulis
# data/fed_speeches.json; app memuatnya live-first dari raw GitHub
# (hasil push cron 6 jam) lalu fallback file bundel. Skor = kata kunci
# transparan, BUKAN penilaian LLM. Hanya gubernur; 12 presiden
# regional belum masuk (Fase 2).
FED_SPEECHES_RAW_URL = ("https://raw.githubusercontent.com/IdAthar/"
                        "Dashboard-Berita-XauUsd/main/data/fed_speeches.json")
FED_SPEECHES_PATH = os.path.join(HERE, "data", "fed_speeches.json")

WITA = timezone(timedelta(hours=8))  # GMT+8

HARI = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
         "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://nfs.faireconomy.com/ff_calendar_thisweek.xml",
]
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"

CSS = """
<style>
.stApp { background: #0b0e14; color: #e6e9f0; }
.block-container { max-width: 720px; padding-top: 1.2rem; }
.ev-card { background:#131722; border:1px solid #1f2937; border-radius:12px;
           padding:14px; margin-bottom:12px; }
.ev-top { display:flex; justify-content:space-between; align-items:center;
          margin-bottom:8px; }
.badge { font-size:11px; font-weight:700; padding:3px 10px; border-radius:20px;
         letter-spacing:.5px; }
.badge.high { background:#3f1d1d; color:#f87171; border:1px solid #ef4444; }
.badge.med { background:#3a2a08; color:#fbbf24; border:1px solid #f59e0b; }
.badge.low { background:#1a1f2b; color:#8b93a7; border:1px solid #374151; }
.badge.src { background:#0f1420; color:#8b93a7; border:1px solid #1f2937;
             margin-left:6px; }
.rel { font-size:12px; color:#8b93a7; }
.rel.future { color:#60a5fa; font-weight:600; }
.rel.fresh { color:#22c55e; font-weight:700; }
.prebrief { background:#0e1626; border:1px dashed #2b3b55; border-radius:10px;
            padding:10px 12px; margin-top:10px; font-size:13px; }
.prebrief div { margin-top:4px; }
.netfx { background:#141b0e; border:1px solid #3f6212; border-radius:12px;
         padding:12px 14px; margin:14px 0 12px 0; font-size:13px; }
.netfx .muted { margin-top:6px; display:block; }
.devrow { background:#0e1626; border-left:3px solid #60a5fa; border-radius:0 10px 10px 0;
          padding:8px 12px; margin-top:10px; font-size:13px; }
.ev-title { font-size:16px; font-weight:700; margin-bottom:2px; }
.ev-time { font-size:12.5px; color:#8b93a7; margin-bottom:10px; }
.fpa { border-top:1px solid #1f2937; padding-top:8px; }
.frow { display:flex; justify-content:space-between; font-size:13.5px; padding:3px 0; }
.frow span { color:#8b93a7; }
.muted { color:#5b6478; font-size:13px; }
.good { color:#22c55e; } .bad { color:#ef4444; } .neutral { color:#e6e9f0; }
.vlabel { font-size:12px; }
.typ { margin-top:10px; background:#0f1420; border:1px solid #1f2937;
       border-radius:8px; padding:10px; font-size:13px; line-height:1.6; }
.typ .up { color:#22c55e; } .typ .down { color:#ef4444; }
.typnote { font-size:11.5px; color:#5b6478; margin-top:2px; }
.caveat { color:#fbbf24; }
.nextbox { background:#0f1420; border:1px solid #1d4ed8; border-radius:12px;
           padding:14px; margin:12px 0; }
.nextbox .cd { font-size:30px; font-weight:800; color:#fbbf24; }
.foot { color:#5b6478; font-size:11.5px; margin:20px 0 30px; text-align:center;
        line-height:1.7; }
.stTabs [data-baseweb="tab-list"] { gap: 8px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Helper format (diport dari news_dashboard.py)
# ----------------------------------------------------------------------------
_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def esc(s):
    return html.escape(s or "", quote=True)


def parse_utc(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fmt_dt_wita(dt_utc):
    w = dt_utc.astimezone(WITA)
    return f"{HARI[w.weekday()]}, {w.day} {BULAN[w.month-1]} {w.year} · {w.strftime('%H:%M')} WITA"


def rel_time(dt_utc, now_utc):
    secs = int((dt_utc - now_utc).total_seconds())
    if secs >= 0:
        if secs < 3600:
            return f"{max(secs // 60, 1)} menit lagi"
        if secs < 86400:
            return f"{secs // 3600} jam lagi"
        return f"{secs // 86400} hari lagi"
    ago = -secs
    if ago < 3600:
        return "baru saja rilis"
    if ago < 86400:
        return f"{ago // 3600} jam lalu"
    if ago < 172800:
        return "kemarin"
    return f"{ago // 86400} hari lalu"


def parse_num(raw):
    if not raw:
        return None
    m = _NUM_RE.search(str(raw).replace(",", ""))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def fmt_actual(actual_num, forecast_raw):
    """Format angka actual mengikuti satuan forecast ('0.2%' -> '0.3%')."""
    if actual_num is None:
        return "-"
    suffix = ""
    if forecast_raw:
        m = _NUM_RE.search(str(forecast_raw).replace(",", ""))
        if m:
            suffix = str(forecast_raw)[m.end():].strip()
    a = int(actual_num) if float(actual_num).is_integer() else round(actual_num, 2)
    return f"{a}{suffix}"


def is_speech_title(t):
    tl = (t or "").lower()
    return any(k in tl for k in ("speak", "speech", "testimony", "press conference"))


def speech_speaker(title):
    """Ambil nama pembicara dari judul pidato Fed.

    'Fed Chairman Warsh Speaks' -> 'Warsh', 'FOMC Member Waller Speaks' -> 'Waller'.
    None = bukan pidato."""
    t = title or ""
    if not is_speech_title(t):
        return None
    m = re.search(r"(?:chairman|chair|member|governor|president|vice chair)\s+([A-Za-z'\-]+)",
                  t, re.IGNORECASE)
    if m:
        return m.group(1).capitalize()
    return "Fed"


def db_title_for(cal_title):
    """Petakan judul kalender -> judul kanonis di DB event-study.

    Disinkronkan dengan news_dashboard.py (7 Nov 2026): CPI headline
    dan core DIBEDAKAN — jangan samakan, actual-nya beda."""
    t = (cal_title or "").lower()
    sp = speech_speaker(cal_title)
    if sp:
        return f"Speech: {sp}"
    if "non-farm" in t or "nonfarm" in t or "non farm payroll" in t or t.strip().startswith("nfp"):
        return "NFP"
    if "core ppi" in t:
        return "PPI (core m/m)"
    if "core cpi" in t:
        return "CPI (core m/m)"
    if "cpi" in t:
        return "CPI (headline m/m)"
    if "ppi" in t:
        return "PPI (final demand m/m)"
    if "core retail" in t:
        return "Retail Sales (core m/m)"
    if "retail sales" in t:
        return "Retail Sales (m/m)"
    if "ism manufacturing" in t:
        return "ISM Manufacturing PMI"
    if "ism services" in t:
        return "ISM Services PMI"
    if "flash manufacturing" in t:
        return "S&P Global Manufacturing PMI"
    if "flash services" in t:
        return "S&P Global Services PMI"
    if "uom consumer sentiment" in t:
        return "UMich Consumer Sentiment"
    if "philly fed" in t:
        return "Philly Fed Manufacturing"
    if "unemployment rate" in t:
        return "Unemployment Rate"
    if "average hourly earnings" in t:
        return "Average Hourly Earnings m/m"
    if "jolts" in t:
        return "JOLTS Job Openings"
    if "core pce" in t:
        return "Core PCE (m/m)"
    if "pce" in t:
        return "PCE (m/m)"
    if "gdp" in t:
        return "GDP (advance q/q)"
    if "claim" in t:
        return "Unemployment Claims"
    return None


def econ_verdict(dbt, actual_num, forecast_num):
    """Verdict SEMANTIK untuk ekonomi AS.

    hijau (good)    = kabar baik untuk ekonomi AS
    putih (neutral) = sesuai ekspektasi
    merah (bad)     = kabar buruk untuk ekonomi AS
    Logika DIBALIK untuk Unemployment Claims (lebih rendah = hijau)
    dan data inflasi (lebih panas dari forecast = merah).
    """
    if actual_num is None or forecast_num is None:
        return "none", ""
    t = (dbt or "").lower()
    is_claims = "claim" in t
    is_inflation = ("cpi" in t) or ("ppi" in t)
    if actual_num == forecast_num:
        return "neutral", "= sesuai ekspektasi"
    hotter = actual_num > forecast_num
    arah = "di atas forecast ▲" if hotter else "di bawah forecast ▼"
    if is_claims:
        good = not hotter          # klaim turun = kabar baik
    elif is_inflation:
        good = not hotter          # inflasi panas = kabar buruk
    else:
        good = hotter              # indikator aktivitas: naik = kabar baik
    if good:
        return "good", f"✓ kabar baik untuk ekonomi AS ({arah})"
    return "bad", f"✗ kabar buruk untuk ekonomi AS ({arah})"


# ----------------------------------------------------------------------------
# Data: feed ForexFactory (minggu berjalan)
# ----------------------------------------------------------------------------
def fetch_feed():
    last_err = None
    for url in FEED_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read(), url
        except Exception as e:  # noqa: BLE001 - coba URL berikutnya
            last_err = e
    raise RuntimeError(f"Semua feed gagal: {last_err}")


def parse_number(raw):
    if not raw:
        return None
    m = _NUM_RE.search(str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# Event Low-impact FF yang tetap relevan untuk XAUUSD (disorot di dossier
# mingguan) — tetap ditampilkan dengan badge Low apa adanya, tidak di-upgrade.
GOLD_LOW_ALLOW = ("nahb", "housing start", "building permit", "pending home",
                  "adp", "case-shiller", "new home sales", "existing home sales",
                  "pce", "payroll")


def parse_event(ev):
    g = lambda t: (ev.findtext(t) or "").strip()  # noqa: E731
    country = g("country")
    impact = g("impact")
    if country != "USD":
        return None
    title = g("title")
    if impact not in ("High", "Medium"):
        if not (impact == "Low" and any(k in title.lower() for k in GOLD_LOW_ALLOW)):
            return None
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", g("date"))
    if not m:
        return None
    mon, day, yr = map(int, m.groups())
    # Waktu di feed ini adalah UTC (terverifikasi) — langsung pakai sebagai UTC.
    tm = re.match(r"(\d+):(\d+)(am|pm)", g("time").lower())
    if tm:
        hh, mm, ap = int(tm.group(1)), int(tm.group(2)), tm.group(3)
        hh = hh % 12 + (12 if ap == "pm" else 0)
        ev_utc = datetime(yr, mon, day, hh, mm, tzinfo=timezone.utc)
        utc_iso = ev_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        utc_iso = None
    forecast_raw, previous_raw = g("forecast"), g("previous")
    return {
        "title": title,
        "country": "USD",
        "impact": impact,
        "event_time_utc": utc_iso,
        "forecast": forecast_raw or None,
        "previous": previous_raw or None,
        "src": "FF",
    }


@st.cache_data(ttl=300)
def load_ff_events():
    """(events, source_note). Live fetch; fallback ke news_calendar.json lokal."""
    try:
        raw, used_url = fetch_feed()
        root = ET.fromstring(raw.decode("windows-1252", "replace"))
        events = []
        for ev in root.findall("event"):
            p = parse_event(ev)
            if p:
                events.append(p)
        events.sort(key=lambda e: (e["event_time_utc"] is None, e["event_time_utc"] or ""))
        return events, f"Feed ForexFactory (live) · {used_url}"
    except Exception as e:  # noqa: BLE001 - fallback file lokal
        try:
            with open(LOCAL_CAL_PATH) as f:
                cal = json.load(f)
            events = []
            for ev in cal.get("events", []):
                events.append({
                    "title": ev.get("title"),
                    "country": "USD",
                    "impact": ev.get("impact") or "Medium",
                    "event_time_utc": ev.get("event_time_utc"),
                    "forecast": ev.get("forecast"),
                    "previous": ev.get("previous"),
                    "src": "FF",
                })
            return events, f"Arsip lokal ({cal.get('fetched_at', '?')}) — feed live gagal: {e}"
        except (OSError, json.JSONDecodeError):
            return [], f"Feed gagal dan arsip lokal tidak ada: {e}"


# ----------------------------------------------------------------------------
# Data: Finnhub (opsional, untuk mengisi minggu depan)
# ----------------------------------------------------------------------------
def finnhub_key():
    try:
        return st.secrets.get("FINNHUB_API_KEY") or None
    except Exception:  # noqa: BLE001 - tidak ada secrets.toml
        return None


def parse_finnhub_time(raw):
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


@st.cache_data(ttl=600)
def load_finnhub_events(key):
    """2 minggu ke depan dari Finnhub. None = key tidak dikonfigurasi,
    [] = gagal / tidak ada event. TIDAK PERNAH mengarang data."""
    if not key:
        return None
    today = datetime.now(timezone.utc).date()
    frm, to = today.isoformat(), (today + timedelta(days=14)).isoformat()
    url = f"{FINNHUB_URL}?from={frm}&to={to}&token={key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in payload.get("economicCalendar", []):
        if (it.get("country") or "").upper() != "US":
            continue
        impact = (it.get("impact") or "").lower()
        if impact not in ("high", "medium"):
            continue
        utc_iso = parse_finnhub_time(it.get("time"))
        if not utc_iso:
            continue
        unit = (it.get("unit") or "").strip()
        est, prev = it.get("estimate"), it.get("prev")
        out.append({
            "title": (it.get("event") or "?").strip(),
            "country": "USD",
            "impact": impact.capitalize(),
            "event_time_utc": utc_iso,
            "forecast": (f"{est}{unit}" if est is not None else None),
            "previous": (f"{prev}{unit}" if prev is not None else None),
            "src": "Finnhub",
        })
    out.sort(key=lambda e: e["event_time_utc"])
    return out


TV_CAL_URL = "https://economic-calendar.tradingview.com/events"


@st.cache_data(ttl=1800)
def load_tradingview_events(days_ahead=35):
    """Minggu-minggu depan dari TradingView Economic Calendar.

    Endpoint publik (dipakai widget TradingView sendiri), tanpa API key —
    hanya butuh header Origin. [] = gagal / tidak ada event.
    TIDAK PERNAH mengarang data."""
    today = datetime.now(timezone.utc)
    frm = today.strftime("%Y-%m-%dT00:00:00.000Z")
    to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%dT00:00:00.000Z")
    url = f"{TV_CAL_URL}?from={frm}&to={to}&countries=US"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.tradingview.com",
        })
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in payload.get("result", []):
        if (it.get("country") or "").upper() != "US":
            continue
        imp = it.get("importance")
        if imp == 1:
            impact = "High"
        elif imp == 0:
            impact = "Medium"
        else:
            continue  # -1 / None = low, skip
        title = (it.get("title") or "?").strip()
        tl = title.lower()
        # TV memberi importance 0 ke sebagian event besar — koreksi eksplisit
        if any(k in tl for k in ("cpi", "fomc", "federal funds", "non farm payrolls")):
            impact = "High"
        try:
            dt = datetime.fromisoformat(str(it.get("date") or "").replace("Z", "+00:00"))
            utc_iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            continue

        def fmt_num(v):
            if v is None:
                return None
            try:
                f = float(v)
                return str(int(f)) if f.is_integer() else str(round(f, 2))
            except (TypeError, ValueError):
                return str(v)

        out.append({
            "title": title,
            "country": "USD",
            "impact": impact,
            "event_time_utc": utc_iso,
            "forecast": fmt_num(it.get("forecast")),
            "previous": fmt_num(it.get("previous")),
            "src": "TV",
        })
    out.sort(key=lambda e: e["event_time_utc"])
    return out


# ----------------------------------------------------------------------------
# Data: DB event-study (actual terverifikasi + respon tipikal)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=300)
def db_actuals():
    """{(db_title, tanggal_utc): actual}."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        for title, eut, actual in con.execute(
                "SELECT title, event_time_utc, actual FROM events WHERE actual IS NOT NULL"):
            try:
                d = datetime.strptime(eut, "%Y-%m-%dT%H:%M:%SZ").date().isoformat()
            except (ValueError, TypeError):
                continue
            out[(title, d)] = actual
        con.close()
    except sqlite3.Error:
        pass
    return out


@st.cache_data(ttl=300)
def db_typical():
    """{db_title: (N, avg_ret_pct)} untuk jendela m60_p60, simbol GC=F."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """SELECT e.title, r.ret_pct FROM responses r
               JOIN events e ON e.id = r.event_id
               WHERE r.window_label = 'm60_p60' AND r.symbol = 'GC=F'""").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    by_title = {}
    for title, ret in rows:
        by_title.setdefault(title, []).append(ret)
    for title, rets in by_title.items():
        out[title] = (len(rets), sum(rets) / len(rets))
    return out


@st.cache_data(ttl=300)
def db_surprises():
    """{db_title: [(actual, forecast), ...]} — max 5 rilis terakhir per metrik,
    untuk tren surprise pra-rilis. Angka mentah apa adanya."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT title, actual, forecast, event_time_utc FROM events"
            " WHERE actual IS NOT NULL AND forecast IS NOT NULL"
            " ORDER BY event_time_utc DESC").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for title, actual, forecast, _eut in rows:
        out.setdefault(title, [])
        if len(out[title]) < 5:
            out[title].append((actual, forecast))
    return out


@st.cache_data(ttl=300)
def db_speech_map():
    """{(db_title, tanggal): (tone, speaker, notes, ret_pct_m60_p60)}
    untuk pidato yang sudah dilabeli nada (hawkish/dovish/neutral)."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """SELECT e.title, e.event_time_utc, e.tone, e.speaker, e.notes, r.ret_pct
               FROM events e LEFT JOIN responses r
                 ON r.event_id = e.id AND r.window_label = 'm60_p60' AND r.symbol = 'GC=F'
               WHERE e.tone IS NOT NULL""").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for title, eut, tone, speaker, notes, ret in rows:
        try:
            d = datetime.strptime(eut, "%Y-%m-%dT%H:%M:%SZ").date().isoformat()
        except (ValueError, TypeError):
            continue
        out[(title, d)] = (tone, speaker, notes, ret)
    return out


@st.cache_data(ttl=300)
def db_speaker_stats():
    """{speaker: {'tones': [(tanggal, tone)], 'n_resp': n, 'avg_ret': x}}."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        for speaker, eut, tone in con.execute(
                "SELECT speaker, event_time_utc, tone FROM events"
                " WHERE speaker IS NOT NULL ORDER BY event_time_utc DESC"):
            try:
                d = datetime.strptime(eut, "%Y-%m-%dT%H:%M:%SZ").date().isoformat()
            except (ValueError, TypeError):
                continue
            st_ = out.setdefault(speaker, {"tones": [], "n_resp": 0, "avg_ret": 0.0})
            if tone:
                st_["tones"].append((d, tone))
        rows = con.execute(
            """SELECT e.speaker, r.ret_pct FROM responses r
               JOIN events e ON e.id = r.event_id
               WHERE r.window_label = 'm60_p60' AND r.symbol = 'GC=F'
                 AND e.speaker IS NOT NULL""").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    by_sp = {}
    for speaker, ret in rows:
        by_sp.setdefault(speaker, []).append(ret)
    for speaker, rets in by_sp.items():
        if speaker in out:
            out[speaker]["n_resp"] = len(rets)
            out[speaker]["avg_ret"] = sum(rets) / len(rets)
    return out


# ----------------------------------------------------------------------------
# Data: Week-ahead WSJ + catatan mingguan bulanan
# ----------------------------------------------------------------------------
@st.cache_data(ttl=300)
def load_week_ahead():
    """Tabel pra-registrasi WSJ minggu depan (week_ahead.json) atau None.

    File ditulis manual seminggu sekali dari WSJ Data Week Ahead (Jumat).
    Bukan auto-fetch — survei WSJ tidak punya API gratis."""
    try:
        with open(WEEK_AHEAD_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


@st.cache_data(ttl=300)
def load_monthly_notes():
    """{week_start_iso: {'label': ..., 'reason': ...}} dari monthly_notes.json."""
    try:
        with open(MONTHLY_NOTES_PATH) as f:
            data = json.load(f)
        out = {}
        for w in data.get("weeks", []):
            if w.get("week_start"):
                out[w["week_start"]] = w
        return out
    except (OSError, ValueError):
        return {}


@st.cache_data(ttl=300)
def load_weekly_narrative():
    """{week_start_iso: {...}} dari weekly_narrative.json (narasi makro mingguan).

    File ditulis manual tiap minggu dari angka terverifikasi — pola yang sama
    dengan alasan mingguan Oktober. Bila minggu ini belum ditulis, dialog
    menampilkan kalimat 'belum ditulis', bukan karangan."""
    try:
        with open(WEEKLY_NARRATIVE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("week_start"):
            return {data["week_start"]: data}
        if isinstance(data, dict):
            return {w.get("week_start"): w for w in data.get("weeks", [])
                    if w.get("week_start")}
    except (OSError, ValueError):
        pass
    return {}


@st.cache_data(ttl=1800)
def load_xaudaily():
    """(data, sumber) — live fetch diutamakan, fallback file bundel.

    sumber: 'live' | 'bundel <generated_at>' | 'tidak tersedia'.
    Live gagal (timeout/HTTP/validasi) -> file data/xaudaily_readings.json
    yang di-push terakhir. Keduanya gagal -> (None, 'tidak tersedia')."""
    try:
        req = urllib.request.Request(
            XAUDAILY_URL, headers={"User-Agent": "Muse-newsapp/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        if data.get("schema") == "xaudaily.readings/v1" and data.get("readings"):
            return data, "live"
    except Exception:
        pass
    try:
        with open(XAUDAILY_PATH) as f:
            data = json.load(f)
        if data.get("schema") == "xaudaily.readings/v1":
            return data, f"bundel {data.get('generated_at') or 'tanpa tanggal'}"
    except (OSError, ValueError):
        pass
    return None, "tidak tersedia"


# ----------------------------------------------------------------------------
# Nowcast Cleveland Fed (v3) — mirror parser kolektor cleveland_nowcast.py
# ----------------------------------------------------------------------------
NOWCAST_SERIES = {
    "CPI (headline m/m)": "CPI",
    "CPI (core m/m)": "Core CPI",
    "PCE (m/m)": "PCE",
    "Core PCE (m/m)": "Core PCE",
}
_NOWCAST_NAMES = ("CPI Inflation", "Core CPI Inflation",
                  "PCE Inflation", "Core PCE Inflation")
_NOWCAST_SHORT = {"CPI Inflation": "CPI", "Core CPI Inflation": "Core CPI",
                  "PCE Inflation": "PCE", "Core PCE Inflation": "Core PCE"}


def _nowcast_resolve_date(target_month, label):
    """'MM/DD' -> ISO date (label bisa di bulan target / bulan berikut)."""
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


def _parse_nowcast_frames(raw):
    """Raw JSON Cleveland -> {frames: {target_month: {series: {...}}}}.

    Nilai = MoM % (0,2421 = 0,24%). Final pre-release nowcast = nilai
    terakhir tidak kosong di frame. Frame/seri kosong dilewati."""
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
        cats = [c.get("label", "") for c in
                frame.get("categories", [{}])[0].get("category", [])]
        entry = {}
        for name in _NOWCAST_NAMES:
            ds = next((s for s in frame.get("dataset", [])
                       if s.get("seriesname") == name), None)
            if ds is None:
                continue
            vals = []
            for i, pt in enumerate(ds.get("data", [])):
                v = (pt or {}).get("value")
                if v in ("", None):
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                date = (_nowcast_resolve_date(target, cats[i])
                        if i < len(cats) else None)
                vals.append((date, fv))
            if not vals:
                continue
            (fd, fval), (ld, lval) = vals[0], vals[-1]
            diff = lval - fval
            direction = ("naik" if diff >= 0.02 else
                         "turun" if diff <= -0.02 else "stabil")
            entry[_NOWCAST_SHORT[name]] = {
                "nowcast": lval, "vintage": ld,
                "first_value": fval, "first_date": fd,
                "direction": direction}
        if entry:
            frames[target] = entry
    if not frames:
        raise ValueError("tidak ada frame yang bisa di-parse")
    from datetime import datetime as _dt
    return {"fetched_at": _dt.now(timezone.utc).isoformat(),
            "source": "Federal Reserve Bank of Cleveland, Inflation Nowcasting",
            "frames": frames}


@st.cache_data(ttl=1800)
def load_cleveland():
    """(data, sumber) — live fetch diutamakan, fallback file bundel.

    sumber: 'live' | 'bundel <fetched_at>' | 'tidak tersedia'.
    Live gagal (timeout/HTTP 403/validasi) -> file data/cleveland_nowcast.json
    yang di-push terakhir. Keduanya gagal -> (None, 'tidak tersedia')."""
    try:
        req = urllib.request.Request(
            CLEVELAND_URL, headers={"User-Agent": CLEVELAND_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.load(r)
        parsed = _parse_nowcast_frames(raw)
        if parsed.get("frames"):
            return parsed, "live"
    except Exception:
        pass
    try:
        with open(CLEVELAND_PATH) as f:
            parsed = json.load(f)
        if isinstance(parsed, dict) and parsed.get("frames"):
            return parsed, f"bundel {parsed.get('fetched_at') or 'tanpa tanggal'}"
    except (OSError, ValueError):
        pass
    return None, "tidak tersedia"


# ----------------------------------------------------------------------------
# Fed Speaker Tracker v1 (10 Nov 2026) — pidato Board of Governors
# ----------------------------------------------------------------------------
# Leksikon skor kata kunci — CERMIN fed_speeches.py (HAWKISH / DOVISH).
# Bila leksikon kolektor berubah, cermin ini wajib ikut berubah.
# Skor: raw = |hawkish| - |dovish| (frasa BERBEDA yang cocok);
# skor = clamp(raw * 0,5, -2, +2). Bukan penilaian LLM.
FED_LEXICON_HAWKISH = [
    "50 basis point", "credibility at risk", "raise the policy rate",
    "raise interest rates", "rate hike", "hiking", "further tightening",
    "tighten monetary policy", "restrictive", "inflation remains elevated",
    "inflation is too high", "inflation has been too high",
    "inflation too high", "above our 2 percent goal",
    "above the 2 percent goal", "more work to do",
    "upside risks to inflation", "persistent inflation",
    "stronger policy move", "additional rate increases",
    "further rate increases", "rates need to rise",
]
FED_LEXICON_DOVISH = [
    "cut the policy rate", "cut interest rates", "rate cut",
    "lower interest rates", "lower the policy rate", "ease monetary policy",
    "easing", "accommodative", "signs of disinflation", "disinflation",
    "inflation is falling", "inflation has fallen", "inflation moderating",
    "inflation has moderated", "avoid overtightening",
    "risk of overtightening", "risking a recession", "hold rates steady",
    "keep rates unchanged", "pause", "downside risks to growth",
    "risks to employment", "labor market softening",
    "weakness in the labor market", "unemployment rising",
    "patient approach", "be patient", "gradual normalization",
]


@st.cache_data(ttl=3600)
def load_fed_speeches():
    """(data, sumber) — live-first dari raw GitHub, fallback file bundel.

    sumber: 'live (GitHub)' | 'bundel <fetched_at>' | 'tidak tersedia'.
    Live = data/fed_speeches.json hasil push cron 30 menit (kolektor
    fed_speeches.py). Keduanya gagal -> (None, 'tidak tersedia')."""
    try:
        req = urllib.request.Request(
            FED_SPEECHES_RAW_URL, headers={"User-Agent": "Muse-newsapp/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        if isinstance(data, dict) and isinstance(data.get("speeches"), list):
            return data, "live (GitHub)"
    except Exception:
        pass
    try:
        with open(FED_SPEECHES_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("speeches"), list):
            return data, f"bundel {data.get('fetched_at') or 'tanpa tanggal'}"
    except (OSError, ValueError):
        pass
    return None, "tidak tersedia"


def _fmt_fed_date(iso):
    """'2026-11-10' -> '10 Nov 2026'."""
    if not iso:
        return "—"
    try:
        y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
        return f"{d} {BULAN[m - 1]} {y}"
    except (ValueError, IndexError):
        return iso


def _fed_score_txt(score, label):
    if score is None:
        return "—"
    return f"{score:+.1f} {label}"


# Garis reaksi TERVERIFIKASI (kutipan pidato/pernyataan, dgn tanggal).
# Tanpa kutipan -> tidak ada baris. Jangan invent.
FED_REACTION_LINES = {
    "Waller": ("Core CPI ≥0,3% → 50bp dipertimbangkan "
               "(pidato 10 Nov 2026)."),
}


def fed_speech_context(actuals, xaudaily_data):
    """Konteks pra-pidato (bukan ramalan skor): fundamental AS terbaru
    dari DB + harga pasar (odds FOMC Polymarket via xaudaily).
    Murni agar bisa dites unit. Kembalikan list baris teks; kosong
    bila tak ada data. Angka-angka ini membentuk ekspektasi (prior),
    bukan menentukan skor pidato."""
    lines = []
    for title, label, unit in (
            ("CPI (core m/m)", "Core CPI", "%"),
            ("CPI (headline m/m)", "CPI headline", "%"),
            ("Unemployment Rate", "Pengangguran", "%"),
            ("NFP", "NFP", "K"),
            ("Core PCE (m/m)", "Core PCE", "%")):
        d, v = _latest_actual(actuals or {}, title)
        if v is not None:
            lines.append(f"{label}: {v:g}{unit} ({_fmt_fed_date(d)})")
    pm = ((xaudaily_data or {}).get("readings", {}).get("extra", {})
          .get("polymarket") or {})
    if pm.get("hike25") is not None:
        lines.append(f"Odds hike Des (Polymarket): "
                     f"{pm['hike25'] * 100:.1f}%"
                     + (f" ({pm.get('asOf', '')})" if pm.get("asOf") else ""))
    return lines


def fed_upcoming_speeches(all_events, speakers, now_utc, hours=48):
    """Pidato Fed terjadwal (kalender FF) dalam `hours` jam ke depan.

    Murni (tanpa st.*) agar bisa dites unit. Judul FF berbentuk
    'FOMC Member Waller Speaks' — nama diambil dari kata sebelum
    'Speaks', dicocokkan ke ringkasan speakers (short name).
    Kembalikan list dict {event, dt, speaker_key, stance}, urut waktu,
    maks 5. Pembicara tak dikenal -> stance None (tanpa karangan)."""
    out = []
    if not all_events or now_utc is None:
        return out
    horizon = now_utc + timedelta(hours=hours)
    for e in all_events:
        title = (e.get("title") or "")
        if "speak" not in title.lower():
            continue
        dt = parse_utc(e.get("event_time_utc"))
        if not dt or not (now_utc < dt <= horizon):
            continue
        m = re.search(r"([A-Za-z][A-Za-z.'-]+)\s+Speaks", title)
        key = None
        if m:
            cand = m.group(1).strip(".'")
            for k in (speakers or {}):
                if k.lower() == cand.lower():
                    key = k
                    break
        out.append({"event": e, "dt": dt, "speaker_key": key,
                    "stance": (speakers or {}).get(key) if key else None})
    out.sort(key=lambda r: r["dt"])
    return out[:5]


def render_fed_tab(fed_data, fed_src, all_events=None, now_utc=None,
                   actuals=None, xaudaily_data=None):
    """Tab 'Suara Fed': brief pra-rilis + pidato terbaru + stance + metode.

    Tidak mengubah tab/dialog lain. Tanpa data -> 'tidak tersedia'.
    Skor pidato selalu SESUDAH teks diterbitkan; yang tampil sebelum
    pidato adalah brief pra-rilis (stance pembicara sebelum pidato)."""
    st.markdown("### 🎙️ Suara Fed — pidato Board of Governors")
    st.caption("Skor kata kunci transparan, bukan penilaian LLM · "
               "skor belum terbukti memprediksi emas (backtest = Fase 2).")
    if not fed_data:
        st.info("Data pidato Fed tidak tersedia — kolektor gagal atau file "
                "belum ter-push. Tidak dikarang.", icon="ℹ️")
        return
    speeches = fed_data.get("speeches") or []
    speakers = fed_data.get("speakers") or {}
    st.caption(f"Sumber: {fed_data.get('source', 'federalreserve.gov')} · "
               f"diambil {fed_src} · {len(speeches)} pidato tahun "
               f"{(fed_data.get('index_url') or '').split('/')[-1][:4]} · "
               f"hanya gubernur — 12 presiden regional belum masuk.")
    # (0) Pidato berikutnya — brief PRA-rilis. Skor pidato itu sendiri
    # baru ada SETELAH teks diterbitkan; yang tampil di sini adalah
    # posisi pembicara SEBELUM pidato (stance 90h + pidato terakhir).
    st.markdown("**Pidato Fed berikutnya** (brief pra-rilis)")
    upcoming = fed_upcoming_speeches(all_events, speakers, now_utc)
    if not upcoming:
        st.caption("Tidak ada pidato Fed terjadwal 48 jam ke depan "
                   "(kalender ForexFactory).")
    else:
        ctx = fed_speech_context(actuals, xaudaily_data)
        st.caption("**Konteks pra-pidato — bukan ramalan skor.** "
                   "Angka-angka ini membentuk ekspektasi (prior), bukan "
                   "menentukan skor; skor tetap dihitung dari teks pidato "
                   "setelah disampaikan.")
        if ctx:
            st.caption(" · ".join(ctx))
        for u in upcoming:
            e = u["event"]
            st.markdown(f"**{esc(e.get('title') or '—')}** — "
                        f"{esc(fmt_dt_wita(u['dt']))}")
            stance = u["stance"]
            if stance is None:
                st.caption("Pembicara belum ada di ringkasan stance "
                           "(bukan gubernur Board / belum pernah diskoring).")
            else:
                mean90 = stance.get("mean90")
                mean_txt = f"{mean90:+.2f}" if mean90 is not None else "—"
                latest = stance.get("latest") or {}
                lsc = latest.get("score")
                lsc_txt = f"{lsc:+.1f}" if lsc is not None else "—"
                cap = (f"Stance 90h: {mean_txt} (n={stance.get('n90', 0)}) · "
                       f"pidato terakhir {_fmt_fed_date(latest.get('date'))} "
                       f"skor {lsc_txt} — “{latest.get('title') or '—'}”. "
                       f"Skor pidato ini muncul setelah teks diterbitkan "
                       f"(biasanya saat pidato disampaikan); kolektor "
                       f"mengecek tiap 30 menit.")
                react = FED_REACTION_LINES.get(u["speaker_key"] or "")
                if react:
                    cap += f" Garis reaksi: {react}"
                st.caption(cap)
    # (a) Pidato terbaru
    st.markdown("**Pidato terbaru** (15 teratas)")
    if not speeches:
        st.info("Belum ada pidato tercatat.")
    else:
        rows = ["| Tanggal | Pembicara | Judul | Skor | Δ vs mean 90h |",
                "|---|---|---|---|---|"]
        for s in speeches[:15]:
            title = (s.get("title") or "—").replace("|", "/")
            link = s.get("url") or ""
            title_md = f"[{title}]({link})" if link else title
            d90 = s.get("delta90")
            if d90 is None:
                d90_txt = "—"
            else:
                d90_txt = f"{d90:+.1f}" + (" ⚠️" if s.get("alert") else "")
            rows.append(
                f"| {_fmt_fed_date(s.get('date'))} | {esc(s.get('speaker') or '—')} "
                f"| {title_md} | {_fed_score_txt(s.get('score'), s.get('label'))} "
                f"| {d90_txt} |")
        st.markdown("\n".join(rows))
        st.caption("⚠️ = |Δ vs mean 90h| ≥ 1,0 (aturan Fed Chirp, referensi) — "
                   "tanda perubahan nada, bukan sinyal trading. "
                   "Klik judul untuk teks penuh di federalreserve.gov.")
        # Evidence pidato terbaru yang punya skor
        latest_scored = next((s for s in speeches
                              if s.get("score") is not None
                              and s.get("evidence")), None)
        if latest_scored:
            ev = ", ".join(f"“{e}”" for e in latest_scored["evidence"])
            st.caption(f"Bukti frasa pidato terbaru "
                       f"({latest_scored.get('speaker')}, "
                       f"{_fmt_fed_date(latest_scored.get('date'))}): {ev}. "
                       f"Pidato tanpa frasa kunci yang cocok = skor 0 netral.")
    # (b) Stance per pembicara
    st.markdown("**Stance per pembicara** (90 hari terakhir)")
    if not speakers:
        st.info("Belum ada ringkasan pembicara.")
    else:
        rows = ["| Pembicara | n (90h) | Mean 90h | Pidato terakhir |",
                "|---|---|---|---|"]
        for sp in sorted(speakers):
            s = speakers[sp]
            latest = s.get("latest") or {}
            mean90 = s.get("mean90")
            mean_txt = f"{mean90:+.2f}" if mean90 is not None else "—"
            lsc = latest.get("score")
            lsc_txt = f"{lsc:+.1f}" if lsc is not None else "—"
            rows.append(
                f"| {esc(s.get('speaker_full') or sp)} | {s.get('n90', 0)} "
                f"| {mean_txt} | {_fmt_fed_date(latest.get('date'))} "
                f"({lsc_txt}) |")
        st.markdown("\n".join(rows))
        st.caption("Mean 90h = rata-rata skor pidato pembicara itu dalam 90 "
                   "hari terakhir (vs pidato terbaru di dataset). "
                   "n = jumlah pidato dalam 90 hari itu.")
    # (c) Metode + leksikon
    with st.expander("📖 Cara skor dihitung (metode + leksikon)"):
        st.markdown(
            "**Skor kata kunci, bukan penilaian LLM.** Setiap pidato dibaca "
            "teks penuhnya (halaman .htm federalreserve.gov; pidato yang "
            "hanya PDF dicatat 'teks belum terambil' dan tidak diskoring). "
            "Dihitung frasa BERBEDA yang cocok dari leksikon di bawah "
            "(kemunculan berulang tidak dihitung dua kali — pidato panjang "
            "tidak otomatis lebih ekstrem). "
            "Skor = (jumlah frasa hawkish − jumlah frasa dovish) × 0,5, "
            "dibatasi −2 sampai +2. "
            "Tiap skor menampilkan maks 3 frasa bukti yang ditemukan; "
            "pidato tanpa frasa kunci yang cocok = 0 netral, bukan penilaian "
            "tersembunyi.\n\n"
            "**Δ vs mean 90h** = skor pidato itu dikurangi rata-rata skor "
            "pidato pembicara yang sama dalam 90 hari SEBELUM pidato itu. "
            "Alert ⚠️ bila |Δ| ≥ 1,0 (aturan Fed Chirp, dikutip sebagai "
            "referensi) — artinya nada pembicara itu berubah tajam dari "
            "biasanya, bukan ramalan arah emas.\n\n"
            "**Batas:** hanya 6–8 gubernur Board (siapa pun yang muncul di "
            "halaman speeches); 12 presiden regional Fed hanya menerbitkan "
            "di situs bank masing-masing dan belum dikolek (Fase 2). "
            "Skor ini lapisan data, sama derajatnya dengan nowcast: "
            "dikolek, diskoring transparan, lalu diuji terhadap gerak emas "
            "— pengujiannya (backtest event-study) adalah Fase 2, setelah "
            "≥2 minggu koleksi live. Jangan jadikan skor ini sinyal entry.")
        st.markdown("**Leksikon hawkish:** " +
                    ", ".join(f"“{p}”" for p in FED_LEXICON_HAWKISH))
        st.markdown("**Leksikon dovish:** " +
                    ", ".join(f"“{p}”" for p in FED_LEXICON_DOVISH))
        st.markdown("Sumber teks: Board of Governors of the Federal "
                    "Reserve System — Speeches (federalreserve.gov). "
                    "Skor pihak ketiga (FedLock, Fed Chirp) hanya "
                    "cross-check, bukan sumber skor ini.")


def _target_month(d):
    """Bulan data yang dirilis: CPI/PCE rilis di bulan M untuk data M-1."""
    if d is None:
        return None
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"


def _fmt_vintage(iso):
    """'2026-11-05' -> '5 Nov 2026'."""
    if not iso:
        return "—"
    try:
        y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
        return f"{d} {BULAN[m - 1]} {y}"
    except (ValueError, IndexError):
        return iso


def nowcast_entry(cleveland_data, dbt, dt_utc):
    """Entri nowcast final untuk satu event (dbt + tanggal UTC event).

    None bila bukan event CPI/PCE, tanggal tak pasti, atau frame/seri
    tidak ada di data."""
    if not cleveland_data or not dbt or dt_utc is None:
        return None
    key = NOWCAST_SERIES.get(dbt)
    if not key:
        return None
    frame = (cleveland_data.get("frames") or {}).get(
        _target_month(dt_utc.astimezone(WITA).date()))
    if not frame:
        return None
    return frame.get(key)


def _nowcast_gap_bp(entry, forecast_num):
    """Selisih nowcast vs konsensus dalam bp (int, pembulatan). None bila
    forecast belum terbit."""
    if entry is None or forecast_num is None:
        return None
    return round((entry["nowcast"] - forecast_num) * 100)


def nowcast_pre_lines(cleveland_data, dbt, dt_utc, forecast_num):
    """Baris kotak 'Nowcast Cleveland Fed' pra-rilis (markdown).

    [] bila bukan event CPI/PCE. Bila data/frame tidak ada, satu baris
    'tidak tersedia' — dialog lain tetap jalan."""
    if dbt not in NOWCAST_SERIES:
        return []
    entry = nowcast_entry(cleveland_data, dbt, dt_utc)
    if entry is None:
        return ["_Nowcast Cleveland Fed tidak tersedia "
                "(fetch gagal / belum ada frame untuk rilis ini)._"]
    nc = entry["nowcast"]
    vint = _fmt_vintage(entry.get("vintage"))
    lines = []
    if forecast_num is not None:
        bp = _nowcast_gap_bp(entry, forecast_num)
        lines.append(
            f"**Nowcast Cleveland Fed (model estimate, vintage {vint}):** "
            f"**{nc:.2f}%** vs konsensus WSJ {forecast_num:.2f}% "
            f"(**{bp:+d} bp**).")
    else:
        lines.append(
            f"**Nowcast Cleveland Fed (model estimate, vintage {vint}):** "
            f"**{nc:.2f}%** vs konsensus WSJ belum terbit.")
    lines.append(
        f"Jalur nowcast: {entry['first_value']:.2f}% "
        f"({_fmt_vintage(entry.get('first_date'))}) → {nc:.2f}% ({vint}) — "
        f"{entry['direction']}.")
    if forecast_num is not None:
        fc = forecast_num
        if bp is not None and bp < 0:
            likely = (f"Nowcast {abs(bp)} bp DI BAWAH konsensus — skenario "
                     f"lunak lebih mungkin (model estimate, bukan prediksi).")
        elif bp is not None and bp > 0:
            likely = (f"Nowcast {bp} bp DI ATAS konsensus — skenario panas "
                     f"lebih mungkin (model estimate, bukan prediksi).")
        else:
            likely = "Nowcast sama dengan konsensus."
        lines.append(
            f"Skenario: lunak (actual &lt; {fc:.2f}%) → hike Desember "
            f"diragukan → textbook: emas didukung · sesuai (actual = "
            f"{fc:.2f}%) → hike tetap di harga → netral · panas (actual &gt; "
            f"{fc:.2f}%) → hike makin terkunci → textbook: emas tertekan. "
            f"{likely}")
    if "cpi" in (dbt or "").lower():
        lines.append(
            "_Arah emas di atas adalah implikasi textbook (kanal USD), bukan "
            "garansi — data event-study kita: CPI core hanya 1 dari 3 surprise "
            "yang searah dengan gerak emas ±1 jam._")
    else:
        lines.append(
            "_Arah emas di atas adalah implikasi textbook (kanal USD), bukan "
            "garansi — arah tidak bisa diklaim dari data event-study kita "
            "(N kecil)._")
    return lines


def nowcast_post_lines(cleveland_data, dbt, dt_utc, actual_num, forecast_num):
    """Baris perbandingan tiga arah pasca-rilis (markdown).

    [] bila bukan event CPI/PCE atau actual/forecast belum ada. Bagian emas
    (ret60/z/direction) TIDAK disentuh — ini baris tambahan."""
    if dbt not in NOWCAST_SERIES or actual_num is None or forecast_num is None:
        return []
    entry = nowcast_entry(cleveland_data, dbt, dt_utc)
    if entry is None:
        return ["_Nowcast Cleveland Fed tidak tersedia untuk rilis ini._"]
    nc = entry["nowcast"]
    d_fc = (actual_num - forecast_num) * 100
    d_nc = (actual_num - nc) * 100
    lines = [
        f"**Perbandingan tiga arah:** actual **{actual_num:.2f}%** vs "
        f"konsensus WSJ {forecast_num:.2f}% ({d_fc:+.0f} bp) vs nowcast "
        f"final Cleveland Fed {nc:.2f}% ({d_nc:+.0f} bp, vintage "
        f"{_fmt_vintage(entry.get('vintage'))})."]
    if abs(abs(d_nc) - abs(d_fc)) < 0.5:
        lines.append("Nowcast dan konsensus **seri** (selisih kedekatan "
                     "< 0,5 bp).")
    elif abs(d_nc) < abs(d_fc):
        lines.append("Nowcast **lebih dekat** ke actual daripada konsensus.")
    else:
        lines.append("Konsensus **lebih dekat** ke actual daripada nowcast.")
    return lines


def nowcast_week_line(cleveland_data, dbt, dt_utc, forecast_num):
    """Satu baris ringkas untuk dialog Mingguan, atau None bila tidak ada
    data/frame (Mingguan tidak menampilkan baris gagal)."""
    if dbt not in NOWCAST_SERIES:
        return None
    entry = nowcast_entry(cleveland_data, dbt, dt_utc)
    if entry is None:
        return None
    vint = _fmt_vintage(entry.get("vintage"))
    if forecast_num is not None:
        bp = _nowcast_gap_bp(entry, forecast_num)
        return (f"Nowcast Cleveland Fed: **{entry['nowcast']:.2f}%** "
                f"(vintage {vint}) vs konsensus WSJ {forecast_num:.2f}% "
                f"(**{bp:+d} bp**).")
    return (f"Nowcast Cleveland Fed: **{entry['nowcast']:.2f}%** "
            f"(vintage {vint}) vs konsensus WSJ belum terbit.")


def _latest_actual(actuals, title):
    """(tanggal, actual) terbaru untuk judul DB, atau (None, None)."""
    cand = [(d, v) for (t, d), v in actuals.items() if t == title]
    if not cand:
        return None, None
    d, v = max(cand)
    return d, v


def xaudaily_check_lines(xd, actuals):
    """Baris cross-check xaudaily vs DB (markdown, tanpa label sumber).

    Aturan: hanya NFP + UMich yang bisa dibandingkan langsung (level).
    CPI/PCE/PPI xaudaily = y/y, DB = m/m — TIDAK dibandingkan, hanya
    konteks. Nilai xaudaily = revisi terbaru, DB = first print."""
    if not xd:
        return ["_Cross-check xaudaily tidak tersedia (fetch gagal)._"]
    r = xd.get("readings", {})
    x = r.get("extra", {})
    lines = []
    live = (r.get("gold") or {}).get("live") or {}
    if live.get("last"):
        lines.append(f"Emas (COMEX tick ~30 mnt): **${live['last']:,.2f}** "
                     f"({live.get('date', '')} {live.get('time', '')}) — "
                     f"cross-check harga, bukan harga desk.")
    pm = x.get("polymarket") or {}
    if pm.get("hike25") is not None:
        lines.append(f"Odds FOMC 10 Des (Polymarket, {pm.get('asOf', '')}): "
                     f"hike **{pm['hike25'] * 100:.1f}%** · "
                     f"hold {pm.get('hold', 0) * 100:.1f}% · "
                     f"cut {(pm.get('cut25', 0) + pm.get('cut50', 0)) * 100:.1f}% — "
                     f"market pricing, bukan konsensus WSJ.")
    nfp = r.get("nfp") or {}
    if nfp.get("vals") and nfp.get("months"):
        xv = nfp["vals"][-1] * 10  # 10k persons -> K
        xm = nfp["months"][-1]
        d, dv = _latest_actual(actuals, "NFP")
        if dv is not None:
            if abs(xv - dv) < 0.5:
                lines.append(f"NFP {xm}: xaudaily **{xv:g}K** = DB first print "
                             f"**{dv:g}K** ({d}) — cocok.")
            else:
                lines.append(f"NFP {xm}: xaudaily **{xv:g}K** vs DB first print "
                             f"**{dv:g}K** ({d}) — beda = revisi setelah rilis, "
                             f"bukan kesalahan.")
        else:
            lines.append(f"NFP {xm}: xaudaily **{xv:g}K** "
                         f"(DB belum punya actual).")
    mi = x.get("michigan") or {}
    if mi.get("vals") and mi.get("months"):
        xv = mi["vals"][-1]
        xm = mi["months"][-1]
        d, dv = _latest_actual(actuals, "UMich Consumer Sentiment")
        if dv is not None:
            if abs(xv - dv) < 0.05:
                lines.append(f"UMich {xm}: xaudaily **{xv:g}** = DB "
                             f"**{dv:g}** ({d}) — cocok.")
            else:
                lines.append(f"UMich {xm}: xaudaily **{xv:g}** vs DB "
                             f"**{dv:g}** ({d}) — beda = revisi setelah rilis.")
        else:
            lines.append(f"UMich {xm}: xaudaily **{xv:g}** "
                         f"(DB belum punya actual).")
    cpi = r.get("cpi") or {}
    if cpi.get("vals"):
        lines.append(f"CPI y/y (xaudaily, {cpi.get('asOf', '')}): "
                     f"**{cpi['vals'][-1]:g}%** — y/y, tidak dibandingkan "
                     f"dengan m/m di DB. Rilis berikut {cpi.get('nextPub', '—')}.")
    evs = x.get("events") or []
    cpi_ev = next((e for e in evs if "CPI" in (e.get("title") or "")), None)
    if cpi_ev:
        lines.append(f"Estimasi model xaudaily untuk "
                     f"{cpi_ev.get('title', 'CPI')}: "
                     f"{cpi_ev.get('est', '—')} ({cpi_ev.get('prob', '')}) — "
                     f"estimasi model, BELUM tervalidasi, bukan konsensus.")
    stale = [k for k in ("gold", "cpi", "nfp", "pce", "ppi")
             if (r.get(key) or {}).get("stale")]
    if stale:
        lines.append(f"⚠ Field stale di xaudaily: {', '.join(stale)} — angka "
                     f"di atas adalah nilai terakhir yang baik, bukan terbaru.")
    return lines


def xaudaily_macro_line(xd):
    """Satu baris konteks makro bulanan (SPDR + arus bank sentral)."""
    if not xd:
        return None
    x = (xd.get("readings") or {}).get("extra", {})
    parts = []
    sp = x.get("spdr") or {}
    if sp.get("value"):
        parts.append(f"SPDR Gold ETF **{sp['value']:,.1f} ton** "
                     f"({sp.get('date', '')})")
    cf = x.get("cbFlow") or {}
    if cf.get("lastMonth"):
        parts.append(f"arus beli bank sentral (IMF) terakhir "
                     f"**{cf['lastMonth']}**")
    if not parts:
        return None
    return ("Konteks makro (xaudaily): " + " · ".join(parts) + ". "
            "Sumber: Gold Data Reading · XAU Daily (xaudaily.com), CC BY 4.0.")


@st.cache_data(ttl=300)
def db_strength():
    """{db_title: (N, mean_|ret60|, mean_|ret240|)} — besaran gerak khas emas.

    Kekuatan sinyal diukur dari BESARAN gerak (mean |ret|), bukan arah:
    dengan N sekecil ini arah belum bisa diklaim (lihat SPEC Analisa
    Fundamental v2). N selalu ditampilkan bersama angkanya."""
    out = {}
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """SELECT e.title, r.window_label, r.ret_pct FROM responses r
               JOIN events e ON e.id = r.event_id
               WHERE r.symbol = 'GC=F'""").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    by_title = {}
    for title, label, ret in rows:
        by_title.setdefault(title, {}).setdefault(label, []).append(ret)
    for title, labs in by_title.items():
        r60 = labs.get("m60_p60", [])
        r240 = labs.get("m60_p240", [])
        m60 = sum(abs(x) for x in r60) / len(r60) if r60 else None
        m240 = sum(abs(x) for x in r240) / len(r240) if r240 else None
        out[title] = (len(r60), m60, m240)
    return out


def strength_label(dbt):
    """(tier, N, mean_|ret60|) — tier per SPEC Analisa Fundamental v2.

    HIGH: N>=2 dan mean|ret60| >= 0,40%. MEDIUM: mean|ret60| >= 0,15%.
    LOW: sisanya. 'none' bila belum ada baris di DB."""
    s = db_strength()
    if not dbt or dbt not in s:
        return "none", 0, None
    n, m60, _m240 = s[dbt]
    if m60 is None:
        return "none", n, None
    if n >= 2 and m60 >= 0.40:
        return "HIGH", n, m60
    if m60 >= 0.15:
        return "MEDIUM", n, m60
    return "LOW", n, m60


def strength_line(dbt):
    """Satu baris label kekuatan sinyal untuk satu judul kanonis."""
    tier, n, m60 = strength_label(dbt)
    if tier == "HIGH":
        return (f"🔥 **BERNILAI TINGGI** — gerak khas emas ±1 jam "
                f"**{m60:.2f}%** (N={n} rilis tercatat)")
    if tier == "MEDIUM":
        return f"◆ SEDANG — gerak khas emas ±1 jam {m60:.2f}% (N={n})"
    if tier == "LOW":
        return (f"○ RENDAH (sejauh ini) — gerak khas emas ±1 jam {m60:.2f}% "
                f"(N={n})")
    return "Belum terukur — belum ada rilis tercatat di database."


def scenario_text(dbt):
    """Satu baris skenario pra-rilis via kanal USD (deterministik, bukan ramalan)."""
    t = (dbt or "").lower()
    if "claim" in t:
        return ("Skenario: klaim **di bawah** forecast (tenaga kerja kuat) → USD menguat → "
                "emas cenderung tertekan; sebaliknya emas cenderung terdukung.")
    return ("Skenario: actual **di atas** forecast → USD menguat → emas cenderung "
            "tertekan; sebaliknya emas cenderung terdukung.")


def combined_read(rows, label):
    """Bacaan gabungan atas event yang SUDAH rilis. None bila belum ada.

    Menghitung dari tiap rilis via gold_direction(): 'down' = pro-USD
    (menekan emas), 'up' = kontra-USD (mendukung emas). Kejutan terbesar
    dipilih dari z-score deviasi vs histori bila ada, else selisih
    actual-forecast terbesar. Komponen selalu ditampilkan, bukan black box."""
    done = [r for r in rows
            if r["actual"] is not None and r["forecast"] is not None]
    if not done:
        return None
    pro = contra = neutral = 0
    scored = []
    for r in done:
        d = gold_direction(r["title"], r["actual"], r["forecast"])
        if d == "down":
            pro += 1
        elif d == "up":
            contra += 1
        else:
            neutral += 1
        info = deviation_info(r["title"], r["actual"], r["forecast"], surprises)
        scored.append((r, info["z"] if info else None))
    with_z = [(r, z) for r, z in scored if z is not None]
    if with_z:
        big_r, big_z = max(with_z, key=lambda x: abs(x[1]))
        big_txt = (f"Kejutan terbesar: **{big_r['title']}** "
                   f"(z={big_z:+.1f}σ vs histori deviasi)")
    else:
        big_r = max(done, key=lambda r: abs(r["actual"] - r["forecast"]))
        big_txt = (f"Kejutan terbesar: **{big_r['title']}** "
                   f"(actual {big_r['actual']:g} vs forecast {big_r['forecast']:g})")
    if pro > contra:
        net = (f"Arah bersih {label}: **pro-USD** — {pro} event menekan emas "
               f"vs {contra} mendukung")
    elif contra > pro:
        net = (f"Arah bersih {label}: **kontra-USD** — {contra} event mendukung emas "
               f"vs {pro} menekan")
    else:
        net = (f"Arah bersih {label}: **berimbang** — {pro} pro-USD "
               f"vs {contra} kontra-USD")
    if neutral:
        net += f" ({neutral} sesuai ekspektasi)"
    return f"**Bacaan gabungan {label}:** {net}.\n\n{big_txt}."


@st.cache_data(ttl=300)
def db_event_rows(date_from, date_to):
    """Baris event-study per tanggal rilis: actual/forecast + respon GC=F.

    List dict {title, eut, forecast, previous, actual, ret60, ret240}
    terurut waktu rilis. ret = None bila jendela belum terukur
    (tidak dikarang)."""
    out = []
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT id, title, event_time_utc, forecast, previous, actual FROM events"
            " WHERE substr(event_time_utc, 1, 10) >= ? AND substr(event_time_utc, 1, 10) <= ?"
            " ORDER BY event_time_utc", (date_from, date_to)).fetchall()
        ids = [r[0] for r in rows]
        resp = {}
        if ids:
            q = ",".join("?" * len(ids))
            for eid, label, ret in con.execute(
                    "SELECT event_id, window_label, ret_pct FROM responses"
                    f" WHERE event_id IN ({q}) AND symbol = 'GC=F'", ids):
                resp.setdefault(eid, {})[label] = ret
        con.close()
    except sqlite3.Error:
        return out
    for eid, title, eut, fc, prev, actual in rows:
        r = resp.get(eid, {})
        out.append({"title": title, "eut": eut, "forecast": fc, "previous": prev,
                    "actual": actual, "ret60": r.get("m60_p60"),
                    "ret240": r.get("m60_p240")})
    return out


def wsj_override_map(week_ahead):
    """{(db_title, tanggal_wita): event_wsj} untuk override forecast kartu.

    Hanya metrik yang punya judul DB 1:1. y/y dikecualikan — metrik berbeda
    dari m/m, jangan sampai menimpa kartu m/m."""
    out = {}
    if not week_ahead:
        return out
    for ev in week_ahead.get("events", []):
        title = (ev.get("title") or "")
        if "y/y" in title.lower():
            continue
        dbt = db_title_for(title)
        if not dbt or not ev.get("date"):
            continue
        out[(dbt, ev["date"])] = ev
    return out


def wsj_table_markdown(week_ahead, actuals):
    """Tabel markdown minggu depan WSJ + kolom Actual dari DB bila sudah rilis."""
    if not week_ahead:
        return ""
    rows = ["| Hari | Jam | Event | Forecast WSJ | Previous | Actual |",
            "|---|---|---|---|---|---|"]
    for ev in week_ahead.get("events", []):
        d = ev.get("date") or ""
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            hari = f"{HARI[dt.weekday()]}, {dt.day} {BULAN[dt.month-1]}"
        except ValueError:
            hari = d
        fc = ev.get("forecast_raw") or "-"
        n = ev.get("n_economists")
        fc_lbl = f"{fc} ({n})" if n else fc
        dbt = db_title_for(ev.get("title") or "")
        actual = actuals.get((dbt, d)) if dbt else None
        act_lbl = f"{actual:g}" if actual is not None else "—"
        rows.append(f"| {hari} | {ev.get('time_wita') or '-'} | {ev.get('title')} | "
                    f"{fc_lbl} | {ev.get('previous') or '-'} | {act_lbl} |")
    table = "\n".join(rows)
    src = week_ahead.get("source", "WSJ Data Week Ahead")
    notes = "\n".join(f"- {n}" for n in week_ahead.get("notes", []))
    extra = f"\n\n{notes}" if notes else ""
    return (f"**Minggu Depan — konsensus WSJ**\n\n{table}\n\n"
            f"Sumber: {src} · angka dalam kurung = jumlah ekonom · "
            f"NA = tidak ada konsensus WSJ (kosong, bukan 0){extra}")


def speech_box_html(e, dbt, now_utc, speech_map, speaker_stats):
    """Box pra/pasca pidato.

    PRA (<=7 hari): konteks factual — histori nada pembicara + reaksi
    rata-rata emas. Pidato tidak punya angka forecast, jadi yang disajikan
    adalah nada historis, bukan prediksi nada.
    PASCA: label nada terverifikasi + reaksi emas terukur."""
    title = e.get("title") or "?"
    speaker = speech_speaker(title)
    if not speaker:
        return ""
    dt_utc = parse_utc(e.get("event_time_utc"))
    stats = speaker_stats.get(speaker, {"tones": [], "n_resp": 0, "avg_ret": 0.0})
    if dt_utc and dt_utc >= now_utc:
        if (dt_utc - now_utc).total_seconds() > 7 * 24 * 3600:
            return ""
        lines = [
            f"🎙️ <b>Pidato {esc(speaker)}</b> — tidak ada angka forecast; "
            f"yang menggerakkan pasar adalah <b>nada hawkish/dovish</b> vs ekspektasi."]
        if stats["tones"]:
            hist = ", ".join(f"{tone} ({d})" for d, tone in stats["tones"][:3])
            lines.append(f"Nada pidato sebelumnya: <b>{esc(hist)}</b>")
        else:
            lines.append("Belum ada histori nada untuk pembicara ini di DB.")
        if stats["n_resp"]:
            caveat = " — data awal, belum stabil" if stats["n_resp"] < 5 else ""
            lines.append(
                f"Reaksi rata-rata emas ±1 jam pasca-pidato: "
                f"<b>{stats['avg_ret']:+.3f}%</b> (N={stats['n_resp']}{caveat})")
        lis = "".join(f"<div>· {l}</div>" for l in lines)
        return (f'<div class="prebrief">{lis}'
                f'<div class="muted">konteks factual dari DB event-study · bukan prediksi nada</div></div>')
    key = (dbt, dt_utc.date().isoformat()) if (dbt and dt_utc) else None
    rec = speech_map.get(key) if key else None
    if not rec:
        return ""
    tone, _sp, notes, ret = rec
    tone_id = {"hawkish": "hawkish 🔴", "dovish": "dovish 🟢",
               "neutral": "netral ⚪"}.get(
        (tone or "").lower(), f"{esc(tone or '-')} ⚪")
    react = (f"· Reaksi terukur emas ±1 jam: <b>{ret:+.3f}%</b> (GC=F)"
             if ret is not None else "· Reaksi terukur: belum diukur")
    return (
        f'<div class="prebrief">🎙️ <b>Pasca-pidato {esc(speaker)}</b>'
        f"<div>· Nada: <b>{tone_id}</b></div>"
        f"<div>· {esc(notes or '')}</div>"
        f"<div>{react}</div>"
        f'<div class="muted">label nada dari riset terverifikasi · angka = hasil ukur</div></div>'
    )


def pre_release_brief_html(e, dbt, typical, surprises, now_utc):
    """Box brief pra-rilis di bawah kartu event mendatang (<=7 hari).

    Tier 1 (SEMUA event): konsensus vs previous + skenario arah via kanal USD.
    Tier 2 (event dengan histori DB): frekuensi surprise + respon tipikal.
    Murni kompilasi data + heuristik transparan. BUKAN ramalan angka rilis."""
    dt_utc = parse_utc(e.get("event_time_utc"))
    if not (dt_utc and dt_utc >= now_utc):
        return ""
    if (dt_utc - now_utc).total_seconds() > 7 * 24 * 3600:
        return ""
    f_raw, p_raw = e.get("forecast"), e.get("previous")
    f_num, p_num = parse_num(f_raw), parse_num(p_raw)
    lines = []
    # Tier 1a: ekspektasi konsensus
    if f_num is not None and p_num is not None:
        arah = "naik" if f_num > p_num else ("turun" if f_num < p_num else "sama")
        lines.append(
            f"Konsensus <b>{esc(f_raw)}</b> vs previous {esc(p_raw)} "
            f"→ ekspektasi <b>{arah}</b>")
    elif f_num is not None:
        lines.append(f"Konsensus <b>{esc(f_raw)}</b> (previous belum ada di sumber)")
    else:
        lines.append("Konsensus <b>belum terbit</b> di sumber")
    # Tier 1b: skenario arah via kanal USD (deterministik, bukan ramalan)
    t = (dbt or "").lower()
    if "claim" in t:
        lines.append(
            "Skenario: klaim <b>di bawah</b> forecast (tenaga kerja kuat) → USD menguat "
            "→ emas cenderung tertekan; sebaliknya emas cenderung terdukung.")
    else:
        lines.append(
            "Skenario: actual <b>di atas</b> forecast → USD menguat → emas cenderung "
            "tertekan; sebaliknya emas cenderung terdukung.")
    # Tier 2: histori DB bila ada mapping
    if dbt:
        hist = (surprises or {}).get(dbt, [])
        if hist:
            above = sum(1 for a, f in hist if a > f)
            lines.append(
                f"Frekuensi historis: <b>{above}/{len(hist)}</b> rilis terakhir "
                f"angkanya di atas forecast (probabilitas berbasis data, bukan ramalan)")
        if dbt in typical:
            n, avg = typical[dbt]
            caveat = " — data awal, belum stabil" if n < 5 else ""
            lines.append(
                f"Respon tipikal ±1 jam: <b>{avg:+.3f}%</b> (N={n}{caveat})")
    lis = "".join(f"<div>· {l}</div>" for l in lines)
    return (
        f'<div class="prebrief">🔮 <b>Brief pra-rilis</b> '
        f'<span class="muted">(kompilasi data + skenario kanal USD, bukan ramalan angka)</span>{lis}</div>'
    )


def gold_direction(dbt, actual_num, forecast_num):
    """Arah implisit untuk EMAS dari satu rilis: 'down' (menekan), 'up'
    (mendukung), 'neutral', atau 'none' (data belum lengkap).

    Kanal USD: data AS yang menguatkan USD → menekan emas; sebaliknya
    mendukung emas. Claims dibalik (klaim rendah = tenaga kerja kuat)."""
    if actual_num is None or forecast_num is None:
        return "none"
    if actual_num == forecast_num:
        return "neutral"
    t = (dbt or "").lower()
    is_claims = "claim" in t
    hotter = actual_num > forecast_num
    usd_strong = (hotter and not is_claims) or ((not hotter) and is_claims)
    return "down" if usd_strong else "up"


def deviation_info(dbt, actual_num, forecast_num, surprises):
    """Skor deviasi untuk rilis yang sudah keluar: (z, direction, n).

    z = (actual-forecast) / std(surprise historis DB) — proper standardized
    surprise, comparable antar event. None bila histori < 3 atau std = 0.
    direction = arah implisit emas via gold_direction()."""
    if actual_num is None or forecast_num is None:
        return None
    direction = gold_direction(dbt, actual_num, forecast_num)
    hist = (surprises or {}).get(dbt, [])
    z = None
    if len(hist) >= 3:
        diffs = [a - f for a, f in hist]
        mean = sum(diffs) / len(diffs)
        var = sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1)
        if var > 0:
            z = (actual_num - forecast_num) / (var ** 0.5)
    return {"z": z, "direction": direction, "n": len(hist)}


def event_actual(e, actuals):
    """(actual_num, forecast_num, dbt) untuk satu event kalender."""
    title = e.get("title") or "?"
    dbt = db_title_for(title)
    dt_utc = parse_utc(e.get("event_time_utc"))
    actual_num = None
    if dbt and dt_utc:
        actual_num = actuals.get((dbt, dt_utc.date().isoformat()))
    return actual_num, parse_num(e.get("forecast")), dbt


def net_effect_html(group, actuals):
    """Efek BERSIH ke emas untuk sekelompok rilis di jam yang sama.

    Menghitung dari tiap event (positif/netral/negatif → menekan/mendukung
    emas via kanal USD), lalu merangkum jadi satu teks. Heuristik transparan:
    komponennya selalu ditampilkan, bukan black box."""
    scored = []
    for e in group:
        actual_num, f_num, dbt = event_actual(e, actuals)
        d = gold_direction(dbt, actual_num, f_num)
        if d == "none":
            continue
        scored.append((e.get("title") or "?", d))
    if len(scored) < 2:
        return ""
    downs = [t for t, d in scored if d == "down"]
    ups = [t for t, d in scored if d == "up"]
    neuts = [t for t, d in scored if d == "neutral"]
    score = len(downs) - len(ups)
    if score >= 3:
        label = "tekanan <b>KUAT</b> ke bawah"
    elif score >= 1:
        label = "cenderung <b>menekan</b> emas ke bawah"
    elif score <= -3:
        label = "dorongan <b>KUAT</b> ke atas"
    elif score <= -1:
        label = "cenderung <b>mengangkat</b> emas ke atas"
    else:
        label = "<b>CAMPURAN</b> — tarik-menarik, bias tipis"
    dt = parse_utc(group[0].get("event_time_utc"))
    jam = dt.astimezone(WITA).strftime("%H:%M") if dt else "?"

    def short(t):
        return t.replace(" m/m", "").replace(" y/y", "")

    parts = []
    if downs:
        parts.append(f"{len(downs)} menekan ({', '.join(short(t) for t in downs)})")
    if ups:
        parts.append(f"{len(ups)} mendukung ({', '.join(short(t) for t in ups)})")
    if neuts:
        parts.append(f"{len(neuts)} netral")
    return (
        f'<div class="netfx">⚖️ <b>Efek bersih {len(scored)} rilis ({jam} WITA)</b>: {label}'
        f'<div class="muted">{" · ".join(parts)} — heuristik kanal USD, bukan model</div></div>'
    )


# ----------------------------------------------------------------------------
# Render kartu
# ----------------------------------------------------------------------------
def event_card_html(e, now_utc, actuals, typical, surprises=None,
                    speech_map=None, speaker_stats=None):
    title = e.get("title") or "?"
    impact = e.get("impact") or "Medium"
    badge_cls = "high" if impact == "High" else ("med" if impact == "Medium" else "low")
    src = e.get("src") or ""
    dt_utc = parse_utc(e.get("event_time_utc"))

    if dt_utc:
        dt_str = fmt_dt_wita(dt_utc)
        rel = rel_time(dt_utc, now_utc)
        rel_cls = "future" if dt_utc >= now_utc else (
            "fresh" if (now_utc - dt_utc).total_seconds() < 3600 else "past")
    else:
        dt_str = "waktu tak pasti"
        rel, rel_cls = "jadwal tentatif", "past"

    f_raw, p_raw = e.get("forecast"), e.get("previous")
    f_num = parse_num(f_raw)

    dbt = db_title_for(title)
    spk = speech_speaker(title)
    actual_num, actual_html = None, '<span class="muted">menunggu rilis</span>'
    if dbt and dt_utc:
        actual_num = actuals.get((dbt, dt_utc.date().isoformat()))
    if actual_num is not None:
        verdict, vlabel = econ_verdict(dbt, actual_num, f_num)
        vcls = {"good": "good", "bad": "bad"}.get(verdict, "neutral")
        actual_html = (f'<b class="{vcls}">{esc(fmt_actual(actual_num, f_raw))}</b>'
                       f' <span class="vlabel {vcls}">{esc(vlabel)}</span>')
    elif dt_utc and dt_utc < now_utc:
        actual_html = '<span class="muted">belum tercatat</span>'

    fpa = (
        f'<div class="frow"><span>Forecast</span><b>{esc(f_raw) if f_raw else "-"}</b></div>'
        f'<div class="frow"><span>Previous</span><b>{esc(p_raw) if p_raw else "-"}</b></div>'
        f'<div class="frow"><span>Actual</span>{actual_html}</div>'
    )

    dev_html = ""
    if actual_num is not None and not spk:
        dev = deviation_info(dbt, actual_num, f_num, surprises)
        if dev:
            if dev["z"] is not None:
                small = ' <i class="caveat">— sampel kecil</i>' if dev["n"] < 5 else ""
                dev_html = (
                    f'<div class="devrow">📐 Deviasi: <b>{dev["z"]:+.1f}σ</b> dari ekspektasi'
                    f' <span class="muted">(N={dev["n"]})</span>{small}</div>')
            else:
                raw = actual_num - f_num
                dev_html = (
                    f'<div class="devrow">📐 Deviasi: <b>{raw:+g}</b> dari forecast'
                    f' <span class="muted">(skala σ belum tersedia, histori &lt; 3)</span></div>')

    typ_html = ""
    if dbt and dbt in typical:
        n, avg = typical[dbt]
        caveat = ' <i class="caveat">— data awal, belum stabil</i>' if n < 5 else ""
        typ_html = (
            f'<div class="typ">📊 <b>Respon tipikal {esc(dbt)}</b> ±1 jam: '
            f'<b class="{"up" if avg >= 0 else "down"}">{avg:+.3f}%</b> '
            f'<span class="muted">(N={n})</span>{caveat}'
            f'<div class="typnote">rata-rata historis GC=F pasca-rilis · bukan ramalan</div></div>'
        )
    elif dbt:
        typ_html = ('<div class="typ"><span class="muted">📊 Belum ada data respon historis '
                    'untuk metrik ini</span></div>')

    src_badge = f'<span class="badge src">{esc(src)}</span>' if src else ""
    if spk:
        # Pidato: pakai box pidato (nada), bukan brief angka
        sbox_html = speech_box_html(e, dbt, now_utc, speech_map or {}, speaker_stats or {})
        brief_html = ""
        typ_html = sbox_html  # gantikan box respon tipikal generik
    else:
        brief_html = pre_release_brief_html(e, dbt, typical, surprises or {}, now_utc)
    return (
        f'<div class="ev-card">'
        f'<div class="ev-top"><div><span class="badge {badge_cls}">{esc(impact)}</span>{src_badge}</div>'
        f'<span class="rel {rel_cls}">{esc(rel)}</span></div>'
        f'<div class="ev-title">{esc(title)}</div>'
        f'<div class="ev-time">{esc(dt_str)}</div>'
        f'<div class="fpa">{fpa}</div>'
        f'{dev_html}{typ_html}{brief_html}</div>'
    )


def render_event_list(evs, now_utc, actuals, typical, surprises,
                      speech_map=None, speaker_stats=None):
    """Render daftar event; kelompokkan per jam rilis WITA dan sisipkan
    box efek bersih untuk jam yang punya >=2 rilis dengan actual."""
    groups, order, singles = {}, [], []
    for e in evs:
        dt = parse_utc(e.get("event_time_utc"))
        if dt is None:
            singles.append(e)
            continue
        w = dt.astimezone(WITA)
        key = (w.date().isoformat(), w.hour, w.minute)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    for key in order:
        g = groups[key]
        box = net_effect_html(g, actuals)
        if box:
            st.markdown(box, unsafe_allow_html=True)
        for e in g:
            st.markdown(event_card_html(e, now_utc, actuals, typical, surprises,
                                        speech_map, speaker_stats),
                        unsafe_allow_html=True)
    for e in singles:
        st.markdown(event_card_html(e, now_utc, actuals, typical, surprises,
                                    speech_map, speaker_stats),
                    unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# UI utama
# ----------------------------------------------------------------------------
now_utc = datetime.now(timezone.utc)
now_wita = now_utc.astimezone(WITA)
today_wita = now_wita.date()

st.title("📰 News XAUUSD")
st.caption("Kalender & dampak rilis ke emas · Bahasa Indonesia")

col_a, col_b = st.columns([3, 1])
with col_b:
    if st.button("🔄 Perbarui data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

ff_events, ff_note = load_ff_events()
tv_events = load_tradingview_events()
key = finnhub_key()
# Finnhub jadi fallback bila TradingView gagal total
fh_events = load_finnhub_events(key) if (key and not tv_events) else None

# Batas minggu berjalan (Senin–Minggu WITA)
week_start = today_wita - timedelta(days=today_wita.weekday())
week_end = week_start + timedelta(days=6)


def _wita_d(e):
    dt = parse_utc(e.get("event_time_utc"))
    return dt.astimezone(WITA).date() if dt else None


# TV/Finnhub hanya untuk SETELAH minggu berjalan — FF yang pegang minggu ini
tv_events = [e for e in tv_events if (_wita_d(e) or week_end) > week_end]
fh_events = [e for e in (fh_events or []) if (_wita_d(e) or week_end) > week_end]

# Gabung + dedupe (FF diutamakan untuk minggu berjalan, lalu TV, lalu Finnhub)
seen, all_events = set(), []
for e in ff_events + tv_events + (fh_events or []):
    k = ((e.get("title") or "").lower(), e.get("event_time_utc"))
    if k not in seen:
        seen.add(k)
        all_events.append(e)
all_events.sort(key=lambda e: (e.get("event_time_utc") is None, e.get("event_time_utc") or ""))

actuals = db_actuals()
typical = db_typical()
surprises = db_surprises()
speech_map = db_speech_map()
speaker_stats = db_speaker_stats()
week_ahead = load_week_ahead()
monthly_notes = load_monthly_notes()
weekly_narrative = load_weekly_narrative()
xaudaily_data, xaudaily_src = load_xaudaily()
cleveland_data, cleveland_src = load_cleveland()
fed_data, fed_src = load_fed_speeches()

# Override forecast kartu minggu depan dengan median WSJ (bila cocok
# judul kanonis + tanggal WITA). Kartu tetap menampilkan badge sumber WSJ.
wsj_map = wsj_override_map(week_ahead)
for e in all_events:
    dt = parse_utc(e.get("event_time_utc"))
    if not dt:
        continue
    key = (db_title_for(e.get("title") or ""), dt.astimezone(WITA).date().isoformat())
    w = wsj_map.get(key)
    if w and w.get("forecast_raw"):
        e["forecast"] = w["forecast_raw"]
        e["src"] = "WSJ"

# --- Box rilis berikutnya (hitung mundur statis, dihitung saat render)
# Urut eksplisit via datetime (bukan string) agar selalu event terdekat.
upcoming = sorted(
    (e for e in all_events
     if parse_utc(e.get("event_time_utc")) and parse_utc(e.get("event_time_utc")) >= now_utc),
    key=lambda e: parse_utc(e.get("event_time_utc")))
if upcoming:
    nx = upcoming[0]
    nx_dt = parse_utc(nx["event_time_utc"])
    delta = nx_dt - now_utc
    hrs, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    if hrs >= 24:
        cd = f"{hrs // 24} hari {hrs % 24} jam lagi"
    elif hrs > 0:
        cd = f"{hrs} jam {mins} menit lagi"
    else:
        cd = f"{mins} menit lagi"
    st.markdown(
        f'<div class="nextbox"><div class="muted">EVENT BERIKUTNYA</div>'
        f'<div class="ev-title">{esc(nx.get("title"))} '
        f'<span class="badge {"high" if nx.get("impact") == "High" else "med"}">{esc(nx.get("impact"))}</span></div>'
        f'<div class="ev-time">{esc(fmt_dt_wita(nx_dt))}</div>'
        f'<div class="cd">⏳ {cd}</div>'
        f'<div class="muted">dihitung saat halaman dimuat · {now_wita.strftime("%H:%M")} WITA</div></div>',
        unsafe_allow_html=True,
    )
else:
    st.info("Tidak ada rilis terjadwal dalam data yang dimuat.")

st.caption(f"Sumber kalender: {ff_note}")
if tv_events:
    st.caption(f"🔭 {len(tv_events)} event minggu-minggu depan via TradingView Economic Calendar (publik, tanpa API key).")
elif fh_events:
    st.caption(f"🔭 {len(fh_events)} event minggu depan via Finnhub.")
else:
    st.warning("ℹ️ Data minggu depan belum tersedia — feed gratis hanya mencakup "
               "minggu berjalan dan sumber TradingView/Finnhub tidak mengembalikan "
               "event. Tidak dikarang.", icon="ℹ️")

# --- Filter impact
imp_filter = st.radio("Filter impact", ["Semua", "High", "Medium"], horizontal=True)


def filt(events):
    if imp_filter == "Semua":
        return events
    return [e for e in events if e.get("impact") == imp_filter]


# --- Partisi tab
month_key = (now_wita.year, now_wita.month)


def wita_date(e):
    dt = parse_utc(e.get("event_time_utc"))
    return dt.astimezone(WITA).date() if dt else None


today_events = [e for e in all_events if wita_date(e) == today_wita]
week_events = [e for e in all_events
               if wita_date(e) and week_start <= wita_date(e) <= week_end]
month_events = [e for e in all_events
                 if wita_date(e) and (wita_date(e).year, wita_date(e).month) == month_key]
next_week_events = [e for e in all_events
                     if wita_date(e) and week_end < wita_date(e) <= week_end + timedelta(days=7)]


# --- Dialog analisa (tombol -> jendela modal) ---
@st.dialog("📊 Analisa Harian", width="large")
def dialog_harian():
    st.markdown(f"**{HARI[today_wita.weekday()]}, {today_wita.day} {BULAN[today_wita.month-1]} "
                f"{today_wita.year}** — {len(today_events)} event USD medium/high.")
    # Makro hari ini: narasi minggu berjalan
    nar = (weekly_narrative or {}).get(week_start.isoformat())
    if nar and nar.get("macro"):
        st.markdown(f"**Makro minggu ini — {nar.get('theme', '')}:** {nar['macro']}")
    else:
        st.markdown("_Narasi makro minggu ini belum ditulis._")
    rows = db_event_rows(today_wita.isoformat(), today_wita.isoformat())
    ret60 = {r["title"]: r["ret60"] for r in rows}
    todays = filt(today_events)
    if todays:
        for e in todays:
            dbt = db_title_for(e.get("title") or "")
            dt = parse_utc(e.get("event_time_utc"))
            st.markdown(f"### {e.get('title')} — {fmt_dt_wita(dt) if dt else '—'}")
            st.markdown(strength_line(dbt))
            actual_num, fc_num, _ = event_actual(e, actuals)
            if actual_num is not None:
                d = deviation_info(dbt, actual_num, fc_num, surprises)
                r60 = ret60.get(dbt)
                r60_txt = f"{r60:+.2f}%" if r60 is not None else "belum terukur"
                z_txt = f" (z={d['z']:+.1f}σ)" if d and d["z"] is not None else ""
                arah = ("menekan" if (d and d["direction"] == "down")
                        else ("mendukung" if (d and d["direction"] == "up")
                              else "netral"))
                st.markdown(
                    f"**Pasca-rilis:** actual **{fmt_actual(actual_num, e.get('forecast'))}** "
                    f"vs forecast {esc(e.get('forecast') or '—')} · "
                    f"respon emas ±1 jam **{r60_txt}**{z_txt} · "
                    f"arah implisit emas: **{arah}**.")
                for ln in nowcast_post_lines(
                        cleveland_data, dbt, dt, actual_num, fc_num):
                    st.markdown(ln)
            else:
                st.markdown(
                    f"**Pra-rilis:** konsensus **{esc(e.get('forecast') or 'belum terbit')}** "
                    f"vs previous {esc(e.get('previous') or '—')}.")
                st.markdown(scenario_text(dbt))
                for ln in nowcast_pre_lines(
                        cleveland_data, dbt, dt, fc_num):
                    st.markdown(ln)
    else:
        # Hari tanpa event: preview event berikut, bukan kosong.
        st.markdown("**Hari ini tidak ada event USD medium/high.** "
                    "Yang perlu diperhatikan:")
        for e in upcoming[:2]:
            dbt = db_title_for(e.get("title") or "")
            dt = parse_utc(e.get("event_time_utc"))
            st.markdown(f"- **{e.get('title')}** — {fmt_dt_wita(dt) if dt else '—'} · "
                        f"konsensus {esc(e.get('forecast') or 'belum terbit')} · "
                        f"{strength_line(dbt)}")
    upcoming_today = [e for e in upcoming
                      if _wita_d(e) == today_wita and e.get("impact") == "High"]
    if upcoming_today:
        nx = upcoming_today[0]
        st.info(f"📌 Sebelum {fmt_dt_wita(parse_utc(nx['event_time_utc']))}: "
                f"snapshot kolam Tahap 2 (pool disapu + pool target, toleransi "
                f"0,25×ATR) dari chart-mu sendiri — gerakan pertama pasca-rilis "
                f"dibaca sebagai sweep, bukan keputusan.")


@st.dialog("📈 Analisa Mingguan", width="large")
def dialog_mingguan():
    ws, we = week_start.isoformat(), week_end.isoformat()
    rows = db_event_rows(ws, we)
    done = [r for r in rows if r["actual"] is not None]
    st.markdown(f"**Minggu {week_start.day}–{week_end.day} "
                f"{BULAN[week_start.month-1]} {week_start.year}** — "
                f"{len(rows)} event tercatat di DB, {len(done)} sudah rilis "
                f"dengan actual.")
    # Tema + narasi makro minggu ini
    nar = (weekly_narrative or {}).get(ws)
    if nar:
        if nar.get("theme"):
            st.markdown(f"**Tema: {nar['theme']}**")
        if nar.get("macro"):
            st.markdown(f"**Makro:** {nar['macro']}")
        for m in nar.get("micro", []):
            st.markdown(f"- **{m.get('title', '')}:** {m.get('text', '')}")
    else:
        st.markdown("_Narasi makro minggu ini belum ditulis._")
    # Cross-check xaudaily (CC BY 4.0 — atribusi wajib)
    st.markdown("### Cross-check xaudaily")
    st.markdown(f"Sumber: Gold Data Reading · XAU Daily (xaudaily.com) — "
                f"data {xaudaily_src}.")
    for ln in xaudaily_check_lines(xaudaily_data, actuals):
        st.markdown(f"- {ln}")
    # Pra-rilis: semua event minggu ini, BERNILAI TINGGI di atas
    tier_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "none": 3}
    week_evs = sorted(
        filt(week_events),
        key=lambda e: tier_rank[strength_label(db_title_for(e.get("title") or ""))[0]])
    if week_evs:
        st.markdown("### Pra-rilis — semua event minggu ini")
        for e in week_evs:
            dbt = db_title_for(e.get("title") or "")
            dt = parse_utc(e.get("event_time_utc"))
            actual_num, _fc_num, _ = event_actual(e, actuals)
            st.markdown(f"**{e.get('title')}** — {fmt_dt_wita(dt) if dt else '—'} · "
                        f"{strength_line(dbt)}")
            if actual_num is not None:
                st.markdown(f"Sudah rilis: actual "
                            f"**{fmt_actual(actual_num, e.get('forecast'))}** "
                            f"vs forecast {esc(e.get('forecast') or '—')}.")
            else:
                st.markdown(f"Konsensus **{esc(e.get('forecast') or 'belum terbit')}** "
                            f"vs previous {esc(e.get('previous') or '—')}. "
                            f"{scenario_text(dbt)}")
                wl = nowcast_week_line(cleveland_data, dbt, dt, _fc_num)
                if wl:
                    st.markdown(wl)
    # Pasca-rilis: tabel + bacaan gabungan
    if done:
        lines = ["| Event | Actual | Forecast | ±1 jam |",
                 "|---|---|---|---|"]
        for r in done:
            ret = f"{r['ret60']:+.2f}%" if r["ret60"] is not None else "—"
            fc = f"{r['forecast']:g}" if r["forecast"] is not None else "—"
            act = f"{r['actual']:g}" if r["actual"] is not None else "—"
            lines.append(f"| {r['title']} | {act} | {fc} | {ret} |")
        st.markdown("\n".join(lines))
        st.caption("Respon ±1 jam = GC=F, proksi XAUUSD · N kecil = data awal, "
                   "belum stabil.")
        read = combined_read(rows, "minggu ini")
        if read:
            st.markdown("---")
            st.markdown(read)
    else:
        st.info("Belum ada rilis minggu ini yang tercatat actual-nya di DB.")
    st.markdown("---")
    if week_ahead:
        st.markdown(wsj_table_markdown(week_ahead, actuals), unsafe_allow_html=True)
    else:
        st.info("Tabel WSJ minggu depan belum tersedia — file week_ahead.json "
                "belum ada.")


@st.dialog("🗓️ Analisa Bulanan", width="large")
def dialog_bulanan():
    st.markdown(f"**{BULAN[now_wita.month-1]} {now_wita.year}** — bulan dipecah "
                f"jadi minggu Senin–Minggu. Alasan per minggu hanya dari angka "
                f"terverifikasi; jendela yang belum terukur tampil kosong.")
    first = now_wita.replace(day=1)
    if now_wita.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last_day = next_first - timedelta(days=1)
    cur = first - timedelta(days=first.weekday())
    month_rows_all = []
    while cur <= last_day:
        ws = cur
        we = min(cur + timedelta(days=6), last_day)
        note = monthly_notes.get(ws.isoformat(), {})
        label = note.get("label") or f"Minggu {ws.day}–{we.day} {BULAN[ws.month-1]}"
        with st.expander(f"{label} ({ws.day} {BULAN[ws.month-1]} – "
                         f"{we.day} {BULAN[we.month-1]})"):
            reason = note.get("reason")
            if reason:
                st.markdown(f"**Alasan minggu ini:** {reason}")
            else:
                st.markdown("_Alasan minggu ini belum ditulis — minggu belum tutup "
                            "atau angkanya belum terverifikasi._")
            rows = db_event_rows(ws.isoformat(), we.isoformat())
            month_rows_all.extend(rows)
            read = combined_read(rows, "minggu ini")
            if read:
                st.markdown(read)
            if rows:
                lines = ["| Event | Actual | Forecast | ±1 jam | ±4 jam |",
                         "|---|---|---|---|---|"]
                for r in rows:
                    r60 = f"{r['ret60']:+.2f}%" if r["ret60"] is not None else "—"
                    r240 = f"{r['ret240']:+.2f}%" if r["ret240"] is not None else "—"
                    fc = f"{r['forecast']:g}" if r["forecast"] is not None else "—"
                    act = f"{r['actual']:g}" if r["actual"] is not None else "—"
                    lines.append(f"| {r['title']} | {act} | {fc} | {r60} | {r240} |")
                st.markdown("\n".join(lines))
            else:
                st.markdown("_Belum ada event tercatat minggu ini._")
        cur += timedelta(days=7)
    # Penutup bulan: arah bersih + event paling berpengaruh
    st.markdown("---")
    st.markdown(f"### Penutup bulan — {BULAN[now_wita.month-1]} {now_wita.year}")
    read_m = combined_read(month_rows_all, "bulan ini")
    if read_m:
        st.markdown(read_m)
        measured = [r for r in month_rows_all if r["ret60"] is not None]
        if measured:
            big = max(measured, key=lambda r: abs(r["ret60"]))
            st.markdown(f"Event paling berpengaruh bulan ini: **{big['title']}** "
                        f"(gerak emas ±1 jam {big['ret60']:+.2f}%, "
                        f"actual {big['actual']:g} vs forecast {big['forecast']:g}).")
    else:
        st.markdown("_Belum ada rilis bulan ini yang tercatat actual-nya._")
    macro = xaudaily_macro_line(xaudaily_data)
    if macro:
        st.markdown(macro)


col_d1, col_d2, col_d3 = st.columns(3)
with col_d1:
    if st.button("📊 Analisa Harian", use_container_width=True):
        dialog_harian()
with col_d2:
    if st.button("📈 Analisa Mingguan", use_container_width=True):
        dialog_mingguan()
with col_d3:
    if st.button("🗓️ Analisa Bulanan", use_container_width=True):
        dialog_bulanan()

tab1, tab2, tab3, tab4 = st.tabs([
    f"Hari ini ({len(filt(today_events))})",
    f"Minggu ini ({len(filt(week_events))})",
    f"Bulan ini ({len(filt(month_events))})",
    "Suara Fed",
])

with tab1:
    evs = filt(today_events)
    if not evs:
        st.info("Tidak ada event USD medium/high impact hari ini.")
    render_event_list(evs, now_utc, actuals, typical, surprises, speech_map, speaker_stats)

with tab2:
    evs = filt(week_events)
    if not evs:
        st.info("Tidak ada event minggu ini di kalender.")
    render_event_list(evs, now_utc, actuals, typical, surprises, speech_map, speaker_stats)
    # Tabel WSJ minggu depan (bila file-nya memang untuk minggu depan)
    if week_ahead and (week_ahead.get("week_start") or "") > week_end.isoformat():
        st.markdown("---")
        st.markdown(wsj_table_markdown(week_ahead, actuals), unsafe_allow_html=True)
    if next_week_events:
        src_lbl = "TradingView" if any(e.get("src") == "TV" for e in next_week_events) else "Finnhub"
        with st.expander(f"🔭 Minggu depan ({len(filt(next_week_events))} event, via {src_lbl})"):
            render_event_list(filt(next_week_events), now_utc, actuals, typical, surprises, speech_map, speaker_stats)

with tab3:
    evs = filt(month_events)
    if not evs:
        st.info("Belum ada event tercatat bulan ini.")
    render_event_list(evs, now_utc, actuals, typical, surprises, speech_map, speaker_stats)

with tab4:
    render_fed_tab(fed_data, fed_src, all_events, now_utc,
                   actuals, xaudaily_data)

st.markdown(
    '<div class="foot">'
    '🟢 Actual hijau = kabar baik untuk ekonomi AS · '
    '⚪ putih = sesuai ekspektasi · '
    '🔴 merah = kabar buruk untuk ekonomi AS<br>'
    '(logika dibalik untuk Unemployment Claims & data inflasi)<br><br>'
    'Sumber kalender: ForexFactory feed (unofficial, gratis) + TradingView Economic Calendar (publik, untuk minggu depan) · '
    'Konsensus minggu depan: WSJ Data Week Ahead (median survei, jumlah ekonom dalam kurung) · '
    'Respon historis memakai <b>GC=F</b> sebagai proksi XAUUSD · '
    'N&lt;5 = data awal, belum stabil<br>'
    'Cross-check: XAU Daily (xaudaily.com, CC BY 4.0) — nilai revisi terbaru, '
    'bukan first print<br>'
    'Nowcast: Federal Reserve Bank of Cleveland, Inflation Nowcasting — '
    'model estimate, bukan konsensus<br>'
    'Pidato Fed: Board of Governors (federalreserve.gov) — skor kata kunci, '
    'bukan penilaian LLM · hanya gubernur<br>'
    'Angka = hasil ukur, bukan ramalan · data, bukan sinyal.'
    '</div>',
    unsafe_allow_html=True,
)
