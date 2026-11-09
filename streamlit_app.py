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
"""
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
    rows = db_event_rows(today_wita.isoformat(), today_wita.isoformat())
    measured = [r for r in rows if r["ret60"] is not None]
    st.markdown(f"**{HARI[today_wita.weekday()]}, {today_wita.day} {BULAN[today_wita.month-1]} "
                f"{today_wita.year}** — {len(today_events)} event USD medium/high, "
                f"{len(measured)} sudah terukur respon ±1 jamnya.")
    upcoming_today = [e for e in upcoming
                      if _wita_d(e) == today_wita and e.get("impact") == "High"]
    if upcoming_today:
        nx = upcoming_today[0]
        st.info(f"📌 Sebelum {fmt_dt_wita(parse_utc(nx['event_time_utc']))}: "
                f"snapshot kolam Tahap 2 (pool disapu + pool target, toleransi "
                f"0,25×ATR) dari chart-mu sendiri — gerakan pertama pasca-rilis "
                f"dibaca sebagai sweep, bukan keputusan.")
    render_event_list(filt(today_events), now_utc, actuals, typical, surprises,
                      speech_map, speaker_stats)


@st.dialog("📈 Analisa Mingguan", width="large")
def dialog_mingguan():
    ws, we = week_start.isoformat(), week_end.isoformat()
    rows = db_event_rows(ws, we)
    done = [r for r in rows if r["actual"] is not None]
    st.markdown(f"**Minggu {week_start.day}–{week_end.day} "
                f"{BULAN[week_start.month-1]} {week_start.year}** — "
                f"{len(rows)} event tercatat di DB, {len(done)} sudah rilis "
                f"dengan actual.")
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

tab1, tab2, tab3 = st.tabs([
    f"Hari ini ({len(filt(today_events))})",
    f"Minggu ini ({len(filt(week_events))})",
    f"Bulan ini ({len(filt(month_events))})",
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
    'Angka = hasil ukur, bukan ramalan · data, bukan sinyal.'
    '</div>',
    unsafe_allow_html=True,
)
