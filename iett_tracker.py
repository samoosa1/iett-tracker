#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iett_tracker.py — Istanbul İETT bus tracker using İBB's official public SOAP API.

Official, documented endpoints (İBB Open Data Portal — data.ibb.gov.tr,
dataset "iett-ibb-web-servisi"):

  https://api.ibb.gov.tr/iett/ibb/ibb.asmx?wsdl
      DurakDetay_GYY(hat_kodu)        -> ordered stops of a line (per direction)
  https://api.ibb.gov.tr/iett/UlasimAnaVeri/HatDurakGuzergah.asmx?wsdl
      GetDurak_json(DurakKodu)        -> stop info ("" = ALL stops, slow!)
      GetHat_json(HatKodu)            -> line info
  https://api.ibb.gov.tr/iett/FiloDurum/SeferGerceklesme.asmx?wsdl
      GetHatOtoKonum_json(HatKodu)    -> live bus positions on a line

Notes:
  * The İETT SOAP services go OFFLINE nightly after ~00:15 Istanbul time.
  * GetDurak_json("") (all ~40k stops) is slow; results are cached locally
    for 7 days in ~/.iett_stops_cache.json (use --refresh-cache to force).

Dependencies:  pip install requests rich
  (Raw SOAP over requests — no zeep — because İBB's WSDL is malformed and
  trips up WSDL-parsing clients.)

Usage:
  iett_tracker.py                          interactive menu
  iett_tracker.py --defaults               live view of your default routes
                                           (522 -> Mecidiyeköy, M7 -> Nurtepe)
  iett_tracker.py --watch-defaults         same, refreshing every 30 s
  iett_tracker.py --search çakmak          find stop codes by name
  iett_tracker.py --stops 522              all stops on line 522
  iett_tracker.py --live 522               live buses on line 522
  iett_tracker.py --live 522 -d mecidiyekoy  ...only direction toward Mecidiyeköy
  iett_tracker.py --watch 522              refresh live view every 30 s
  iett_tracker.py --stop 123456            buses approaching a stop
  iett_tracker.py --stop çakmak            ...or a MY_STOPS alias
  iett_tracker.py --watch-stop 123456 --interval 20
"""

import argparse
import json
import math
import re
import sys
import time
import unicodedata
import sqlite3
import statistics
import http.server
import socketserver
import threading
import logging
from logging.handlers import RotatingFileHandler
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

# ──────────────────────────────────────────────────────────────────────────────
# YOUR SAVED STOPS — fill in codes you find with `--search NAME`
# (keys are aliases you can pass to --stop / --watch-stop)
# ──────────────────────────────────────────────────────────────────────────────
MY_STOPS = {
    # alias        stop code (find with: iett_tracker.py --search çakmak)
    "cakmak":      "",   # e.g. "221681" — Çakmak (Ümraniye), on line 522
    "mecidiyekoy": "",   # e.g. "104761" — Mecidiyeköy
}

# Default routes tracked by --defaults / --watch-defaults / interactive menu.
# "direction" filters live buses by destination text (matched against the
# bus's `yon` field) or by leg code G/D in `guzergahkodu`.
DEFAULT_ROUTES = [
    # 522  ALEMDAĞ - MECİDİYEKÖY MEZARLIK : we want the leg TOWARD Mecidiyeköy
    {"line": "522", "direction": "mecidiyekoy", "label": "522 → Mecidiyeköy"},
    # M7   NURTEPE - MECİDİYEKÖY: the FREE emergency ring İETT runs while the
    # M7 metro's Mecidiyeköy–Çağlayan tunnel is shut (groundwater damage, since
    # 6 Jun 2026). Its Nurtepe-side terminus is the stop "HACI ETHEM ÜKTEM
    # ORTAOKULU" — buses heading there show that as their destination (yön),
    # NOT the word "Nurtepe", so we filter on that instead.
    {"line": "M7", "direction": "haci ethem", "label": "M7 ring → Nurtepe (Hacı Ethem)"},
]

WATCH_DEFAULT_INTERVAL = 30  # seconds

# ──────────────────────────────────────────────────────────────────────────────
# SOAP plumbing
# ──────────────────────────────────────────────────────────────────────────────
BASE = "https://api.ibb.gov.tr/iett"
EP_MAIN = BASE + "/UlasimAnaVeri/HatDurakGuzergah.asmx"   # GetDurak_json, GetHat_json
EP_FLEET = BASE + "/FiloDurum/SeferGerceklesme.asmx"      # GetHatOtoKonum_json
EP_IBB = BASE + "/ibb/ibb.asmx"                           # DurakDetay_GYY
EP_PLAN = BASE + "/UlasimAnaVeri/PlanlananSeferSaati.asmx"  # GetPlanlananSeferSaati_json
SOAP_NS = "http://tempuri.org/"

CACHE_FILE = Path.home() / ".iett_stops_cache.json"
CACHE_TTL = 7 * 24 * 3600

APP_DIR = Path(__file__).resolve().parent    # data lives next to the app, so a
SEG_DB = APP_DIR / "iett_segments.db"        # service finds it under any account
LOG_DIR = APP_DIR / "logs"                   # rotating diagnostic logs live here
BACKUP_DIR = APP_DIR / "backups"             # daily snapshots of the learned DB
BACKUP_INTERVAL = 24 * 3600                   # back up once a day
BACKUP_KEEP = 7                               # keep the last week of snapshots
COLLECT_INTERVAL = 20                         # default seconds between samples

log = logging.getLogger("iett")               # structured diagnostic log (file)


def setup_logging(context: str = "app") -> None:
    """Rotating file log next to the app (logs/iett.log, ~10 MB cap) plus capture
    of uncaught exceptions on the main thread AND worker threads — so a crash
    leaves a diagnosable trail instead of a blank window. Idempotent."""
    if getattr(setup_logging, "_done", False):
        return
    setup_logging._done = True
    try:
        LOG_DIR.mkdir(exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / "iett.log", maxBytes=2_000_000,
                                backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.setLevel(logging.INFO)
        log.addHandler(h)
        log.propagate = False
    except Exception:
        return   # logging must never stop the app from running

    def _excepthook(et, ev, tb):
        log.critical("UNCAUGHT %s: %s", getattr(et, "__name__", et), ev,
                     exc_info=(et, ev, tb))
        sys.__excepthook__(et, ev, tb)
    sys.excepthook = _excepthook
    try:                                       # Python 3.8+ per-thread hook
        def _thread_excepthook(a):
            if a.exc_type is SystemExit:
                return
            log.critical("UNCAUGHT in thread %s: %s: %s", a.thread.name,
                         a.exc_type.__name__, a.exc_value,
                         exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
        threading.excepthook = _thread_excepthook
    except Exception:
        pass
    import os as _os
    log.info("=== logging up (%s) pid=%s python=%s ===", context, _os.getpid(),
             sys.version.split()[0])
MIN_SAMPLES = 3                               # min observations before a hop is trusted
BAND_CORR_INFLATE = 1.4                        # widen the quadrature ETA band a bit to
                                              # allow for correlated congestion on bad runs
# Bias correction. Median-based ETAs run optimistic (right-skewed travel times).
# Two learned corrections, both measured against actual arrivals and re-checked by
# /accuracy (a working correction drives the residual toward 0):
#   1. Time-of-day floor: a flat per-bucket offset (the base optimism), min to add.
#   2. Horizon ramp (14 Jul 2026, 40k matched 'history' arrivals): the post-floor
#      residual is ~0 up to 6 min out, then climbs to ~+4 min by 16 min out as
#      segment errors compound. _horizon_bias adds most of that back but stays
#      under the measured residual (leaves a hair of optimism, the safe side for
#      catching a bus) and caps past 16 min instead of chasing the headway-bound
#      long tail (the confidence band carries that uncertainty).
# The 'estimate' distance/speed fallback runs the OTHER way, ~1.8 min pessimistic
# (4.6k arrivals, 14 Jul 2026): showing more time than there is makes you miss the
# bus, so ESTIMATE_BIAS pulls it back. Set BIAS_CORRECT=False to turn all of it off.
# 'origin-wait' likewise ran ~0.9 min pessimistic — measured FLAT (-0.85..-1.0) at
# every lead from 2 to 25 min over 3.0k matched arrivals (20 Jul 2026), so it takes a
# constant, not a ramp like history. Left uncorrected the bus reaches the stop before
# the shown time, which is the miss-your-bus direction. Applied at all leads: past
# ~25 min origin-wait error is dominated by departure-slot uncertainty (+11 min) where
# this shift is noise, and it errs toward "arrives later than shown", the safe side.
BIAS_CORRECT = True
BIAS_OFFSETS = {"weekday-daytime": 0.9, "weekday-rush": 1.4, "weekend": 0.0}  # base min to add
ESTIMATE_BIAS = -1.7                          # min added to 'estimate' ETAs (show sooner)
ORIGIN_WAIT_BIAS = -0.9                       # min added to 'origin-wait' ETAs (show sooner)
HORIZON_BIAS_ANCHORS = [(6, 0.0), (8, 1.9), (10, 2.6), (13, 3.3), (16, 3.7)]  # (lead_min, +min)
# İBB caps the live-position service at 100 req/HOUR (see LIVE_TTL note), so we
# can't truly "sample fast" — the cache shares one upstream call per ~40 s. These
# intervals govern the collector's local cadence; rush is a touch denser, off-peak
# gentler, both safely under the limit. Windows are local-hour [start, end).
RUSH_WINDOWS = [(7, 10), (17, 20)]            # 07:00–09:59 and 17:00–19:59
RUSH_INTERVAL = 40                            # seconds between samples during rush
OFFPEAK_INTERVAL = 40                         # seconds between samples otherwise
                                              # (=LIVE_TTL/rate-limit floor; the 522
                                              # owns the whole İBB budget now that the
                                              # fleet/34G loggers are off)
# When calls fail (İBB down OR our IP is rate-limit-penalised), go QUIET fast and
# long: failed pokes still count against the rolling quota and keep a penalty
# alive, so the only way to recover is to stop hitting it. Exponential up to 15 min.
COLLECT_BACKOFF_MAX = 900


def collect_interval_now() -> int:
    h = datetime.now().hour
    return RUSH_INTERVAL if any(a <= h < b for a, b in RUSH_WINDOWS) else OFFPEAK_INTERVAL


def collect_sleep_secs(present: bool, empties: int) -> int:
    """Adaptive poll interval. Buses running → normal rush/off-peak cadence. None
    running (night/service gap) or İBB down → growing backoff (capped), so we stop
    pounding the flaky gateway with calls that can't collect anything."""
    if not present:   # exponential cooldown 60→120→240→480→900s — go quiet to let
        return min(COLLECT_BACKOFF_MAX, 60 * (2 ** min(empties, 6)))   # a penalty drain
    return collect_interval_now()
# İBB hourly traffic-density data (geohash speed by hour) — used to build a
# time-of-day congestion prior. ~135 MB/month. The portal package lists one CSV
# per month; we resolve the newest at run time and fall back to this if offline.
TRAFFIC_PACKAGE = "hourly-traffic-density-data-set"
TRAFFIC_CSV_URL = ("https://data.ibb.gov.tr/dataset/3ee6d744-5da2-40c8-9cd6-"
                   "0e3e41f1928f/resource/57cb067b-1a0b-460b-8342-7884bd4537e8/"
                   "download/traffic_density_202501.csv")
_GEOHASH_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"

import os as _osmod, hashlib as _hashlib, secrets as _secrets
# Remote self-update — token comes from the environment only, with NO baked-in default,
# so a public checkout can never carry a working one. The endpoint is off regardless.
UPDATE_TOKEN = _osmod.environ.get("IETT_UPDATE_TOKEN", "")
UPDATE_ENABLED = False               # /update OFF — no RCE surface on a public URL; deploy via SSH
# HTTP Basic Auth — the ONLY thing protecting the app when exposed publicly (Funnel).
# Resolution order: env IETT_PASS → a gitignored `.auth_secret` beside this file →
# generate one and persist it there. The secret therefore never lives in synced code,
# and a fresh clone still comes up secured without any manual setup.
AUTH_USER = _osmod.environ.get("IETT_USER", "bus")
_SECRET_PATH = _osmod.path.join(_osmod.path.dirname(_osmod.path.abspath(__file__)),
                                ".auth_secret")


def _resolve_auth_pass() -> str:
    pw = _osmod.environ.get("IETT_PASS", "").strip()
    if pw:
        return pw
    try:
        with open(_SECRET_PATH, encoding="utf-8") as fh:
            pw = fh.read().strip()
        if pw:
            return pw
    except OSError:
        pass
    pw = _secrets.token_urlsafe(12)
    try:
        with open(_SECRET_PATH, "w", encoding="utf-8") as fh:
            fh.write(pw + "\n")
        print("[auth] generated a login password, saved to %s" % _SECRET_PATH)
    except OSError:
        print("[auth] WARNING: could not write %s — password will change on restart"
              % _SECRET_PATH)
    return pw


AUTH_PASS = _resolve_auth_pass()
# Session-cookie value set by the custom /login form (so we don't use the ugly
# native Basic-Auth dialog). Derived from the password, never sent as the password.
AUTH_COOKIE = _hashlib.sha256(("iett-session::" + AUTH_PASS).encode()).hexdigest()
SOAP_RETRIES = 3       # attempts per SOAP call before giving up
SOAP_BACKOFF = 0.8     # seconds, grows linearly per retry

console = Console()
_session = requests.Session()
_session.headers["User-Agent"] = "iett-tracker/1.0 (personal use)"


def soap_post(endpoint: str, method: str, **params) -> str:
    """POST a raw SOAP 1.1 envelope and return the response body text.

    We talk SOAP by hand (rather than via a WSDL client) because İBB's WSDLs
    reference message definitions that don't exist, which breaks strict and
    non-strict WSDL parsers alike. The .asmx operations themselves are fine.
    """
    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in params.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{method} xmlns="{SOAP_NS}">{inner}</{method}></soap:Body>'
        '</soap:Envelope>'
    )
    headers = {"Content-Type": "text/xml; charset=utf-8",
               "SOAPAction": SOAP_NS + method}
    # İBB's gateway throws transient 5xx / "Policy Falsified" faults constantly;
    # a couple of quick retries smooth over the short blips.
    last = None
    for attempt in range(SOAP_RETRIES):
        try:
            resp = _session.post(endpoint, data=envelope.encode("utf-8"),
                                 headers=headers, timeout=30)
            resp.raise_for_status()
            if "<soap:Fault" in resp.text or "<faultstring" in resp.text:
                raise RuntimeError("SOAP Fault: " + resp.text[:200])
            return resp.text
        except Exception as exc:
            last = exc
            if attempt < SOAP_RETRIES - 1:
                time.sleep(SOAP_BACKOFF * (attempt + 1))
    fail(f"{method}({params}) on {endpoint}", last)


def soap_json(endpoint: str, method: str, **params) -> list:
    """Call a `*_json` operation that returns a JSON string in its *Result."""
    root = ET.fromstring(soap_post(endpoint, method, **params).encode("utf-8"))
    for el in root.iter():
        if strip_ns(el.tag) == f"{method}Result":
            return parse_json(el.text)
    return []


def soap_dataset(endpoint: str, method: str, **params) -> list:
    """Call a `*_GYY` operation that returns a .NET DataSet; extract row dicts."""
    root = ET.fromstring(soap_post(endpoint, method, **params).encode("utf-8"))
    rows = []
    for node in root.iter():
        if strip_ns(node.tag) in ("schema", "diffgram", "NewDataSet", "DataSet"):
            continue
        children = list(node)
        if children and all(len(c) == 0 for c in children):
            rows.append({strip_ns(c.tag): (c.text or "").strip() for c in children})
    return rows


# When True (set by the map server), fail() raises instead of printing a big
# red panel + exiting — so an İBB outage doesn't spam the console every poll.
_quiet_fail = False


def fail(what: str, exc: Exception):
    if _quiet_fail:
        raise RuntimeError(f"{what}: {type(exc).__name__}: {exc}")
    console.print(Panel(
        f"[bold red]İBB API call failed:[/] {what}\n"
        f"[red]{type(exc).__name__}:[/] {exc}\n\n"
        "[dim]Hints: İETT SOAP services shut down nightly after ~00:15 TR time; "
        "they can also be slow/flaky at peak hours. Check connectivity to "
        "api.ibb.gov.tr and retry.[/dim]",
        title="error", border_style="red"))
    sys.exit(1)


def parse_json(payload) -> list:
    """The *_json methods return a JSON string."""
    if payload is None:
        return []
    try:
        data = json.loads(payload)
        return data if isinstance(data, list) else [data]
    except (TypeError, ValueError):
        return []


def strip_ns(tag) -> str:
    return tag.split("}", 1)[1] if isinstance(tag, str) and "}" in tag else str(tag)


# ──────────────────────────────────────────────────────────────────────────────
# Turkish-aware text matching
# ──────────────────────────────────────────────────────────────────────────────
_TR = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiIoOsSuU")


def norm(text: str) -> str:
    if text is None:
        return ""
    text = str(text).translate(_TR)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def direction_match(bus: dict, wanted: str) -> bool:
    """Match a live-bus row against a wanted direction.

    `wanted` may be destination text (e.g. 'mecidiyekoy', matched against the
    bus destination field `yon`) or a leg code 'G'/'D' (matched against
    `guzergahkodu` like '522_G_D0')."""
    if not wanted:
        return True
    w = norm(wanted)
    if w in ("g", "d"):
        return f"_{w.upper()}_" in (bus.get("guzergahkodu") or "").upper()
    return w in norm(bus.get("yon", "")) or w in norm(bus.get("guzergahkodu", ""))


# ──────────────────────────────────────────────────────────────────────────────
# API wrappers
# ──────────────────────────────────────────────────────────────────────────────
# İBB's official doc rate-limits the SeferGerceklesme (live-position) service to
# 100 requests/HOUR — one per 36 s. Exceeding it triggers "Policy Falsified"
# gateway rejections (much of our "outage" pain). So we cache a successful live
# fetch for 40 s and SHARE it across every consumer (collector + all map viewers):
# upstream İBB is hit at most ~90×/hour regardless of how fast anyone polls locally.
LIVE_TTL = 40                     # seconds to reuse a successful live fetch (rate limit!)
LIVE_FAIL_TTL = 20                # seconds to remember a failure (avoid retry storms)
_live_cache: dict = {}            # line -> (epoch, ok, buses_or_exc)
_live_lock = threading.Lock()
_live_health: list = []           # (epoch, ok) of ACTUAL upstream fetches, last hour


def _record_live(ok: bool):
    """Track real upstream live-fetch outcomes (cache hits excluded) so /stats can
    show the true İBB success rate — i.e. whether the rate-limit fix is working."""
    now = time.time()
    _live_health.append((now, ok))
    cut = now - 3600
    while _live_health and _live_health[0][0] < cut:
        _live_health.pop(0)


def live_health() -> dict:
    n = len(_live_health)
    ok = sum(1 for _, o in _live_health if o)
    return {"calls_1h": n, "ok_1h": ok, "fail_1h": n - ok,
            "fail_pct_1h": round(100 * (n - ok) / n, 1) if n else None}


def api_live_buses(line: str) -> list:
    """Live positions of all buses on a line. Fields: kapino, boylam, enlem,
    hatkodu, guzergahkodu, hatad, yon, son_konum_zamani, yakinDurakKodu.

    Cached so the map's /positions, /eta and the collector share one upstream
    call per refresh instead of hammering İBB's flaky fleet API. Failures are
    cached briefly too, so during an outage we don't re-retry on every consumer."""
    with _live_lock:
        ent = _live_cache.get(line)
        if ent:
            age = time.time() - ent[0]
            if ent[1] and age < LIVE_TTL:
                return ent[2]
            if not ent[1] and age < LIVE_FAIL_TTL:
                raise ent[2]
    try:
        buses = soap_json(EP_FLEET, "GetHatOtoKonum_json", HatKodu=line)
    except (Exception, SystemExit) as exc:
        err = exc if isinstance(exc, Exception) else RuntimeError(str(exc) or "down")
        with _live_lock:
            _live_cache[line] = (time.time(), False, err)
        _record_live(False)
        raise
    with _live_lock:
        _live_cache[line] = (time.time(), True, buses)
    _record_live(True)
    return buses


def api_line_info(line: str) -> list:
    return soap_json(EP_MAIN, "GetHat_json", HatKodu=line)


def api_stop_info(code: str) -> list:
    return soap_json(EP_MAIN, "GetDurak_json", DurakKodu=code)


_plan_cache: dict = {}


def api_planned_times(line: str) -> list:
    """Scheduled departure times (timetable) from the line's origin. Rows:
    SYON (G/D leg), SGUNTIPI (I=weekday, C=Saturday, P=Sunday), DT ('06:00').
    On the healthy UlasimAnaVeri host, so usable even when live tracking is down.
    Cached 12 h (the timetable rarely changes)."""
    ent = _plan_cache.get(line)
    if ent and (time.time() - ent[0]) < 43200:
        return ent[1]
    rows = soap_json(EP_PLAN, "GetPlanlananSeferSaati_json", HatKodu=line)
    _plan_cache[line] = (time.time(), rows)
    return rows


def api_line_stops(line: str) -> list:
    """Ordered stops of a line (both directions) via ibb.asmx DurakDetay_GYY.
    Tries the historical parameter names in case İBB renamed them."""
    for param in ("hat_kodu", "HatKodu", "hatkodu"):
        rows = soap_dataset(EP_IBB, "DurakDetay_GYY", **{param: line})
        if rows:
            return rows
    return []


def all_stops(refresh: bool = False) -> list:
    """All İETT stops, cached locally (the full download is slow)."""
    if not refresh and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            try:
                return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except ValueError:
                pass
    console.print("[dim]Downloading full stop list from İBB (one-off, slow — "
                  "cached for 7 days)...[/dim]")
    stops = api_stop_info("")
    if stops:
        CACHE_FILE.write_text(json.dumps(stops, ensure_ascii=False), encoding="utf-8")
    return stops


def resolve_stop(code_or_alias: str) -> str:
    key = norm(code_or_alias)
    if key in {norm(k): k for k in MY_STOPS} and not code_or_alias.isdigit():
        code = MY_STOPS[{norm(k): k for k in MY_STOPS}[key]]
        if not code:
            console.print(f"[yellow]MY_STOPS['{code_or_alias}'] is empty — find the "
                          f"code with --search and fill it in at the top of the "
                          f"script.[/yellow]")
            sys.exit(2)
        return code
    return code_or_alias


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Compass bearing (0=N, 90=E) from point 1 to point 2."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dl = math.radians(float(lon2) - float(lon1))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_coords_cache: dict = {}


def stop_coords(line: str) -> dict:
    """{stop_code: (lat, lon)} for a line, cached 1 h."""
    ent = _coords_cache.get(line)
    if ent and (time.time() - ent[0]) < 3600:
        return ent[1]
    out = {}
    for r in cached_route(line):
        try:
            out[str(r.get("DURAKKODU"))] = (float(r["YKOORDINATI"]), float(r["XKOORDINATI"]))
        except (TypeError, ValueError, KeyError):
            continue
    _coords_cache[line] = (time.time(), out)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────
def cmd_search(name: str, refresh: bool = False):
    stops = all_stops(refresh)
    q = norm(name)
    hits = [s for s in stops if q in norm(s.get("SDURAKADI", ""))]
    if not hits:
        console.print(f"[yellow]No stop matching '{name}'.[/yellow]")
        return
    t = Table(title=f"Stops matching “{name}” ({len(hits)})", box=box.SIMPLE_HEAVY)
    for col in ("Code", "Name", "District", "Direction", "Type"):
        t.add_column(col)
    for s in hits[:80]:
        t.add_row(str(s.get("SDURAKKODU", "")), s.get("SDURAKADI", ""),
                  s.get("ILCEADI", ""), s.get("SYON", ""), s.get("DURAK_TIPI", ""))
    console.print(t)
    if len(hits) > 80:
        console.print(f"[dim]...and {len(hits) - 80} more. Narrow your search.[/dim]")


def cmd_stops(line: str):
    rows = api_line_stops(line)
    if not rows:
        console.print(f"[yellow]No stops returned for line {line} "
                      f"(DurakDetay_GYY on {WSDL_IBB}).[/yellow]")
        return
    info = api_line_info(line)
    title = f"Line {line}"
    if info:
        title += f" — {info[0].get('HAT_UZUN_ADI') or info[0].get('HATADI', '')}"
    for leg in ("G", "D"):
        leg_rows = [r for r in rows if (r.get("YON") or "").upper().startswith(leg)]
        if not leg_rows:
            continue
        leg_rows.sort(key=lambda r: int(r.get("SIRANO") or 0))
        t = Table(title=f"{title}  [direction {leg}: "
                        f"{'outbound (gidiş)' if leg == 'G' else 'return (dönüş)'}]",
                  box=box.SIMPLE)
        t.add_column("#", justify="right")
        t.add_column("Stop code")
        t.add_column("Stop name")
        t.add_column("District")
        for r in leg_rows:
            t.add_row(r.get("SIRANO", ""), r.get("DURAKKODU", ""),
                      r.get("DURAKADI", ""), r.get("ILCEADI", ""))
        console.print(t)


def live_table(line: str, direction: str | None, label: str | None = None) -> Table:
    buses = [b for b in api_live_buses(line) if direction_match(b, direction or "")]
    title = label or f"Line {line}" + (f" → {direction}" if direction else "")
    t = Table(title=f"{title}  —  {len(buses)} bus(es)  "
                    f"[dim]{datetime.now():%H:%M:%S}[/dim]", box=box.SIMPLE_HEAVY)
    for col in ("Door no", "Destination (yön)", "Route leg", "Near stop",
                "Lat", "Lon", "Last seen"):
        t.add_column(col)
    for b in buses:
        t.add_row(b.get("kapino", ""), b.get("yon", ""), b.get("guzergahkodu", ""),
                  str(b.get("yakinDurakKodu", "")),
                  f"{float(b['enlem']):.5f}" if b.get("enlem") else "",
                  f"{float(b['boylam']):.5f}" if b.get("boylam") else "",
                  (b.get("son_konum_zamani") or "")[-8:])
    if not buses:
        t.add_row(*(["—"] * 7))
    return t


def cmd_live(line: str, direction: str | None = None):
    console.print(live_table(line, direction))


def cmd_watch(line: str, direction: str | None, interval: int):
    watch_loop(lambda: [live_table(line, direction)], interval)


def cmd_defaults():
    for r in DEFAULT_ROUTES:
        console.print(live_table(r["line"], r["direction"], r["label"]))


def cmd_watch_defaults(interval: int):
    watch_loop(lambda: [live_table(r["line"], r["direction"], r["label"])
                        for r in DEFAULT_ROUTES], interval)


def watch_loop(render_tables, interval: int):
    from rich.live import Live
    from rich.console import Group
    try:
        with Live(Group(*render_tables()), console=console,
                  refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                live.update(Group(*render_tables()))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


_route_cache: dict = {}


def cached_route(line: str) -> list:
    """Raw route rows for a line, cached 1 h (DurakDetay_GYY is a 48 KB call)."""
    ent = _route_cache.get(line)
    if ent and (time.time() - ent[0]) < 3600:
        return ent[1]
    rows = api_line_stops(line)
    if rows:
        _route_cache[line] = (time.time(), rows)
    return rows


def approaching(stop_code: str, lines: list) -> list:
    """Buses approaching `stop_code` on the given lines, derived from official
    data: route stop order (DurakDetay_GYY) × live positions (GetHatOtoKonum)."""
    out = []
    for line in lines:
        route = cached_route(line)
        # order lookup per direction: {('G', stopcode): (sirano, name, x, y)}
        idx = {}
        for r in route:
            leg = (r.get("YON") or "").upper()[:1]
            try:
                idx[(leg, str(r.get("DURAKKODU")))] = (
                    int(r.get("SIRANO") or 0), r.get("DURAKADI", ""),
                    r.get("XKOORDINATI") or r.get("KOORDINATX"),
                    r.get("YKOORDINATI") or r.get("KOORDINATY"))
            except (TypeError, ValueError):
                continue
        targets = {leg: v for (leg, code), v in idx.items() if code == str(stop_code)}
        if not targets:
            continue  # this line doesn't serve the stop
        for b in api_live_buses(line):
            leg = "G" if "_G_" in (b.get("guzergahkodu") or "") else \
                  "D" if "_D_" in (b.get("guzergahkodu") or "") else ""
            if leg not in targets:
                continue
            near = idx.get((leg, str(b.get("yakinDurakKodu"))))
            if not near:
                continue
            stops_away = targets[leg][0] - near[0]
            if stops_away < 0:
                continue  # already passed the stop
            dist = None
            tx, ty = targets[leg][2], targets[leg][3]
            if b.get("enlem") and b.get("boylam") and tx and ty:
                try:  # XKOORDINATI=lon, YKOORDINATI=lat in İETT data
                    dist = haversine_km(b["enlem"], b["boylam"], ty, tx)
                except (TypeError, ValueError):
                    dist = None
            out.append({"line": line, "kapino": b.get("kapino", ""),
                        "yon": b.get("yon", ""), "stops_away": stops_away,
                        "near_name": near[1], "dist_km": dist,
                        "seen": (b.get("son_konum_zamani") or "")[-8:],
                        "stop_name": targets[leg][1], "leg": leg,
                        "near_sira": near[0], "target_sira": targets[leg][0]})
    out.sort(key=lambda x: (x["stops_away"],
                            x["dist_km"] if x["dist_km"] is not None else 99))
    return out


def approaching_table(stop_code: str, lines: list) -> Table:
    rows = approaching(stop_code, lines)
    name = rows[0]["stop_name"] if rows else stop_code
    t = Table(title=f"Approaching stop {stop_code} ({name})  —  lines "
                    f"{', '.join(lines)}  [dim]{datetime.now():%H:%M:%S}[/dim]",
              box=box.SIMPLE_HEAVY)
    for col in ("Line", "Bus", "Destination", "Stops away", "Currently near",
                "Dist (km)", "Last seen"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["line"], r["kapino"], r["yon"], str(r["stops_away"]),
                  r["near_name"],
                  f"{r['dist_km']:.1f}" if r["dist_km"] is not None else "?",
                  r["seen"])
    if not rows:
        t.add_row(*(["—"] * 7))
    return t


def cmd_stop(stop_code: str, lines: list):
    console.print(approaching_table(stop_code, lines))


def cmd_watch_stop(stop_code: str, lines: list, interval: int):
    watch_loop(lambda: [approaching_table(stop_code, lines)], interval)


def cmd_dump_methods():
    """The operations this tool uses, per endpoint. (We no longer parse the
    WSDLs — they're malformed — so this is the curated, working set.)"""
    known = {
        EP_MAIN: ["GetDurak_json(DurakKodu)", "GetHat_json(HatKodu)"],
        EP_FLEET: ["GetHatOtoKonum_json(HatKodu)"],
        EP_IBB: ["DurakDetay_GYY(hat_kodu)"],
    }
    for endpoint, ops in known.items():
        console.print(f"\n[bold]{endpoint}[/bold]")
        for op in ops:
            console.print(f"  {op}")


# ──────────────────────────────────────────────────────────────────────────────
# Live map (real location tracking, on an OpenStreetMap canvas)
# ──────────────────────────────────────────────────────────────────────────────
# Browsers can't call the İBB SOAP API directly (no CORS), so the script runs a
# tiny local web server: it serves the map page and a /positions JSON endpoint
# that proxies live İBB data. The page polls that endpoint and moves the markers.
# ── YOUR ROUTE ────────────────────────────────────────────────────────────────
# Everything below is read from `config.json` beside this file, if it exists.
# Without one, the built-in defaults (the 522 this was written for) are used, so
# the app runs out of the box. To make it YOUR line:
#
#   1. python iett_tracker.py --stops 500T     # list a line's stops + codes
#      python iett_tracker.py --search kadikoy # or search stops by name
#   2. cp config.example.json config.json      # then edit it
#
# See config.example.json for every field and what it does.
_DEFAULT_CONFIG = {
    "line": "522",
    "focus": [
        {"line": "522", "stop": "260112", "label": "→ Mecidiyeköy", "at": "board at Ümraniye"},
        {"line": "522", "stop": "302361", "label": "→ Home", "at": "board at Mecidiyeköy"},
    ],
    "eta": {"stop": "260112", "lines": ["522"], "label": "51. ÜMRANİYE"},
    "home_stops": ["260112"],
    "return_stops": ["260111"],
    "dest_hints": ["viyaduk"],
    "line_colors": {"522": "#0a7a52", "M7": "#e67300"},
    "directions": {"522": "g"},
    "app_name": "İETT 522 Tracker",
    "app_short_name": "522",
    "eta_speed_kmh": 18.0,
}
CONFIG_PATH = _osmod.path.join(_osmod.path.dirname(_osmod.path.abspath(__file__)), "config.json")


def _load_config() -> dict:
    """Merge config.json over the built-in defaults. A broken or missing file is
    never fatal: we warn and fall back, because a tracker that refuses to start
    is worse than one running the sample route."""
    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise ValueError("config.json must contain a JSON object")
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print("[config] ignoring %s: %s" % (CONFIG_PATH, exc))
    return cfg


CONFIG = _load_config()
# Hero-card focus points (the "I'm walking to the stop" boarding points). Each is
# a segmented-control option; the card shows that stop's next buses + timetable.
MAP_FOCUS = CONFIG["focus"]
# Live ETA panel: buses approaching this stop, on these lines.
MAP_ETA = CONFIG["eta"]
# Stops to flag specially on the live map (--map):
MAP_HOME_STOPS = set(CONFIG["home_stops"])
MAP_RETURN_STOPS = set(CONFIG["return_stops"])
MAP_DEST_HINTS = tuple(CONFIG["dest_hints"])   # flag stops whose name contains these
# Per-line bus colours on the map (others fall back to a default).
MAP_LINE_COLORS = CONFIG["line_colors"]
# "My way" per line — the direction filter the map uses (so buses going the other
# way are hidden). 'g'/'d' match the route leg; text matches destination. Lines
# NOT listed here are never direction-filtered, which is what you want for a short
# ring route where both directions are useful.
MAP_DIRECTIONS = CONFIG["directions"]
DEFAULT_LINE = str(CONFIG["line"]).upper()
ETA_SPEED_KMH = float(CONFIG["eta_speed_kmh"])  # city-bus average, for the "~min" estimate
# Stops we score prediction accuracy for (the boarding stops we predict ETAs to).
ACCURACY_STOPS = {f["stop"] for f in MAP_FOCUS}

PWA_MANIFEST = json.dumps({
    "name": CONFIG["app_name"], "short_name": CONFIG["app_short_name"], "start_url": "/",
    "display": "standalone", "background_color": "#0e1014", "theme_color": "#0e1014",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}, ensure_ascii=False)

PWA_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#0e1014"/>
<rect x="104" y="138" width="304" height="196" rx="30" fill="#19c37d"/>
<rect x="128" y="168" width="112" height="74" rx="10" fill="#0e1014"/>
<rect x="272" y="168" width="112" height="74" rx="10" fill="#0e1014"/>
<circle cx="178" cy="350" r="30" fill="#0e1014"/><circle cx="334" cy="350" r="30" fill="#0e1014"/>
<text x="256" y="452" font-family="Arial,Helvetica,sans-serif" font-size="96" font-weight="800" fill="#19c37d" text-anchor="middle">522</text>
</svg>"""
ICON_192_B64 = "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"
ICON_512_B64 = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AAAbrUlEQVR4nO3dCbBkV3kf8K/79XtvRiONRmIQSAgJoQ2BMEZCCLMaCRRHoMRUYogpSFx2pSrElB3slEMcx4mduGLsxLGzKYsdQiWVxAYvQi4LcECE1URmkUA4ko0itALanzSaeVt3qnsYMxot8/p29+tzz/f7VY1GKr1+fc693ff733PPPbcTzMXuPXsHNj3AQSsP3tuxLbaXDT5DijzA5ISD2RAApkSxB9g+QsHkBICGFHyAcggE4xMAtkjBB2gPgeDoBICnoOgDtJ8w8MQEgCMo+gD1Ega+QwD4NoUfII8Vtx3mDgCKPgArScNAyk4r/ABkDwKpOqvwA3A0K0mCQIpOKvwAjGul8iBQdecUfgAmtVJpEKiyUwo/ANO2UlkQ6EZlFH8A1JejqybNKPwAbJeVCkYDWt8BhR+AeVlpcRBo9SUAxR+Aedq9Z++grXuglcmlzRscgDqttGw0oHUjAIo/ACXa3bKT01YFgLZtXABy2d2iOtWK4Yo2bVAAaMMlgeJHABR/ANpod+Enr0UHgNI3HgC0tY4VOTxR8gYDgBouCRQ3AqD4A1Cj3YWd3BYVAErbOABQa50rJgCUtFEAoPZ6V0QAKGVjAECWujf3AFDCRgCAbPWvm7nzAJC1Ds4tACj+ADC/ejiXAKD4A8B86+K2BwDFHwDmXx+3NQAo/gBQRp3ctgCg+ANAOfVyWwKA4g8AZdXNmQcAxR8Ayqufc18ICADYfjMNAM7+AaDMOjqzAKD4A0C59XQmAUDxB4Cy66o5AACQ0NQDgLN/AJi+adfXqQYAxR8AZmeadXZqAUDxB4DZm1a9NQcAABKaSgBw9g8A22cadXfiAKD4A8D2m7T+ugQAAAlNFACc/QPA/ExSh40AAEBCjQOAs38AmL+m9bhRAFD8AaAcTeqySwAAkNDYAcDZPwCUZ9z6bAQAABIaKwA4+weAco1Tp40AAEBCWw4Azv4BoHxbrddGAAAgoS0FAGf/ANAeW6nbRgAAIKGjBgBn/wDQPker30YAACAhAQAAEnrKAGD4HwDa66nquBEAAEjoSQOAs38AaL8nq+dGAAAgIQEAABJ6wgBg+B8A6vFEdd0IAAAkJAAAQEKPCwCG/wGgPkfWdyMAAJCQAAAACQkAAJA9ALj+DwD1OrzOGwEAgIQEAABISAAAgMwBwPV/AKjfoXpvBAAAEhIAACAhAQAAEhIAACChzvAfJgACQC5GAAAgIQEAABISAAAgIQEAABISAAAgIQEAABLqugUQAPIxAgAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACQkAAJCQAAAACfXm3QDmb9fV75h3E4A52HfFlbZ7Yp3de/YO5t0ItpeCDzwRgSAXASARhR/YCkEgBwGgcoo+MAlhoF4mAVZM8QccR3gyRgAqpPADs2A0oC5GACqj+AOOL2yFAFARxR9wnGGrBIBKKP6A4w3jEAAqoPgDjjuMSwBoOcUfcPyhCQGgxRR/YN4ch9pLAGgpXzqgFI5H7SQAtJAvG1Aax6X2EQAAICEBoGWkbKBUjk/tIgC0iC8XUDrHqfYQAAAgIQEAABISAFrCsBrQFo5X7SAAAEBCAkALSNNA2zhulU8AAICEBAAASEgAAICEBIDCuY4GtJXjV9kEAABISAAAgIQEAABISAAAgIR6824A5dp3xZXzbgItnkzl81OONn5+mD0BgMdx4C53n7ThQO7zU542fX7YPi4B8BgO3mUrff+U3r7s7B8OJwDw5xwc2qHU/VRqu3gs+4lDBABGHBTapbT9VVp7eGr2F0MCAA4GLVXKQbyUdjAe+w0BAAASEgCScxbQbvPef/N+fyZj/+UmAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkACS36+p3zLsJtHj/zfv9mYz9l5sAAAAJCQA4C2ipUs7eSmkH47HfEAAYcTBol9L2V2nt4anZXwwJADgotEypB+9S28Vj2U8cIgDwGA4OZSt9/5TevuzsHw7Xe8x/wWEHiX1XXGl7FKJNB26fn/K06fPD9hEAeFIOGkzC5wfK5hIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgJA4fZdceW8mwDQiONX2QQAAEhIAACAhAQAAEhIAGgB19GAtnHcKp8AAAAJCQAtIU0DbeF41Q4CAAAkJAAAQEICQIsYVgNK5zjVHp3de/YO5t0IxrPr6nek3mTjHGCybysm5/M2m23F/PXm3QCY5UHlyNcKBPi8wUFGAFoqQyGb5dlEhu3HeHzeJuPsv30EgBarsYjN4yBS43Zka3zepkPxbyeTAFusti/dvPpT23Zka3zepsP3p72MAFSg7WewJR1A2r4tOTqftzq3JeMzAlCBNn8JS2t7ae2h7v1bWnuytJ2DBIBKtPHLWGqbS20Xde7XUttVW5t5PAGgIm36Upbe1tLbR137s/T2tbWtPDUBoDJt+HK2oY1taid17Mc2tLMNbWTrTAKsWIkT2tp4AClxO7I1Pm95tyNHZwSgYqV9aUtrT+3tzq6t+620dpfWHqbHCEAi8zyTreEgYiSgPXzebD+OTgBIaLsLWQ0H40OEgPL5vNl2bI2HASXkATnAEx0LyMUIAAAkZBIgACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQkAABAQgIAACQk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"

# Service worker: app-shell offline + installability. Network-first for our own
# routes (page + live data stay fresh), cache-first for Leaflet/tiles. Only
# registers over HTTPS (Funnel) — silently no-ops on plain HTTP.
PWA_SW = """const C='iett522-v1';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.origin===location.origin){
    e.respondWith(fetch(e.request).then(r=>{if(u.pathname==='/'){const cp=r.clone();caches.open(C).then(c=>c.put('/',cp).catch(()=>{}));}return r;}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/'))));
  }else{
    e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(resp=>{const cp=resp.clone();caches.open(C).then(c=>c.put(e.request,cp).catch(()=>{}));return resp;})));
  }
});"""

MAP_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>İETT — __TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#0e1014">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" sizes="192x192" href="/icon-192.png">
<link rel="icon" href="/icon.svg">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#0e1014;--card:rgba(22,25,32,.92);--fg:#f3f5f7;--muted:#a6b0bb;
    --green:#19c37d;--red:#f0506e;--gold:#ffc107;--line:rgba(255,255,255,.09)}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
  #map{position:absolute;inset:0;background:var(--bg)}
  #hero{position:absolute;z-index:1000;top:max(10px,env(safe-area-inset-top));left:10px;right:10px;
    background:var(--card);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
    border:1px solid var(--line);border-radius:18px;padding:14px 15px 12px;
    box-shadow:0 12px 34px rgba(0,0,0,.5);font-variant-numeric:tabular-nums}
  @media(min-width:560px){#hero{left:14px;right:auto;width:380px}}
  .seg{display:flex;gap:7px;margin-bottom:13px}
  .seg button{flex:1;appearance:none;border:1px solid var(--line);background:transparent;
    color:var(--muted);font-size:12.5px;font-weight:600;padding:7px 6px;border-radius:12px;
    cursor:pointer;line-height:1.2;transition:background .15s,border-color .15s,color .15s}
  .seg button:focus-visible{outline:2px solid var(--fg);outline-offset:2px}
  .seg button .s{display:block;font-size:10px;font-weight:500;opacity:.9;margin-top:2px}
  .seg button.on{color:var(--fg);font-weight:700}  /* direction colour set inline in renderSeg */
  .next{display:flex;align-items:baseline;gap:7px}
  .min{font-size:48px;font-weight:800;line-height:.85;letter-spacing:-2px}
  .unit{font-size:14px;color:var(--muted);font-weight:600}
  .badge{margin-left:auto;align-self:center;font-size:11px;font-weight:700;padding:4px 10px;
    border-radius:999px;background:rgba(255,255,255,.1)}
  .badge.live{background:rgba(25,195,125,.18);color:var(--green)}
  .badge.sched{background:rgba(255,193,7,.16);color:var(--gold)}
  .sub{color:var(--muted);font-size:12.5px;margin-top:5px;line-height:1.38}
  .conf{font-size:9px;font-weight:700;letter-spacing:.4px;vertical-align:middle;opacity:.8}
  .acc{margin-top:8px;border-top:1px solid var(--line);padding-top:6px;font-size:11px;color:var(--muted);display:none}
  .acc b{color:var(--fg);font-weight:700}
  .more{margin-top:9px;border-top:1px solid var(--line);padding-top:6px}
  .row{display:flex;justify-content:space-between;align-items:center;font-size:13px;padding:3px 0}
  .row .t{color:var(--muted)}
  .empty{color:var(--muted);font-size:12.5px;padding:5px 0}
  .foot{display:flex;align-items:center;gap:7px;margin-top:11px;font-size:11px;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
  .dot.warn{background:var(--gold)} .dot.down{background:var(--red)}
  .dim{color:var(--muted)}
  @keyframes breathe{0%,100%{box-shadow:0 0 0 0 rgba(25,195,125,.55)}70%{box-shadow:0 0 0 8px rgba(25,195,125,0)}}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
  @keyframes bounce{0%,80%,100%{transform:translateY(0);opacity:.45}40%{transform:translateY(-4px);opacity:1}}
  .iL{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;animation:breathe 2s infinite}
  .iD{width:8px;height:8px;border-radius:50%;background:var(--red);display:inline-block;animation:blink 1.5s infinite}
  .iW i{width:5px;height:5px;border-radius:50%;background:var(--muted);display:inline-block;margin-right:3px;animation:bounce 1.1s infinite}
  .iW i:nth-child(2){animation-delay:.15s} .iW i:nth-child(3){animation-delay:.3s}
  .busglide{transition:transform .9s linear}
  #offline{display:none;position:absolute;top:0;left:0;right:0;z-index:1200;
    background:#f0506e;color:#fff;text-align:center;font-size:13px;font-weight:600;
    padding:7px 10px;letter-spacing:.2px}
  .home{color:#fff;font-size:16px;border-radius:50%;width:26px;height:26px;display:flex;
    align-items:center;justify-content:center;background:var(--red);border:2px solid #0e1014;
    box-shadow:0 0 0 rgba(240,80,110,.6);animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(240,80,110,.5)}
    70%{box-shadow:0 0 0 13px rgba(240,80,110,0)}100%{box-shadow:0 0 0 0 rgba(240,80,110,0)}}
  .hometip{background:var(--red);color:#fff;border:none;font-weight:700;font-size:11px}
  .hometip:before{border-right-color:var(--red)}
  .leaflet-popup-content-wrap,.leaflet-tooltip{background:#11141a !important;color:var(--fg) !important;
    border:1px solid var(--line) !important;border-radius:10px}
  .leaflet-popup-tip{background:#11141a}
  .leaflet-tooltip{box-shadow:0 2px 10px rgba(0,0,0,.5);font-size:11px}
  .leaflet-tooltip-top:before{border-top-color:#11141a}
  .leaflet-container a{color:var(--green)}
  .leaflet-bar a{background:#11141a;color:var(--fg);border-color:var(--line)}
  #login{display:none;position:fixed;inset:0;z-index:3000;background:var(--bg);
    align-items:center;justify-content:center;padding:24px}
  #login.show{display:flex}
  .lcard{background:var(--card);border:1px solid var(--line);border-radius:20px;
    padding:28px 22px;width:100%;max-width:320px;text-align:center;
    box-shadow:0 12px 40px rgba(0,0,0,.6)}
  .lbus{width:52px;height:52px;border-radius:14px}
  .ltitle{font-size:21px;font-weight:800;margin-top:8px}
  .lsub{color:var(--muted);font-size:13px;margin:4px 0 18px}
  #lpw{width:100%;box-sizing:border-box;padding:13px 14px;border-radius:12px;
    border:1px solid var(--line);background:#11141a;color:var(--fg);font-size:16px;outline:none}
  #lpw::placeholder{color:#8892a0}   /* legible in sunlight (≥4.5:1), not default faint gray */
  #lpw:focus{border-color:var(--green)}
  #lbtn{width:100%;margin-top:12px;padding:13px;border:none;border-radius:12px;
    background:var(--green);color:#06281b;font-size:16px;font-weight:800;cursor:pointer}
  #lbtn:disabled{opacity:.6}
  #lerr{color:var(--red);font-size:13px;margin-top:11px;min-height:17px}
  /* Motion conveys liveness here; when the user opts out, hold state statically
     (buses jump to position instead of gliding; the live/stale dots stop pulsing). */
  @media (prefers-reduced-motion: reduce){
    .busglide{transition:none}
    .iL,.iD,.home{animation:none}
    .iW i{animation:none;opacity:.7}
    .iL{box-shadow:0 0 6px var(--green)}
  }
</style></head>
<body>
<div id="login"><div class="lcard">
  <img class="lbus" src="/icon.svg" alt=""><div class="ltitle">İETT 522</div>
  <div class="lsub">Enter password to continue</div>
  <input id="lpw" type="password" placeholder="Password" autocomplete="current-password" enterkeyhint="go">
  <button id="lbtn" onclick="doLogin()">Sign in</button>
  <div id="lerr"></div>
</div></div>
<div id="map"></div>
<div id="offline">⚠ tracker offline — can't reach the server</div>
<div id="hero">
  <div class="seg" id="seg"></div>
  <div class="next"><span class="min" id="min">–</span><span class="unit" id="unit">min</span>
    <span class="badge" id="badge"></span></div>
  <div class="sub" id="sub">loading…</div>
  <div class="more" id="more"></div>
  <div class="foot"><span class="dot" id="dot"></span><span id="status">…</span>
    <span style="margin-left:auto" id="time"></span></div>
  <div class="acc" id="acc"></div>
</div>
<script>
// Custom login: data routes 401 when not signed in → show our own overlay
// (no native browser dialog). /login sets a cookie that's sent automatically after.
const _fetch=window.fetch.bind(window);
window.fetch=(...a)=>_fetch(...a).then(r=>{if(r.status===401)showLogin();return r;});
function showLogin(){const l=document.getElementById('login');if(!l.classList.contains('show')){l.classList.add('show');setTimeout(()=>document.getElementById('lpw').focus(),60);}}
function hideLogin(){document.getElementById('login').classList.remove('show');}
async function doLogin(){
  const pw=document.getElementById('lpw').value, b=document.getElementById('lbtn'), er=document.getElementById('lerr');
  er.textContent=''; b.disabled=true;
  try{
    const r=await _fetch('/login',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'password='+encodeURIComponent(pw)});
    if(r.ok){hideLogin();document.getElementById('lpw').value='';refresh();fetchAccuracy();warmCache();}
    else{er.textContent='Wrong password';}
  }catch(e){er.textContent='Connection error — try again';}
  b.disabled=false;
}
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&document.getElementById('login').classList.contains('show'))doLogin();});
const LINES=__LINES_JSON__, PRIMARY="__PRIMARY__", INTERVAL=__INTERVAL__;
const COLORS=__COLORS_JSON__, DEFCOLOR="#3679c9", FOCUS=__FOCUS_JSON__;
const DIR_G='#19c37d', DIR_D='#f0506e';   // going = green · coming back = red
function colorFor(ln){return COLORS[ln]||DEFCOLOR;}
function lp(ln){return LINES.length>1?ln+' · ':'';}   // line prefix only when tracking several
const map=L.map('map',{zoomControl:false,attributionControl:false}).setView([41.05,29.0],12);
L.control.zoom({position:'bottomright'}).addTo(map);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {maxZoom:20,subdomains:'abcd'}).addTo(map);
const routeLayer=L.layerGroup().addTo(map);
const busLayer=L.layerGroup().addTo(map), stopLayer=L.layerGroup().addTo(map);
let firstFit=true, refitNext=false, stopsDrawn=false, homeMarker=null, focusIdx=0;
const GHOST_MAX_AGE=300;   // drop a GPS-lost ghost after 5 min with no fresh fix
const keyPts=[];
// Arrow as an SVG triangle so it can be a SOLID fill when the GPS is live, or a
// hollow OUTLINE ("ghost") when the fix is stale / the bus dropped its coordinate.
function arrowSVG(leg,bearing,stale){
  const c = leg==='D' ? DIR_D : DIR_G, known = bearing!=null;
  return '<svg width="15" height="15" viewBox="0 0 16 16" style="transform:rotate('
    +(known?bearing:0)+'deg);opacity:'+(known?1:0.6)+';filter:drop-shadow(0 0 1.5px rgba(0,0,0,.85))">'
    +'<polygon points="8,2 14,14 2,14" fill="'+(stale?'none':c)+'" stroke="'+c
    +'" stroke-width="'+(stale?1.8:0)+'" stroke-linejoin="round"/></svg>';
}
function busIcon(leg,bearing,stale){
  return L.divIcon({className:'busglide',html:arrowSVG(leg,bearing,stale),
    iconSize:[15,15],iconAnchor:[8,8]});
}
function styleArrow(m,leg,bearing,stale){   // restyle an existing marker's arrow in place
  const svg=m._icon&&m._icon.firstChild; if(!svg) return;
  const c = leg==='D' ? DIR_D : DIR_G, known=bearing!=null;
  svg.style.transform='rotate('+(known?bearing:0)+'deg)';
  svg.style.opacity = known?1:0.6;
  const pg=svg.querySelector('polygon');
  if(pg){ pg.setAttribute('fill',stale?'none':c);
          pg.setAttribute('stroke',c);
          pg.setAttribute('stroke-width',stale?1.8:0); }
}
const homeIcon=L.divIcon({className:'',html:'<div class="home">★</div>',iconSize:[26,26],iconAnchor:[13,13]});
function drawStops(stops){
  stops.forEach(s=>{
    if(s.hl==='home'){
      keyPts.push([s.lat,s.lon]);
      homeMarker=L.marker([s.lat,s.lon],{icon:homeIcon,zIndexOffset:1000})
        .bindTooltip('🏠 '+s.name,{permanent:true,direction:'right',className:'hometip',offset:[12,0]})
        .bindPopup('<b>🏠 '+s.name+'</b><br><span class=dim>loading…</span>',{minWidth:230})
        .addTo(stopLayer);
      homeMarker.on('popupopen',()=>loadStopPopup(homeMarker, PRIMARY, s.code, s.name, 'going'));
    }else if(s.hl==='dest'){
      keyPts.push([s.lat,s.lon]);
      const dir = s.yon==='G' ? 'going · arriving' : s.yon==='D' ? 'coming back · departing' : '';
      const m=L.circleMarker([s.lat,s.lon],{radius:6,color:'#b8860b',fillColor:'#ffc107',
        fillOpacity:.95,weight:2}).bindTooltip(s.name+(dir?' ('+dir+')':''),{direction:'top'})
        .bindPopup('<b>'+s.name+'</b><br><span class=dim>loading…</span>',{minWidth:230}).addTo(stopLayer);
      m.on('popupopen',()=>loadStopPopup(m, PRIMARY, s.code, s.name, dir));
    }else if(s.hl==='return'){
      keyPts.push([s.lat,s.lon]);
      const m=L.circleMarker([s.lat,s.lon],{radius:6,color:'#1e40af',fillColor:'#3b82f6',
        fillOpacity:.95,weight:2}).bindTooltip('🏡 '+s.name+' (coming home)',{direction:'top'})
        .bindPopup('<b>🏡 '+s.name+'</b><br><span class=dim>loading…</span>',{minWidth:230}).addTo(stopLayer);
      m.on('popupopen',()=>loadStopPopup(m, PRIMARY, s.code, s.name, 'coming home · arriving'));
    }else{
      L.circleMarker([s.lat,s.lon],{radius:2,color:'#5a6675',fillColor:'#5a6675',
        fillOpacity:.6,weight:0}).bindTooltip(s.name,{direction:'top'}).addTo(stopLayer);
    }
  });
}
async function drawRoute(){
  try{
    const r=await fetch('/route?_='+Date.now()).then(r=>r.json());
    Object.keys(r).forEach(leg=>{
      if((r[leg]||[]).length>1)
        L.polyline(r[leg],{color:leg==='D'?DIR_D:DIR_G,weight:3,opacity:.45}).addTo(routeLayer);
    });
  }catch(e){}
}
const busMarkers={};   // kapino -> marker, reused so buses glide instead of teleporting
let offlineHits=0;     // consecutive failures to reach OUR server (not İBB)
function serverReachable(ok){
  if(ok){ offlineHits=0; document.getElementById('offline').style.display='none'; }
  else if(++offlineHits>=2){ document.getElementById('offline').style.display='block'; }
}
async function tick(){
  try{
    // only fetch the line you're actually looking at (keeps 34G off the İBB
    // budget unless its tab is open); base line draws its stops once.
    const actLine=(FOCUS[focusIdx]||{}).line;
    const reqLines=actLine?[actLine]:LINES, onBase=(actLine===PRIMARY);
    const url='/positions?lines='+encodeURIComponent(reqLines.join(','))
      +'&dir=0'+((stopsDrawn||!onBase)?'':'&stops=1')+'&_='+Date.now();
    const d=await fetch(url).then(r=>r.json());
    serverReachable(true);   // we reached our own server (İBB may still be down)
    if(!stopsDrawn && d.stops && d.stops.length){drawStops(d.stops);stopsDrawn=true;}
    const pts=[], seen={};
    (d.buses||[]).forEach(b=>{
      const fresh = b.lat!=null && b.lon!=null;
      let m=busMarkers[b.kapino];
      if(!fresh){                         // GPS-lost: hold as a ghost, but not forever
        const ageS=ageSecs(b.seen);       // drop if it never had a position, or the
        if(!m || (ageS!=null && ageS>GHOST_MAX_AGE)){   // ghost is too stale to trust
          if(m){ busLayer.removeLayer(m); delete busMarkers[b.kapino]; }
          return;                         // left out of `seen` → stays dropped
        }
      }
      const lost = !fresh;               // in the feed but no GPS coordinate this frame
      const stale = b.projected || lost || ageSecs(b.seen)>150;
      const tip=b.kapino+' · '+(b.line||'')+' '+(b.leg==='D'?'(coming)':'(going)')
        +(b.projected?' · est · İBB down':(lost?' · GPS lost · last fix '+(b.seen||'?'):''));
      if(fresh) pts.push([b.lat,b.lon]);
      seen[b.kapino]=1;                  // keep it on the map even while GPS is quiet
      if(m){
        if(fresh) m.setLatLng([b.lat,b.lon]);   // glide to new fix; if lost, hold last
        styleArrow(m,b.leg,b.bearing,stale);    // solid → hollow as it goes stale
        m.setTooltipContent(tip);
      }else{
        m=L.marker([b.lat,b.lon],{icon:busIcon(b.leg,b.bearing,stale),zIndexOffset:500})
          .bindTooltip(tip,{direction:'top'});
        m.addTo(busLayer); busMarkers[b.kapino]=m;
      }
    });
    Object.keys(busMarkers).forEach(k=>{           // drop buses that vanished
      if(!seen[k]){ busLayer.removeLayer(busMarkers[k]); delete busMarkers[k]; }
    });
    if((firstFit||refitNext) && (pts.length||keyPts.length)){
      map.fitBounds((pts.length?pts:keyPts),{padding:[70,70],maxZoom:13});
      firstFit=false;refitNext=false;}
  }catch(e){ serverReachable(false); }   // fetch threw → our server is unreachable
}
function etaBody(d){
  if(d.error) return '<span class=dim>no live data (İBB down) — see timetable below</span>';
  const rows=(d.rows||[]).slice(0,6);
  if(!rows.length) return '<span class=dim>no buses approaching</span>';
  const mark=r=>r.eta_src==='history'?' ✓':r.eta_src==='schedule'?' (sched)':'';
  return rows.map(r=>'<div class="row"><span>'+(r.eta_min!=null?r.eta_min+' min':'?')
    +'<span class=dim>'+mark(r)+'</span></span><span class="t">'+r.line+' · '+r.stops_away
    +' stops</span></div>').join('');
}
function scheduleBody(s){
  const rows=(s&&s.rows)||[];
  if(!rows.length) return '';
  return '<div style="margin-top:6px" class=dim>timetable: '
    +rows.slice(0,5).map(r=>r.arr+' ('+r.in_min+'m)').join(' · ')+'</div>';
}
async function loadStopPopup(m, line, code, name, dir){
  try{
    const [d,sch]=await Promise.all([
      fetch('/eta?line='+line+'&stop='+code+'&_='+Date.now()).then(r=>r.json()),
      fetch('/schedule?line='+line+'&stop='+code+'&_='+Date.now()).then(r=>r.json()).catch(()=>({rows:[]}))]);
    m.setPopupContent('<b>'+name+'</b>'+(dir?' <span class=dim>('+dir+')</span>':'')
      +'<br>'+etaBody(d)+scheduleBody(sch));
  }catch(e){ m.setPopupContent('<b>'+name+'</b><br><span class=dim>error</span>'); }
}
function badge(src){
  return src==='history'?['live','live ✓']:src==='projected'?['sched','est · İBB down']
       :src==='origin-wait'?['sched','at origin']
       :src==='schedule'?['sched','baseline']:src==='estimate'?['sched','estimate']
       :['sched','timetable'];
}
function confMark(c){   // a small dot conveying how trustworthy the ETA is
  return c==='high'?'<span class="conf" style="color:var(--green)">●</span>'
       :c==='med'?'<span class="conf" style="color:var(--gold)">●</span>'
       :c==='low'?'<span class="conf" style="color:var(--muted)">○</span>':'';
}
function etaText(r){  // show a range when we have one, else the point; clamp ≥0
  if(r.no_more) return '–';
  const lo=r.eta_lo, hi=r.eta_hi;
  if(lo!=null && hi!=null && hi>lo) return Math.max(0,lo)+'–'+Math.max(0,hi);
  return r.eta_min!=null ? (''+Math.max(0,r.eta_min)) : '?';
}
function ageSecs(seen){  // seen = "HH:MM:SS" (bus's last GPS report, local time)
  if(!seen) return null;
  const p=seen.split(':'); if(p.length<3) return null;
  const now=new Date(), t=new Date(now);
  t.setHours(+p[0],+p[1],+p[2],0);
  let d=(now-t)/1000;
  if(d<-60) d+=86400;        // bus report just before a midnight rollover
  return Math.max(0,Math.round(d));
}
const SEGC=[DIR_G,DIR_D,'#4aa3df'],
      SEGT=['rgba(25,195,125,.16)','rgba(240,80,110,.15)','rgba(74,163,223,.18)'];  // active tab = its colour (34G=blue)
function renderSeg(){
  document.getElementById('seg').innerHTML = FOCUS.map((f,i)=>{
    const on=i===focusIdx, c=SEGC[i%SEGC.length], t=SEGT[i%SEGT.length];
    const bst=on?'border-color:'+c+';background:'+t:'', sst=on?'color:'+c:'';
    return '<button class="'+(on?'on':'')+'" style="'+bst+'" onclick="setFocus('+i+')">'
      +f.label+'<span class="s" style="'+sst+'">'+f.at+'</span></button>';
  }).join('');
}
function setFocus(i){
  const ol=(FOCUS[focusIdx]||{}).line; focusIdx=i;
  const nl=(FOCUS[i]||{}).line;
  renderSeg(); updateHero(); fetchAccuracy();
  if(nl!==ol){                              // switched line → refit map + show only its layers
    refitNext=true;
    const base=(nl===PRIMARY);              // route + 522 stops/home marker belong to the base line
    [routeLayer,stopLayer].forEach(L=>{ base ? L.addTo(map) : map.removeLayer(L); });
  }
  tick();
}
function accTone(b){return Math.abs(b)<0.5?'well-centred'
  :b<0?Math.abs(b).toFixed(1)+'m optimistic':b.toFixed(1)+'m cautious';}
const BUCKET_LBL={'weekday-rush':'weekday rush','weekday-daytime':'weekday daytime','weekend':'weekend'};
async function fetchAccuracy(){   // how close predictions land (show-only)
  try{
    const a=await fetch('/accuracy?stop='+FOCUS[focusIdx].stop+'&_='+Date.now()).then(r=>r.json());
    const el=document.getElementById('acc');
    el.style.display='block';
    const bn=a&&a.bucket_now, bb=a&&a.by_bucket&&a.by_bucket[bn];
    if(bb&&bb.n>=8){   // enough data for THIS time-of-week — show the bucketed number
      el.innerHTML='ETA accuracy ('+(BUCKET_LBL[bn]||bn)+'): <b>±'+bb.mae+' min</b> · '
        +accTone(bb.bias)+' <span class=dim>(measured, n='+bb.n+')</span>'; return;
    }
    const s=a&&(a.near10||a.overall);
    if(s&&s.n>=10){   // pooled measured number once it's not just noise
      el.innerHTML='ETA accuracy: <b>±'+s.mae+' min</b> · '+accTone(s.bias)
        +' <span class=dim>(measured, n='+s.n+')</span>'; return;
    }
    const bt=a&&a.backtest;   // immediate estimate from historical data meanwhile
    if(bt&&bt.n){
      if(bt.days<3 || bt.coverage<0.6){   // too little history to be believed yet
        el.innerHTML='self-check: <b>learning the route…</b> <span class=dim>('
          +bt.days+(bt.days===1?' day':' days')+' of data so far)</span>'; return;
      }
      el.innerHTML='ETA accuracy ≈ <b>±'+bt.mae+' min</b> · '+accTone(bt.bias)
        +' <span class=dim>(modelled from '+bt.days+'d history · measuring live…)</span>'; return;
    }
    el.innerHTML='self-check: <b>learning…</b> <span class=dim>— needs more data</span>';
  }catch(e){}
}
const heroCache={};   // last {eta,sch} per focus — so the toggle/refresh never blanks
const LS_HERO='iett522hero', LS_VIEW='iett522view';
function saveCache(){ try{ localStorage.setItem(LS_HERO, JSON.stringify(heroCache)); }catch(e){} }
function loadCache(){ try{ const j=localStorage.getItem(LS_HERO);
  if(j) Object.assign(heroCache, JSON.parse(j)); }catch(e){} }   // survive a full reload
function restoreView(){ try{ const v=JSON.parse(localStorage.getItem(LS_VIEW)||'null');
  if(v&&v.c){ map.setView(v.c, v.z); firstFit=false; } }catch(e){} }
map.on('moveend',()=>{ try{ const c=map.getCenter();
  localStorage.setItem(LS_VIEW, JSON.stringify({c:[c.lat,c.lng],z:map.getZoom()})); }catch(e){} });
function setInd(k){   // working = fetch in flight · live = fresh · stale/down
  const el=document.getElementById('dot'); el.className='';
  el.innerHTML = k==='working' ? '<span class="iW"><i></i><i></i><i></i></span>'
    : k==='down' ? '<span class="iD"></span>'
    : k==='stale' ? '<span class="iD" style="background:var(--gold)"></span>'
    : '<span class="iL"></span>';
}
function renderHero(eta, sch){
  const live=(eta.rows||[]), srows=(sch.rows||[]);
  const $=id=>document.getElementById(id);
  let list;
  if(live.length){
    const r=live[0];
    $('min').textContent=etaText(r); $('unit').style.display='';
    const [bc,bt]=badge(r.eta_src); $('badge').className='badge '+bc;
    $('badge').innerHTML=bt+' '+confMark(r.eta_conf);
    const bunched=r.bunched && live[1] && live[1].bunched;  // two clumped at the front
    if(r.at_origin){   // parked at the terminus — show the departure, not "1 stop"
      if(r.no_more){
        $('sub').textContent=lp(r.line)+'🅿 at origin · no more departures today'
          +(eta.last_passed?' · last passed '+eta.last_passed:'');
      }else{
        $('sub').textContent=lp(r.line)+'🅿 waiting at origin'
          +(r.depart_hhmm?' · departs ~'+r.depart_hhmm:' · departs soon')
          +(r.dep_offset?' (typ. '+(r.dep_offset>0?r.dep_offset+'m late':(-r.dep_offset)+'m early')+')':'')
          +(eta.last_passed?' · last passed '+eta.last_passed:'');
      }
    }else{
      $('sub').textContent=lp(r.line)+r.stops_away+' stops away'
        +(bunched?' · ⚠ bunched':'')
        +(eta.last_passed?' · last passed '+eta.last_passed:'');
    }
    if(eta.stale){   // İBB down → these rows are dead-reckoned from the last fetch
      setInd('stale');
      $('status').textContent='projected · İBB down · last live '
        +Math.round((eta.age||0)/60)+'m ago';
    }else{
      const age=ageSecs(r.seen);   // age of the last GPS fix — the arrow is already
      setInd(age==null||age<150?'live':'stale');   // projected forward to NOW from it
      $('status').textContent = age==null ? 'live'
        : age<90 ? 'live'                                   // arrow = current estimate
        : age<150 ? 'live · GPS confirmed '+age+'s ago'
        : 'live (estimated) · last GPS '+Math.round(age/60)+'m ago';
    }
    list=live.slice(1,4).map(x=>({m:etaText(x),b:(x.bunched?'bunched':badge(x.eta_src)[1]),t:lp(x.line)+x.stops_away+' stops'}));
  } else if(eta.due_min!=null){   // no live buses → frequency (headway) estimate
    $('min').textContent=Math.max(0,eta.due_min); $('unit').style.display='';
    $('badge').className='badge sched'; $('badge').textContent='headway';
    $('sub').textContent='~a bus every '+eta.headway_min+' min'
      +(eta.last_passed?' · last passed '+eta.last_passed:'');
    setInd(eta.error?'down':'stale');
    $('status').textContent=(eta.error?'İBB down':'no live buses')+' — frequency estimate';
    list=srows.slice(0,3).map(r=>({m:r.in_min,b:'sched',t:'scheduled '+r.arr}));
  } else if(srows.length){
    $('min').textContent=srows[0].in_min; $('unit').style.display='';
    $('badge').className='badge sched'; $('badge').textContent='timetable';
    $('sub').textContent='scheduled · arrives '+srows[0].arr;
    setInd(eta.error?'down':'stale');
    $('status').textContent=eta.error?'İBB down — timetable':'no live buses — timetable';
    list=srows.slice(1,4).map(r=>({m:r.in_min,b:'',t:'scheduled '+r.arr}));
  } else {
    $('min').textContent='–'; $('unit').style.display='none'; $('badge').textContent='';
    $('badge').className='badge'; $('sub').textContent='no buses right now';
    setInd('down'); $('status').textContent=eta.error?'İBB down':'no data'; list=[];
  }
  if(eta.bias_adj){ $('status').textContent += ' · +'+eta.bias_adj+'m tuned'; }
  $('more').innerHTML = list.length
    ? list.map(x=>'<div class="row"><span>'+x.m+' min <span class=dim>'+(x.b||'')+'</span></span>'
        +'<span class="t">'+x.t+'</span></div>').join('')
    : '<div class="empty">nothing further out</div>';
  $('time').textContent=new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
}
async function fetchHero(f){
  return Promise.all([
    fetch('/eta?line='+f.line+'&stop='+f.stop+'&_='+Date.now()).then(r=>r.json()),
    fetch('/schedule?line='+f.line+'&stop='+f.stop+'&_='+Date.now()).then(r=>r.json()).catch(()=>({rows:[]}))
  ]);
}
async function updateHero(){
  const f=FOCUS[focusIdx], key=focusIdx;
  if(heroCache[key]) renderHero(heroCache[key].eta, heroCache[key].sch);  // instant: last value
  setInd('working');   // bouncing dots while the fresh fetch is in flight
  let eta, sch;
  try{ [eta,sch]=await fetchHero(f); }catch(e){ return; }  // keep cached value on a hiccup
  heroCache[key]={eta,sch}; saveCache();
  if(key===focusIdx) renderHero(eta,sch);   // only paint if still the active side
}
function warmCache(){   // pre-fetch every side once so the first toggle is instant too
  FOCUS.forEach((f,i)=>fetchHero(f).then(([eta,sch])=>{heroCache[i]={eta,sch};}).catch(()=>{}));
}
function defaultFocus(){return (new Date().getHours()>=14 && FOCUS.length>1)?1:0;}
function refresh(){tick();updateHero();}
loadCache(); focusIdx=defaultFocus(); renderSeg();
if(heroCache[focusIdx]) renderHero(heroCache[focusIdx].eta, heroCache[focusIdx].sch);  // instant: last session
restoreView(); drawRoute(); refresh(); warmCache(); fetchAccuracy();
setInterval(refresh, INTERVAL*1000); setInterval(fetchAccuracy, 180000);
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{});}  // PWA (HTTPS only)
</script></body></html>"""

# Server-side cache of (annotated) stop lists — so fast map polling never
# re-downloads the 48 KB route every few seconds.
_stops_cache: dict = {}


def _map_stops(line: str) -> list:
    out = []
    for s in api_line_stops(line):
        try:  # İETT data: XKOORDINATI = lon, YKOORDINATI = lat
            code = s.get("DURAKKODU", "")
            nm = norm(s.get("DURAKADI", ""))
            hl = ("home" if code in MAP_HOME_STOPS else
                  "return" if code in MAP_RETURN_STOPS else
                  "dest" if any(h in nm for h in MAP_DEST_HINTS) else "")
            out.append({"name": s.get("DURAKADI", ""), "sira": s.get("SIRANO", ""),
                        "yon": s.get("YON", ""), "code": code, "hl": hl,
                        "lat": float(s["YKOORDINATI"]), "lon": float(s["XKOORDINATI"])})
        except (TypeError, ValueError, KeyError):
            continue
    return out


def cached_stops(line: str) -> list:
    ent = _stops_cache.get(line)
    if ent and (time.time() - ent[0]) < 3600:
        return ent[1]
    try:
        stops = _map_stops(line)
    except (Exception, SystemExit):
        return ent[1] if ent else []
    _stops_cache[line] = (time.time(), stops)
    return stops


# Last successful live fetch per line — kept so we can dead-reckon buses forward
# during İBB's frequent outages instead of blanking the map.
_pos_cache: dict = {}
POS_PROJECT_MAX = 600   # cap projection at 10 min of elapsed time (then drop)

# ── Map-matching + Kalman tracking ───────────────────────────────────────────
# Snap noisy GPS onto the route polyline (a 1-D distance-along-route coordinate),
# then run a constant-velocity Kalman filter on it — the same technique transit
# agencies use. Gives smooth gliding, a real speed estimate, and principled
# dead-reckoning during İBB outages (project distance forward at the last speed).
_poly_cache: dict = {}
MAX_SNAP_OFF_M = 200.0      # ignore a fix farther than this from the route (off-route)
KF_ACCEL_STD = 1.0          # m/s² process noise — how fast a bus can change speed
KF_MEAS_STD = 18.0          # m — map-matched position measurement noise
KF_MAX_SPEED = 22.0         # m/s clamp (~80 km/h) so a bad fix can't fling a marker
_kf_state: dict = {}        # kapino -> filter state


def _equirect(lat, lon, lat0):
    r = 6371000.0
    return (math.radians(lon) * r * math.cos(math.radians(lat0)),
            math.radians(lat) * r)


def _poly_index(line: str) -> dict:
    """Per-leg route polyline with cumulative metres (from OSRM road geometry), cached.
    NOTE: a stop-centerline rebuild was tried (18 Jun) to kill self-overlaps but it
    REGRESSED the median snap error (5 m → 82 m) because straight chords cut road
    curves, and the ~360 m tail was unaffected — so the overlaps weren't the tail.
    Reverted to OSRM; the tail is KF re-init transients + genuine bus unpredictability."""
    ent = _poly_cache.get(line)
    if ent and (time.time() - ent[0]) < 3600:
        return ent[1]
    out = {}
    try:
        rd = road_data(line)
    except (Exception, SystemExit):
        rd = {}
    for leg, info in rd.items():
        pts = info.get("geom") or []
        if len(pts) < 2:
            continue
        cum = [0.0]
        for i in range(1, len(pts)):
            cum.append(cum[-1] + haversine_km(pts[i-1][0], pts[i-1][1],
                                              pts[i][0], pts[i][1]) * 1000.0)
        out[leg] = {"pts": pts, "cum": cum, "total": cum[-1]}
    _poly_cache[line] = (time.time(), out)
    return out


def _snap_to_route(line, leg, lat, lon):
    """Project a GPS fix onto the leg polyline → (lat, lon, s_along_m, bearing, off_m)."""
    idx = _poly_index(line).get(leg)
    if not idx:
        return None
    pts, cum = idx["pts"], idx["cum"]
    px, py = _equirect(lat, lon, lat)
    best = None
    for i in range(len(pts) - 1):
        ax, ay = _equirect(pts[i][0], pts[i][1], lat)
        bx, by = _equirect(pts[i+1][0], pts[i+1][1], lat)
        dx, dy = bx - ax, by - ay
        seg2 = dx*dx + dy*dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / seg2))
        d2 = (px - (ax+t*dx))**2 + (py - (ay+t*dy))**2
        if best is None or d2 < best[0]:
            best = (d2, i, t)
    d2, i, t = best
    s_along = cum[i] + t * (cum[i+1] - cum[i])
    la = pts[i][0] + t * (pts[i+1][0] - pts[i][0])
    lo = pts[i][1] + t * (pts[i+1][1] - pts[i][1])
    return la, lo, s_along, round(bearing_deg(pts[i][0], pts[i][1],
                                              pts[i+1][0], pts[i+1][1])), math.sqrt(d2)


def _point_at_s(line, leg, s):
    """Inverse map-match: (lat, lon, bearing) at distance `s` metres along the leg."""
    idx = _poly_index(line).get(leg)
    if not idx:
        return None
    pts, cum, total = idx["pts"], idx["cum"], idx["total"]
    s = max(0.0, min(total, s))
    lo, hi = 0, len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= s:
            lo = mid
        else:
            hi = mid
    seg_len = cum[lo+1] - cum[lo]
    t = 0.0 if seg_len == 0 else (s - cum[lo]) / seg_len
    return (pts[lo][0] + t * (pts[lo+1][0] - pts[lo][0]),
            pts[lo][1] + t * (pts[lo+1][1] - pts[lo][1]),
            round(bearing_deg(pts[lo][0], pts[lo][1], pts[lo+1][0], pts[lo+1][1])))


_proj_log: list = []   # (ts, dt, signed_err_m) — how far each projection missed


def _record_proj(dt, err):
    """Log one projection error: when a fresh fix arrives, `err` = actual − projected
    along-route metres over a `dt`-second gap. This grades the between-update motion
    model for free (every real fix is ground truth for the projection we just made)."""
    _proj_log.append((time.time(), dt, err))
    if len(_proj_log) > 8000:
        del _proj_log[:len(_proj_log) - 8000]


def projection_stats() -> dict:
    """How wrong the current (constant-velocity) between-update projection is —
    the baseline to beat. Restricted to normal staleness gaps (5–180 s)."""
    xs = [(dt, e) for (_, dt, e) in _proj_log if 5 <= dt <= 180]
    if not xs:
        return {"n": 0}
    abserr = sorted(abs(e) for _, e in xs)
    rates = [abs(e) / dt * 60 for dt, e in xs if dt > 0]
    return {"n": len(xs),
            "median_abs_m": round(statistics.median(abserr)),
            "p90_abs_m": round(abserr[min(len(abserr) - 1, int(len(abserr) * 0.9))]),
            "signed_bias_m": round(statistics.mean(e for _, e in xs)),
            "median_gap_s": round(statistics.median(dt for dt, _ in xs)),
            "drift_m_per_min": round(statistics.median(rates)) if rates else None}


def _kf_update(line, leg, kapino, s_meas, epoch):
    """Constant-velocity Kalman filter on distance-along-route. Returns smoothed
    (s, v) in metres / m·s⁻¹. Re-inits on a new bus/leg or a long gap."""
    st = _kf_state.get(kapino)
    if (st is None or st["line"] != line or st["leg"] != leg
            or epoch - st["epoch"] > 600):
        _kf_state[kapino] = {"s": s_meas, "v": 6.0, "P00": 100.0, "P01": 0.0,
                             "P10": 0.0, "P11": 25.0, "epoch": epoch,
                             "line": line, "leg": leg}
        return s_meas, 6.0
    if epoch <= st["epoch"]:   # İBB repeated the same GPS fix — keep state, no reset
        return st["s"], st["v"]
    dt = epoch - st["epoch"]
    s, v = st["s"], st["v"]
    P00, P01, P10, P11 = st["P00"], st["P01"], st["P10"], st["P11"]
    a2 = KF_ACCEL_STD ** 2                                  # predict step
    p00 = P00 + dt*(P01+P10) + dt*dt*P11 + 0.25*dt**4*a2
    p01 = P01 + dt*P11 + 0.5*dt**3*a2
    p10 = P10 + dt*P11 + 0.5*dt**3*a2
    p11 = P11 + dt*dt*a2
    s_p, v_p = s + v*dt, v
    if abs(s_meas - s_p) > 500:   # innovation gate: a >500 m jump is a map-match
        _kf_state[kapino] = {"s": s_p, "v": v_p, "P00": p00, "P01": p01,  # error —
                             "P10": p10, "P11": p11, "epoch": epoch,      # coast on
                             "line": line, "leg": leg}                    # the model
        return s_p, v_p
    _record_proj(dt, s_meas - s_p)   # grade the projection: actual − projected
    S = p00 + KF_MEAS_STD ** 2                              # update step
    k0, k1 = p00/S, p10/S
    y = s_meas - s_p
    v_new = max(0.0, min(KF_MAX_SPEED, v_p + k1*y))         # forward-only on a leg
    _kf_state[kapino] = {"s": s_p + k0*y, "v": v_new, "P00": (1-k0)*p00,
                         "P01": (1-k0)*p01, "P10": p10 - k1*p00,
                         "P11": p11 - k1*p01, "epoch": epoch,
                         "line": line, "leg": leg}
    return s_p + k0*y, v_new


def _live_bus_dicts(line: str, want: str) -> list:
    """Build the map's bus dicts for a line from the live feed (raises on API
    outage). Each carries `near`/`near_sira` so it can be projected later."""
    legs, coords = route_legs(line), stop_coords(line)
    result = []
    for b in api_live_buses(line):   # raises on outage; caller handles
        if want and not direction_match(b, want):
            continue
        try:
            lat = float(b["enlem"]) if b.get("enlem") else None
            lon = float(b["boylam"]) if b.get("boylam") else None
        except (TypeError, ValueError):
            lat = lon = None
        gk = b.get("guzergahkodu") or ""
        leg = "G" if "_G_" in gk else "D" if "_D_" in gk else ""
        # heading along the route: bearing from this stop to the next in
        # sequence (or previous→this near the end), in increasing sira
        bearing = near_sira = None
        lm = legs.get(leg)
        near = str(b.get("yakinDurakKodu"))
        if lm and near in lm["by_code"]:
            s = near_sira = lm["by_code"][near]
            nxt, prv = lm["by_sira"].get(s + 1), lm["by_sira"].get(s - 1)
            a, c = (coords.get(near), coords.get(nxt)) if nxt else \
                   (coords.get(prv), coords.get(near))
            if a and c:
                bearing = round(bearing_deg(a[0], a[1], c[0], c[1]))
        # map-match onto the road + smooth with the Kalman filter (when on-route)
        s_along = vel = None
        kap = b.get("kapino", "")
        if lat is not None and lon is not None and leg and kap:
            snap = _snap_to_route(line, leg, lat, lon)
            if snap and snap[4] <= MAX_SNAP_OFF_M:
                epoch = _parse_ts(b.get("son_konum_zamani") or "") or time.time()
                s_along, vel = _kf_update(line, leg, kap, snap[2], epoch)
                pt = _point_at_s(line, leg, s_along)
                if pt:
                    lat, lon, bearing = pt[0], pt[1], pt[2]
        result.append({"kapino": kap, "yon": b.get("yon", ""),
                       "guzergahkodu": gk, "line": line, "leg": leg,
                       "bearing": bearing, "near": near, "near_sira": near_sira,
                       "seen": (b.get("son_konum_zamani") or "")[-8:],
                       "lat": lat, "lon": lon, "s": s_along, "v": vel,
                       "projected": False})
    return result


def _project_buses(line: str, buses: list, age: float, hour: int) -> list:
    """Advance each cached bus `age` seconds. Preferred path: Kalman dead-reckoning
    — push the along-route distance forward at the filtered speed (s += v·age) and
    map back to a road point. Falls back to the learned-hop walk for buses that
    weren't map-matched. Flags them projected/stale so the UI dims them."""
    out = []
    db_buses = []
    for b in buses:
        nb = dict(b)
        nb["projected"], nb["stale"], nb["age"] = True, True, round(age)
        leg, s, v = b.get("leg"), b.get("s"), b.get("v")
        idx = _poly_index(line).get(leg) if leg else None
        if idx and s is not None and v is not None:
            v = max(0.0, min(KF_MAX_SPEED, v))
            pt = _point_at_s(line, leg, min(s + v * age, idx["total"]))
            if pt:
                nb["lat"], nb["lon"], nb["bearing"] = pt[0], pt[1], pt[2]
            out.append(nb)
        else:
            db_buses.append(nb)        # no map-match → learned-hop fallback below
    if db_buses:
        legs, coords = route_legs(line), stop_coords(line)
        with _db_lock:
            cur = db()
            memo = {}
            for nb in db_buses:
                leg, s = nb.get("leg"), nb.get("near_sira")
                lm = legs.get(leg) if leg else None
                if lm and s is not None and nb.get("lat") is not None:
                    remaining, cur_s, advanced = age, s, 0
                    while advanced < 8:
                        fc, tc = lm["by_sira"].get(cur_s), lm["by_sira"].get(cur_s + 1)
                        if not (fc and tc):
                            break
                        seg, _src = _segment_seconds(cur, line, leg, fc, tc, hour, memo)
                        if seg is None:
                            seg = 60.0
                        if remaining < seg:
                            a, c = coords.get(fc), coords.get(tc)
                            if a and c:
                                f = remaining / seg if seg else 0
                                nb["lat"] = a[0] + (c[0] - a[0]) * f
                                nb["lon"] = a[1] + (c[1] - a[1]) * f
                                nb["bearing"] = round(bearing_deg(a[0], a[1], c[0], c[1]))
                            break
                        remaining -= seg
                        cur_s += 1
                        advanced += 1
                    nb["near_sira"] = cur_s
                out.append(nb)
    return out


def positions_payload(lines: list, include_stops: bool = False, primary: str = "",
                      filter_dir: bool = True) -> dict:
    """Live buses for `lines` (+ stops for `primary` on demand). When `filter_dir`
    is set, only buses going "my way" (per MAP_DIRECTIONS) are returned. On an İBB
    outage, falls back per-line to the last good fetch, dead-reckoned forward.
    Never exits the process on API failure (it runs inside the map server)."""
    out = {"buses": [], "stops": [], "error": None, "lines": lines}
    now_hour = datetime.now().hour
    errs = []
    for ln in lines:
        want = MAP_DIRECTIONS.get(ln, "") if filter_dir else ""
        try:
            buses = _live_bus_dicts(ln, want)
            _pos_cache[ln] = (time.time(), buses)
            out["buses"].extend(buses)
        except (Exception, SystemExit) as exc:  # fail() raises SystemExit
            errs.append(str(exc) or type(exc).__name__)
            ent = _pos_cache.get(ln)
            if ent:
                age = time.time() - ent[0]
                if age <= POS_PROJECT_MAX:
                    out["buses"].extend(_project_buses(ln, ent[1], age, now_hour))
    if errs:
        out["error"] = errs[0]
    if include_stops:
        out["stops"] = cached_stops(primary or (lines[0] if lines else ""))
    return out


def _eta_conf(src, secs, lo, hi):
    """Confidence label for an ETA: 'high' only when it's learned live data with
    a tight spread; 'med' for looser live or schedule; 'low' for raw estimates."""
    if src == "history":
        width = (hi - lo) / secs if secs else 1
        return "high" if width < 0.30 else "med"
    return "med" if src == "schedule" else "low"


# Last good /eta result per (stop, line) — used to dead-reckon approaching buses
# forward when İBB's live feed drops, instead of showing nothing.
_eta_cache: dict = {}
ETA_PROJECT_MAX = 15 * 60   # don't project a stale snapshot older than 15 min

_pred_last: dict = {}        # (kapino, stop) -> epoch of last logged prediction
PRED_LOG_GAP = 90            # don't log a bus's prediction more than once / 90 s


def log_predictions(stop, rows):
    """Record each live ETA we just served for an accuracy self-check later.
    Throttled per bus; skips projected/outage rows (those aren't real forecasts)."""
    if str(stop) not in ACCURACY_STOPS:
        return
    now = time.time()
    batch = []
    for r in rows:
        kap, em, src = r.get("kapino"), r.get("eta_min"), r.get("eta_src")
        if not kap or em is None or src == "projected":
            continue
        key = (kap, str(stop))
        if now - _pred_last.get(key, 0) < PRED_LOG_GAP:
            continue
        _pred_last[key] = now
        batch.append((r.get("line"), kap, str(stop), now + em * 60, now, em, src))
    if batch:
        with _db_lock:
            db().executemany("INSERT INTO pred VALUES (?,?,?,?,?,?,?)", batch)
            db().commit()


def _project_eta(rows, age):
    """Advance a cached approaching-set by `age` seconds: each bus's ETA shrinks
    by the elapsed time; buses whose ETA has run out are dropped (they arrived)."""
    mins = age / 60.0
    out = []
    for r in rows:
        em = r.get("eta_min")
        if em is None:
            continue
        em2 = em - mins
        if em2 < 0:
            continue   # bus has most likely reached the stop already
        nr = dict(r)
        nr["eta_min"] = round(em2)
        if r.get("eta_lo") is not None:
            nr["eta_lo"] = max(0, round(r["eta_lo"] - mins))
        if r.get("eta_hi") is not None:
            nr["eta_hi"] = max(0, round(r["eta_hi"] - mins))
        nr["eta_src"] = "projected"
        nr["eta_conf"] = "low"
        nr["projected"] = True
        out.append(nr)
    out.sort(key=lambda x: x["eta_min"])
    return out


def _horizon_bias(eta_min) -> float:
    """Extra minutes to add to a learned ETA for its lead time: segment-median sums
    are ~unbiased close in, then compound with distance. Piecewise-linear through
    HORIZON_BIAS_ANCHORS; held flat at the last anchor beyond 16 min."""
    if not BIAS_CORRECT or eta_min is None:
        return 0.0
    a = HORIZON_BIAS_ANCHORS
    if eta_min <= a[0][0]:
        return 0.0
    if eta_min >= a[-1][0]:
        return a[-1][1]
    for (x0, y0), (x1, y1) in zip(a, a[1:]):
        if x0 <= eta_min < x1:
            return y0 + (y1 - y0) * (eta_min - x0) / (x1 - x0)
    return 0.0


def _apply_bias_correction(rows) -> float:
    """Land the shown ETA on the actual arrival. 'history'/'schedule' rows get the
    time-of-day floor plus the horizon ramp (grows with lead time); the 'estimate'
    fallback and 'origin-wait' each get their own measured constant, both pulling the
    other way (they ran pessimistic). Outage 'projected' rows are left alone. Returns
    the tuning applied to the soonest learned bus, for the status line."""
    if not BIAS_CORRECT:
        return 0.0
    off = BIAS_OFFSETS.get(_time_bucket(time.time()), 0.0)
    shown = 0.0
    for r in rows:
        src, em = r.get("eta_src"), r.get("eta_min")
        if em is None:
            continue
        if src in ("history", "schedule"):
            adj = off + _horizon_bias(em)
            if shown == 0.0:
                shown = adj
        elif src == "estimate":
            adj = ESTIMATE_BIAS
        elif src == "origin-wait":
            adj = ORIGIN_WAIT_BIAS
        else:
            continue
        if adj == 0:
            continue
        r["eta_min"] = max(0, round(em + adj))
        if r.get("eta_lo") is not None:
            r["eta_lo"] = max(0, round(r["eta_lo"] + adj))
        if r.get("eta_hi") is not None:
            r["eta_hi"] = max(0, round(r["eta_hi"] + adj))
    return round(shown, 1)


def _flag_bunching(rows, gap_min=2):
    """Mark consecutive approaching buses ≤ gap_min apart as bunched — they're
    clumped, so the real wait after them is the gap to the next un-bunched bus."""
    prev = None
    for r in rows:
        em = r.get("eta_min")
        if (prev is not None and em is not None
                and prev.get("eta_min") is not None
                and (em - prev["eta_min"]) <= gap_min):
            r["bunched"] = prev["bunched"] = True
        prev = r


def headway_stats(line, stop, days=21) -> dict:
    """Median minutes between actual arrivals at a stop (from the arr log), per
    time bucket — a frequency predictor ('a bus every ~X min') that beats the
    timetable when buses don't run to a strict clock."""
    since = time.time() - days * 86400
    with _db_lock:
        ts = [r[0] for r in db().execute(
            "SELECT ts FROM arr WHERE line=? AND stop=? AND ts>? ORDER BY ts",
            (line, str(stop), since)).fetchall()]
    buckets: dict = {}
    for a, b in zip(ts, ts[1:]):
        gap = (b - a) / 60.0
        if 1 <= gap <= 60:
            buckets.setdefault(_time_bucket(b), []).append(gap)
    return {k: round(statistics.median(v), 1)
            for k, v in buckets.items() if len(v) >= 5}


def _headway_info(line, stop):
    """(typical headway now, predicted minutes to next bus) from the arrival log."""
    if str(stop) not in ACCURACY_STOPS:
        return None, None
    hw_now = headway_stats(line, stop).get(_time_bucket(time.time()))
    if hw_now is None:
        return None, None
    with _db_lock:
        la = db().execute("SELECT MAX(ts) FROM arr WHERE line=? AND stop=?",
                          (line, str(stop))).fetchone()
    due = round((la[0] + hw_now * 60 - time.time()) / 60) if la and la[0] else None
    return hw_now, due


def _leg_slots(line: str, leg: str, day: str) -> list:
    """Sorted epoch timestamps of every scheduled departure for a leg on a day,
    anchored to today's date. [] if the timetable is unavailable for that leg/day."""
    try:
        dts = {r["DT"] for r in api_planned_times(line)
               if r.get("SYON") == leg and r.get("SGUNTIPI") == day and r.get("DT")}
    except (Exception, SystemExit):
        return []
    base = datetime.now()
    out = []
    for dt in dts:
        try:
            hh, mm = int(dt[:2]), int(dt[3:5])
        except (ValueError, IndexError):
            continue
        day_off, hh = divmod(hh, 24)        # 24:xx after-midnight runs
        out.append((base.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    + timedelta(days=day_off)).timestamp())
    return sorted(out)


def _next_departures(line: str, leg: str, n: int = 1):
    """Up to `n` next scheduled departures [(datetime,'HH:MM'),...] at/after now.
    Returns (list, status): 'ok' = slots found; 'done' = timetable known but no
    departures left today; 'unknown' = timetable unavailable."""
    slots = _leg_slots(line, leg, _today_daycode())
    if not slots:
        return [], "unknown"
    cutoff = time.time() - 60          # 1-min grace for a just-due run
    out = [(datetime.fromtimestamp(s), datetime.fromtimestamp(s).strftime("%H:%M"))
           for s in slots if s >= cutoff][:n]
    return out, ("ok" if out else "done")


_dep_match = {"matched": 0, "dropped": 0}


def _match_scheduled_dep(line, leg, obs_epoch):
    """The scheduled slot an observed origin departure is fulfilling. Buses leave at
    or slightly after their slot, so we match the LATEST slot ≤ obs (90 s early
    grace) — not the symmetric nearest, which would mis-assign a late bus forward to
    the next slot and record it as 'early', pulling the learned median toward zero.
    None (and counted as a drop) if >15 min late or >3 min early → an extra run."""
    d = datetime.fromtimestamp(obs_epoch)
    day = "C" if d.weekday() == 5 else "P" if d.weekday() == 6 else "I"
    cands = _leg_slots(line, leg, day)
    if not cands:
        return None
    prior = [c for c in cands if c <= obs_epoch + 90]
    best = max(prior) if prior else min(cands)   # the slot it's serving
    off = obs_epoch - best
    if off > 900 or off < -180:                  # >15m late / >3m early → ambiguous
        _dep_match["dropped"] += 1
        return None
    _dep_match["matched"] += 1
    return best


def origin_offset_stats(line, days=21) -> dict:
    """Per time-bucket stats on how late (+) / early (−) buses actually leave the
    origin vs schedule (minutes), from the dep log: median offset, sample count n,
    and IQR (spread) so a noisy bucket can be held back from display."""
    since = time.time() - days * 86400
    with _db_lock:
        rows = db().execute("SELECT obs_dep, sched_dep FROM dep WHERE line=? "
                            "AND obs_dep>?", (line, since)).fetchall()
    buckets: dict = {}
    for obs, sched in rows:
        off = (obs - sched) / 60.0
        if abs(off) <= 15:
            buckets.setdefault(_time_bucket(obs), []).append(off)
    out = {}
    for k, v in buckets.items():
        if len(v) >= 4:
            q = statistics.quantiles(v, n=4)     # [p25, p50, p75]
            iqr = q[2] - q[0]
        else:
            iqr = (max(v) - min(v)) if len(v) >= 2 else 0.0
        out[k] = {"offset": round(statistics.median(v), 1),
                  "n": len(v), "iqr": round(iqr, 1)}
    return out


_origin_off_cache: dict = {}


def _cached_offset_stats(line) -> dict:
    """origin_offset_stats(line) memoised 5 min — shared by _origin_offset (the
    correction) and origin_dep_report (/stats), so /stats never re-runs the 21-day
    `dep` scan on every hit (the collectors poll /stats ~once a minute)."""
    ent = _origin_off_cache.get(line)
    if not ent or (time.time() - ent[0]) >= 300:
        ent = (time.time(), origin_offset_stats(line))
        _origin_off_cache[line] = ent
    return ent[1]


def origin_dep_report() -> dict:
    """Snapshot of the origin-departure learning for /stats: how many observed
    departures matched a slot vs were dropped as extras, plus per-bucket offsets."""
    lines = sorted({f["line"] for f in MAP_FOCUS})
    return {"match": dict(_dep_match),
            "offset": {ln: _cached_offset_stats(ln) for ln in lines}}


def _origin_offset(line):
    """(offset_min, n) for the current time bucket — the learned origin-departure
    lateness, cached 5 min. Applies the correction ONLY when a bucket has ≥10
    samples AND a tight spread (IQR ≤ 4 min); otherwise (0.0, n) so a thin or noisy
    bucket never surfaces a confident-wrong number."""
    b = _cached_offset_stats(line).get(_time_bucket(time.time()))
    if b and b["n"] >= 10 and b.get("iqr", 99) <= 4.0:
        return b["offset"], b["n"]
    return 0.0, (b["n"] if b else 0)


def _apply_origin_wait(rows, lines) -> bool:
    """A bus sitting at the route origin (sira 1) hasn't started its run yet — it
    can dwell several minutes filling up before departing, so `stops_away` badly
    understates the wait. Replace its ETA with (next scheduled departure − now) +
    the origin→stop travel time and flag it so the UI shows 'waiting at origin'."""
    touched = False
    now = datetime.now()
    # group at-origin buses by leg so several waiting buses share out DISTINCT
    # slots instead of all claiming the same next departure (which would stack
    # them on one minute and collapse them in the UI).
    groups: dict = {}
    for r in rows:
        if r.get("near_sira") != 1 or r.get("eta_min") is None:
            continue
        if r.get("target_sira") in (None, 1):
            continue   # the stop itself is the origin — nothing to wait through
        groups.setdefault((r["line"], r.get("leg", "")), []).append(r)
    for (line, leg), grp in groups.items():
        # FIFO at the terminus: the bus confirmed at origin longest leaves first
        grp.sort(key=lambda r: _origin_since.get(r.get("kapino", ""), float("inf")))
        deps, status = _next_departures(line, leg, len(grp))
        off, _n = _origin_offset(line)      # learned lateness (0 until mature)
        for i, r in enumerate(grp):
            travel = r["eta_min"]           # learned origin→stop run time
            if i < len(deps):
                dep_adj = deps[i][0] + timedelta(minutes=off)
                depart_in = max(0.0, (dep_adj - now).total_seconds() / 60.0)
                eta = round(depart_in + travel)
                r["eta_min"] = eta
                r["eta_lo"] = max(round(travel), eta - 3)   # could pull out soon
                r["eta_hi"] = eta + 3                        # or dwell past sched
                r["depart_hhmm"] = dep_adj.strftime("%H:%M")
                if off:
                    r["dep_offset"] = off   # schedule nudged by measured lateness
            elif status == "unknown":
                # timetable unavailable → can't name a departure, just widen + flag
                r["eta_hi"] = (r["eta_hi"] if r.get("eta_hi") is not None
                               else travel) + 6
                r["depart_hhmm"] = None
            else:
                # timetable known but no slot left for this bus today (last run gone,
                # or more buses parked than departures remaining) → honest "no more"
                r["no_more"] = True
                r["eta_min"] = None
                r["depart_hhmm"] = None
            r["at_origin"] = True
            r["origin_name"] = r.get("near_name")
            r["eta_src"] = "origin-wait"    # own accuracy bucket + own bias constant
            r["eta_conf"] = "low"
            touched = True
    return touched


def eta_payload(stop: str, lines: list) -> dict:
    """Buses approaching `stop`. ETA comes from learned stop-to-stop times when
    we have enough history (eta_src='history'); otherwise it falls back to a
    distance/speed estimate (eta_src='estimate'). When İBB's live feed is down we
    dead-reckon the last good result forward (eta_src='projected')."""
    try:
        rows = approaching(stop, lines)
        now = datetime.now()
        now_hour, now_dow = now.hour, now.weekday()
        memo = {}   # share segment lookups across all approaching buses
        for r in rows:
            band = hist_eta_band(r["line"], r.get("leg", ""),
                                 r.get("near_sira"), r.get("target_sira"),
                                 now_hour, memo, now_dow)
            if band is not None:
                secs, lo, hi, src = band
                r["eta_min"], r["eta_src"] = round(secs / 60), src
                r["eta_lo"], r["eta_hi"] = round(lo / 60), round(hi / 60)
                r["eta_conf"] = _eta_conf(src, secs, lo, hi)
            else:
                d = r.get("dist_km")
                r["eta_min"] = round(d / ETA_SPEED_KMH * 60) if d else None
                r["eta_src"] = "estimate"
                r["eta_lo"] = r["eta_hi"] = None
                r["eta_conf"] = "low"
        # a bus parked at the origin terminus departs on the timetable, not "soon"
        _apply_origin_wait(rows, lines)
        # re-sort by real ETA: the origin bus is no longer "next" if a bus already
        # on the road arrives sooner (the whole point of the correction)
        rows.sort(key=lambda r: r["eta_min"] if r.get("eta_min") is not None else 1e9)
        with _db_lock:   # when did a bus last actually reach this stop?
            lp = db().execute("SELECT ts FROM hops WHERE to_code=? ORDER BY ts DESC "
                              "LIMIT 1", (stop,)).fetchone()
        last_passed = lp[0][11:16] if lp else None
        if lines:
            _eta_cache[(stop, lines[0])] = (time.time(), rows, last_passed)
        bias_adj = _apply_bias_correction(rows)   # correct, THEN log/show the corrected ETA
        try:
            log_predictions(stop, rows)
        except Exception:
            pass   # accuracy logging must never break the ETA response
        _flag_bunching(rows)
        hw_now, due_min = _headway_info(lines[0], stop) if lines else (None, None)
        return {"stop": stop, "rows": rows, "error": None, "last_passed": last_passed,
                "headway_min": hw_now, "due_min": due_min, "bias_adj": bias_adj}
    except (Exception, SystemExit) as exc:
        hw_now, due_min = _headway_info(lines[0], stop) if lines else (None, None)
        base = {"stop": stop, "headway_min": hw_now, "due_min": due_min}
        ent = _eta_cache.get((stop, lines[0])) if lines else None
        if ent and (time.time() - ent[0]) <= ETA_PROJECT_MAX:
            age = time.time() - ent[0]
            proj = _project_eta(ent[1], age)
            if proj:
                return {**base, "rows": proj, "error": None,
                        "last_passed": ent[2], "stale": True, "age": round(age)}
        return {**base, "rows": [], "error": str(exc) or type(exc).__name__}


def _today_daycode() -> str:
    wd = datetime.now().weekday()           # İETT: I=weekday, C=Saturday, P=Sunday
    return "C" if wd == 5 else "P" if wd == 6 else "I"


def scheduled_arrivals(line: str, home_code: str, n: int = 6) -> dict:
    """Upcoming scheduled arrivals at `home_code` = timetable departure from the
    origin + our modelled travel time to the stop. Works off the timetable, so it
    still produces predictions when live tracking is down."""
    try:
        legs = route_legs(line)
        leg = home_sira = None
        for lg, d in legs.items():
            if str(home_code) in d["by_code"]:
                leg, home_sira = lg, d["by_code"][str(home_code)]
                break
        if leg is None:
            return {"rows": [], "error": "stop not on line"}
        day = _today_daycode()
        deps = sorted({r["DT"] for r in api_planned_times(line)
                       if r.get("SYON") == leg and r.get("SGUNTIPI") == day and r.get("DT")})
        now = datetime.now()
        travel_by_hour: dict = {}
        out = []
        for dt in deps:
            try:
                hh, mm = int(dt[:2]), int(dt[3:5])
            except (ValueError, IndexError):
                continue
            day_off, hh = divmod(hh, 24)        # handle 24:xx after-midnight runs
            dep = now.replace(hour=hh, minute=mm, second=0, microsecond=0) \
                  + timedelta(days=day_off)
            if hh not in travel_by_hour:
                secs, _ = hist_eta_seconds(line, leg, 1, home_sira, hh,
                                           dow=now.weekday())
                travel_by_hour[hh] = secs
            secs = travel_by_hour[hh]
            if secs is None:
                continue
            arr = dep + timedelta(seconds=secs)
            in_min = round((arr - now).total_seconds() / 60)
            if in_min >= 0:
                out.append({"dep": dt, "arr": arr.strftime("%H:%M"), "in_min": in_min})
        out.sort(key=lambda x: x["in_min"])
        return {"rows": out[:n], "error": None, "day": day}
    except (Exception, SystemExit) as exc:
        return {"rows": [], "error": str(exc) or type(exc).__name__}


# ──────────────────────────────────────────────────────────────────────────────
# Learned travel times — sample the live feed, record stop→stop segment times,
# and use their medians to produce sharper ETAs than a flat average speed.
# ──────────────────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()
_db = None
_legs_cache: dict = {}
_collector_state: dict = {}   # kapino -> (line, leg, sira, epoch_seconds)
_origin_since: dict = {}      # kapino -> epoch first confirmed at origin (sira 1)


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(SEG_DB, check_same_thread=False)
        # WAL: far more resilient to the ungraceful shutdowns this box suffers
        # (battery death, lid-close, hard reboots) than the default rollback
        # journal, and lets the web server read while the collector writes.
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")   # durable across app crashes
        _db.execute("""CREATE TABLE IF NOT EXISTS hops(
            line TEXT, leg TEXT, from_code TEXT, to_code TEXT,
            seconds REAL, dow INTEGER, hour INTEGER, ts TEXT)""")
        _db.execute("""CREATE INDEX IF NOT EXISTS idx_hop
            ON hops(line, leg, from_code, to_code)""")
        # baseline per-segment times that live data overrides once a segment has
        # enough real observations. hour = -1 is the all-day schedule baseline;
        # hour 0..23 are traffic-adjusted per-hour baselines.
        _db.execute("""CREATE TABLE IF NOT EXISTS seed(
            line TEXT, leg TEXT, from_code TEXT, to_code TEXT, hour INTEGER,
            seconds REAL, source TEXT,
            PRIMARY KEY(line, leg, from_code, to_code, hour))""")
        cols = [r[1] for r in _db.execute("PRAGMA table_info(seed)")]
        if "hour" not in cols:   # migrate an older (hour-less) seed table
            _db.execute("DROP TABLE seed")
            _db.execute("""CREATE TABLE seed(
                line TEXT, leg TEXT, from_code TEXT, to_code TEXT, hour INTEGER,
                seconds REAL, source TEXT,
                PRIMARY KEY(line, leg, from_code, to_code, hour))""")
        # Accuracy self-check: every ETA we serve (pred) and every time a bus
        # actually crosses a tracked stop (arr). Matched by kapino to score how
        # close predictions land. made/pred_arr/ts are epoch seconds.
        _db.execute("""CREATE TABLE IF NOT EXISTS pred(
            line TEXT, kapino TEXT, stop TEXT, pred_arr REAL, made REAL,
            eta_min INTEGER, src TEXT)""")
        _db.execute("""CREATE INDEX IF NOT EXISTS idx_pred
            ON pred(kapino, stop, made)""")
        _db.execute("""CREATE TABLE IF NOT EXISTS arr(
            line TEXT, kapino TEXT, stop TEXT, ts REAL)""")
        _db.execute("""CREATE INDEX IF NOT EXISTS idx_arr
            ON arr(kapino, stop, ts)""")
        # Origin departures: when a bus actually pulls out of the terminus (sira 1)
        # vs its matched scheduled departure — to learn the real on-time offset and
        # factor it into the 'waiting at origin' ETA. offset = obs_dep − sched_dep.
        _db.execute("""CREATE TABLE IF NOT EXISTS dep(
            line TEXT, leg TEXT, kapino TEXT, sched_dep REAL, obs_dep REAL,
            dow INTEGER, hour INTEGER)""")
        _db.execute("""CREATE INDEX IF NOT EXISTS idx_dep ON dep(line, obs_dep)""")
        _db.commit()
    return _db


def route_legs(line: str) -> dict:
    """Per-leg ordered stop sequence for a line: {leg: {by_sira, by_code, order}}."""
    ent = _legs_cache.get(line)
    if ent and (time.time() - ent[0]) < 3600:
        return ent[1]
    legs: dict = {}
    for r in cached_route(line):
        leg = (r.get("YON") or "").upper()[:1]
        try:
            sira = int(r.get("SIRANO") or 0)
        except (TypeError, ValueError):
            continue
        code = str(r.get("DURAKKODU"))
        d = legs.setdefault(leg, {"by_sira": {}, "by_code": {}})
        d["by_sira"][sira] = code
        d["by_code"][code] = sira
    _legs_cache[line] = (time.time(), legs)
    return legs


def _parse_ts(s: str) -> float | None:
    """İETT 'son_konum_zamani' like '2026-06-12 11:15:54' → epoch seconds. All
    timestamps share one timezone, so deltas between them are tz-independent."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, AttributeError):
        return None


def record_hops(line: str) -> int:
    """One collector sample: read live buses, infer the consecutive stop hops
    each bus has advanced through since we last saw it, and store their times."""
    legs = route_legs(line)
    rows = []
    arr_rows = []   # actual arrivals at tracked stops, for the accuracy check
    dep_rows = []   # actual origin departures, matched to the timetable
    for b in api_live_buses(line):  # may raise on API outage; caller guards
        kapino = b.get("kapino")
        leg = "G" if "_G_" in (b.get("guzergahkodu") or "") else \
              "D" if "_D_" in (b.get("guzergahkodu") or "") else ""
        legmap = legs.get(leg)
        if not kapino or not legmap:
            continue
        sira = legmap["by_code"].get(str(b.get("yakinDurakKodu")))
        epoch = _parse_ts(b.get("son_konum_zamani") or "")
        if sira is None or epoch is None:
            continue
        if sira == 1:                       # track how long it's been at the origin
            _origin_since.setdefault(kapino, epoch)   # (so multiple waiters can be
        else:                               #  FIFO-ordered onto distinct slots)
            _origin_since.pop(kapino, None)
        prev = _collector_state.get(kapino)
        _collector_state[kapino] = (line, leg, sira, epoch)
        if not prev:
            continue
        p_line, p_leg, p_sira, p_epoch = prev
        if p_line != line or p_leg != leg:
            continue
        steps, dt = sira - p_sira, epoch - p_epoch
        # only forward moves of a few stops in a sane time window are usable
        if steps <= 0 or steps > 8 or dt <= 0 or dt > 1800:
            continue
        per_hop = dt / steps
        when = datetime.fromtimestamp(epoch)
        for k in range(p_sira, sira):
            fc, tc = legmap["by_sira"].get(k), legmap["by_sira"].get(k + 1)
            if fc and tc:
                rows.append((line, leg, fc, tc, per_hop, when.weekday(), when.hour,
                             when.isoformat(timespec="seconds")))
        # did this bus just cross one of the stops we predict for? log the
        # (interpolated) moment it did, to score past predictions against.
        for ts_code in ACCURACY_STOPS:
            ts_sira = legmap["by_code"].get(str(ts_code))
            if ts_sira is not None and p_sira < ts_sira <= sira:
                cross = p_epoch + (ts_sira - p_sira) / steps * (epoch - p_epoch)
                arr_rows.append((line, kapino, str(ts_code), cross))
        # did it just pull OUT of the origin terminus (sira 1 → 2+)? record the
        # real departure vs its scheduled slot, to learn how on-time departures are.
        # True departure is between the last-at-origin fix (p_epoch) and the first-
        # gone fix (epoch); use the MIDPOINT (capped at one ~60s batch) so we don't
        # bake the up-to-60s polling lag into the very lateness we're measuring.
        if p_sira == 1 and sira > 1:
            obs_dep = p_epoch + min((epoch - p_epoch) / 2.0, 60.0)
            sched = _match_scheduled_dep(line, leg, obs_dep)
            if sched is not None:
                w = datetime.fromtimestamp(obs_dep)
                dep_rows.append((line, leg, kapino, sched, obs_dep,
                                 w.weekday(), w.hour))
    if rows or arr_rows or dep_rows:
        with _db_lock:
            if rows:
                db().executemany("INSERT INTO hops VALUES (?,?,?,?,?,?,?,?)", rows)
            if arr_rows:
                db().executemany("INSERT INTO arr VALUES (?,?,?,?)", arr_rows)
            if dep_rows:
                db().executemany("INSERT INTO dep VALUES (?,?,?,?,?,?,?)", dep_rows)
            db().commit()
    return len(rows)


OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
_road_cache: dict = {}


def osrm_route(coords):
    """Road route through `coords` [(lat,lon),...]. Returns (per-leg km list,
    geometry [(lat,lon),...]) or None. coords sent to OSRM as lon,lat."""
    if len(coords) < 2:
        return None
    cs = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{OSRM_URL}{cs}?overview=full&geometries=geojson&annotations=distance"
    try:
        d = _session.get(url, timeout=60).json()
        if d.get("code") != "Ok":
            return None
        route = d["routes"][0]
        leg_km = [lg["distance"] / 1000 for lg in route["legs"]]
        geom = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
        return leg_km, geom
    except Exception:
        return None


def _road_disk_path(line: str) -> Path:
    return APP_DIR / f"road_{line}.json"


def _road_to_json(out: dict) -> dict:
    return {leg: {"dist": [[c1, c2, km] for (c1, c2), km in info["dist"].items()],
                  "geom": [[a, b] for a, b in info["geom"]], "road": info["road"]}
            for leg, info in out.items()}


def _road_from_json(j: dict) -> dict:
    return {leg: {"dist": {(d[0], d[1]): d[2] for d in info["dist"]},
                  "geom": [(p[0], p[1]) for p in info["geom"]], "road": info["road"]}
            for leg, info in j.items()}


def road_data(line: str) -> dict:
    """Per-leg real road distances and geometry for a line, via OSRM. Cached in
    memory AND on disk (~/.iett_road_<line>.json), so the flaky public OSRM demo
    server is hit at most once ever per line — never on a restart or new host.
    Falls back to straight-line distance if OSRM is unavailable (not persisted, so
    it retries OSRM next time). Returns {leg: {dist:{(from,to):km}, geom, road}}."""
    ent = _road_cache.get(line)
    if ent and (time.time() - ent[0]) < 86400:
        return ent[1]
    disk = _road_disk_path(line)
    if disk.exists():
        try:
            out = _road_from_json(json.loads(disk.read_text(encoding="utf-8")))
            if out:
                _road_cache[line] = (time.time(), out)
                return out
        except (ValueError, KeyError, IndexError, OSError):
            pass   # corrupt/old file — recompute below
    legs_stops: dict = {}
    for r in cached_route(line):
        leg = (r.get("YON") or "").upper()[:1]
        try:
            sira = int(r.get("SIRANO") or 0)
            lat = float(r["YKOORDINATI"]); lon = float(r["XKOORDINATI"])
        except (TypeError, ValueError, KeyError):
            continue
        legs_stops.setdefault(leg, []).append((sira, str(r.get("DURAKKODU")), lat, lon))
    out: dict = {}
    for leg, stops in legs_stops.items():
        stops.sort()
        coords = [(la, lo) for _, _, la, lo in stops]
        res = osrm_route(coords)
        dist = {}
        if res and len(res[0]) == len(stops) - 1:
            leg_km, geom = res
            for (_, c1, la1, lo1), (_, c2, la2, lo2), km in zip(stops, stops[1:], leg_km):
                st = haversine_km(la1, lo1, la2, lo2)
                # guard: OSRM sometimes detours wildly off a bad/off-road stop
                # coordinate (12 km between stops 0.3 km apart) — trust straight-
                # line when the road figure is implausibly large.
                if st > 0 and km > max(0.5, 4 * st):
                    km = st
                dist[(c1, c2)] = km
            out[leg] = {"dist": dist, "geom": geom, "road": True}
        else:   # OSRM unavailable → straight-line fallback
            for (_, c1, la1, lo1), (_, c2, la2, lo2) in zip(stops, stops[1:]):
                dist[(c1, c2)] = haversine_km(la1, lo1, la2, lo2)
            out[leg] = {"dist": dist, "geom": coords, "road": False}
    _road_cache[line] = (time.time(), out)
    if any(info["road"] for info in out.values()):   # only persist real OSRM data
        try:
            _road_disk_path(line).write_text(
                json.dumps(_road_to_json(out)), encoding="utf-8")
        except OSError:
            pass
    return out


def seed_schedule(line: str) -> int:
    """Build a baseline per-segment time for a line from the official scheduled
    trip duration (GetHat_json · SEFER_SURESI), distributed across stops in
    proportion to each segment's *real road* length (OSRM, straight-line if
    unavailable) so curvy/highway segments get their fair share. Returns rows."""
    info = api_line_info(line)
    try:
        total_min = float((info or [{}])[0].get("SEFER_SURESI") or 0)
    except (TypeError, ValueError):
        total_min = 0.0
    if total_min <= 0:
        return 0
    rows, total_sec = [], total_min * 60
    for leg, info_leg in road_data(line).items():
        dist = info_leg["dist"]
        dist_sum = sum(dist.values())
        if dist_sum <= 0:
            continue
        for (c1, c2), d in dist.items():   # hour -1 = all-day baseline; time ∝ road km
            rows.append((line, leg, c1, c2, -1,
                         max(15.0, total_sec * d / dist_sum), "schedule"))
    if rows:
        with _db_lock:
            db().executemany(
                "INSERT OR REPLACE INTO seed VALUES (?,?,?,?,?,?,?)", rows)
            db().commit()
    return len(rows)


def _live_median(cur, line, leg, fc, tc, hours=None):
    """Median of live hop times for a segment, optionally restricted to `hours`."""
    if hours is None:
        rows = cur.execute(
            "SELECT seconds FROM hops WHERE line=? AND leg=? AND from_code=? "
            "AND to_code=? ORDER BY ts DESC LIMIT 300", (line, leg, fc, tc))
    else:
        ph = ",".join("?" * len(hours))
        rows = cur.execute(
            f"SELECT seconds FROM hops WHERE line=? AND leg=? AND from_code=? "
            f"AND to_code=? AND hour IN ({ph}) ORDER BY ts DESC LIMIT 300",
            (line, leg, fc, tc, *hours))
    vals = [r[0] for r in rows]
    return statistics.median(vals) if len(vals) >= MIN_SAMPLES else None


def geohash_encode(lat: float, lon: float, precision: int = 6) -> str:
    """Standard geohash, matching İBB's traffic-density GEOHASH field."""
    lat_iv, lon_iv = [-90.0, 90.0], [-180.0, 180.0]
    gh, bit, ch, even = [], 0, 0, True
    while len(gh) < precision:
        if even:
            mid = (lon_iv[0] + lon_iv[1]) / 2
            if lon > mid:
                ch |= (16 >> bit); lon_iv[0] = mid
            else:
                lon_iv[1] = mid
        else:
            mid = (lat_iv[0] + lat_iv[1]) / 2
            if lat > mid:
                ch |= (16 >> bit); lat_iv[0] = mid
            else:
                lat_iv[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            gh.append(_GEOHASH_B32[ch]); bit, ch = 0, 0
    return "".join(gh)


def latest_traffic_url() -> str | None:
    """Resolve the newest monthly traffic CSV (traffic_density_YYYYMM.csv) from
    the İBB open-data package, so re-seeding always uses the freshest month."""
    try:
        data = _session.get(
            f"https://data.ibb.gov.tr/api/3/action/package_show?id={TRAFFIC_PACKAGE}",
            timeout=30).json()
        best_key, best_url = "", None
        for res in data.get("result", {}).get("resources", []):
            url = res.get("url", "")
            m = re.search(r"traffic_density_(\d{6})\.csv", url)
            if m and m.group(1) > best_key:
                best_key, best_url = m.group(1), url
        return best_url
    except Exception:
        return None


def seed_traffic(line: str, csv_src: str = TRAFFIC_CSV_URL) -> dict:
    """Build a time-of-day congestion prior for one line from İBB's hourly
    traffic-density data, applied as an hourly multiplier on the schedule
    baseline (road speed ≠ bus speed, so we use the *relative* hourly variation
    only). `csv_src` may be a URL or a local path. Streams the file."""
    mm = re.search(r"(\d{6})", csv_src)
    month = mm.group(1) if mm else "?"
    src_tag = f"traffic:{month}"
    if seed_count(line) == 0:        # need the all-day baseline first
        seed_schedule(line)
    with _db_lock:
        base = {(leg, fc, tc): sec for leg, fc, tc, sec in db().execute(
            "SELECT leg, from_code, to_code, seconds FROM seed "
            "WHERE line=? AND hour=-1", (line,))}
    # each segment → the geohash-6 cell of its midpoint (+ build the cell set).
    # The cell set is widened along the *real road* geometry so highway stretches
    # between far-apart stops still pick up their traffic cells.
    rd = road_data(line)
    seg_gh, cells = {}, set()
    legs: dict = {}
    for r in cached_route(line):
        leg = (r.get("YON") or "").upper()[:1]
        try:
            sira = int(r.get("SIRANO") or 0)
            lat = float(r.get("YKOORDINATI")); lon = float(r.get("XKOORDINATI"))
        except (TypeError, ValueError):
            continue
        legs.setdefault(leg, []).append((sira, str(r.get("DURAKKODU")), lat, lon))
    for leg, stops in legs.items():
        stops.sort()
        for (s1, c1, la1, lo1), (s2, c2, la2, lo2) in zip(stops, stops[1:]):
            gh = geohash_encode((la1 + la2) / 2, (lo1 + lo2) / 2, 6)
            seg_gh[(leg, c1, c2)] = gh
            cells.add(gh)
        for la, lo in rd.get(leg, {}).get("geom", []):   # the road corridor
            cells.add(geohash_encode(la, lo, 6))

    # stream the CSV, weighted-average speed per (geohash, hour) and per geohash
    sum_h: dict = {}   # (gh,hour) -> [speed*veh, veh]
    sum_all: dict = {}  # gh -> [speed*veh, veh]
    fh = (requests.get(csv_src, stream=True, timeout=120).iter_lines(decode_unicode=True)
          if csv_src.startswith("http") else open(csv_src, encoding="utf-8-sig"))
    rows_seen = 0
    try:
        header = None
        for raw in fh:
            if not raw:
                continue
            rows_seen += 1
            if header is None and "GEOHASH" in raw:
                header = True
                continue
            parts = [p.strip().strip('"') for p in raw.split(",")]
            if len(parts) < 8:
                continue
            gh = parts[3]
            if gh not in cells:
                continue
            try:
                hour = int(parts[0][11:13])
                spd = float(parts[6]); veh = float(parts[7]) or 1.0
            except (ValueError, IndexError):
                continue
            for key, store in (((gh, hour), sum_h), (gh, sum_all)):
                s = store.setdefault(key, [0.0, 0.0])
                s[0] += spd * veh; s[1] += veh
    finally:
        if not csv_src.startswith("http"):
            fh.close()

    def mean(store, key):
        s = store.get(key)
        return (s[0] / s[1]) if s and s[1] else None

    rows, written = [], 0
    for (leg, c1, c2), gh in seg_gh.items():
        base_sec = base.get((leg, c1, c2))
        overall = mean(sum_all, gh)
        if base_sec is None or not overall:
            continue
        for hour in range(24):
            hm = mean(sum_h, (gh, hour))
            if not hm:
                continue
            factor = min(2.5, max(0.5, overall / hm))   # >1 when slower than usual
            rows.append((line, leg, c1, c2, hour, max(15.0, base_sec * factor),
                         src_tag))
            written += 1
    if rows:
        with _db_lock:
            db().executemany("INSERT OR REPLACE INTO seed VALUES (?,?,?,?,?,?,?)", rows)
            db().commit()
    return {"cells": len(cells), "segments": len(seg_gh), "hourly_rows": written,
            "month": month}


def _segment_seconds(cur, line, leg, fc, tc, hour=None, memo=None):
    """Best per-segment time (see _compute_segment), memoised within one request
    so segments shared by several approaching buses are computed only once."""
    if memo is not None:
        key = (leg, fc, tc, hour)
        if key in memo:
            return memo[key]
        res = _compute_segment(cur, line, leg, fc, tc, hour)
        memo[key] = res
        return res
    return _compute_segment(cur, line, leg, fc, tc, hour)


def _compute_segment(cur, line, leg, fc, tc, hour=None):
    """Best per-segment time, in priority order:
      1. live data from the same hour-of-day (then ±1 h), so rush hour uses
         rush-hour observations,
      2. live data from any hour,
      3. the schedule seed.
    Returns (seconds, source) or (None, None)."""
    if hour is not None:
        for hours in ([hour], [(hour - 1) % 24, hour, (hour + 1) % 24]):
            m = _live_median(cur, line, leg, fc, tc, hours)
            if m is not None:
                return m, "live"
    m = _live_median(cur, line, leg, fc, tc, None)
    if m is not None:
        return m, "live"
    # baseline seed: prefer this hour's traffic-adjusted value, else all-day
    for h in ([hour, -1] if hour is not None else [-1]):
        row = cur.execute(
            "SELECT seconds FROM seed WHERE line=? AND leg=? AND from_code=? "
            "AND to_code=? AND hour=?", (line, leg, fc, tc, h)).fetchone()
        if row:
            return row[0], "seed"
    return None, None


def _live_values(cur, line, leg, fc, tc, hours=None, dows=None):
    """Raw live hop times for a segment (for percentile/range computation).
    Optionally restricted to specific hours and/or weekdays (dow 0=Mon..6=Sun)."""
    clauses = ["line=?", "leg=?", "from_code=?", "to_code=?"]
    params = [line, leg, fc, tc]
    if hours is not None:
        clauses.append("hour IN (%s)" % ",".join("?" * len(hours)))
        params += list(hours)
    if dows is not None:
        clauses.append("dow IN (%s)" % ",".join("?" * len(dows)))
        params += list(dows)
    rows = cur.execute(
        "SELECT seconds FROM hops WHERE " + " AND ".join(clauses)
        + " ORDER BY ts DESC LIMIT 300", params)
    return [r[0] for r in rows]


def _dow_class(dow):
    """Weekday (Mon–Fri) vs weekend (Sat/Sun) bucket, as a tuple of dows —
    travel times differ between the two, so we prefer same-bucket samples."""
    return (5, 6) if dow in (5, 6) else (0, 1, 2, 3, 4)


def _band(vals):
    """(median, low, high) from a sample, using the inter-quartile range as the
    'typical' band. None if too few samples."""
    if len(vals) < MIN_SAMPLES:
        return None
    med = statistics.median(vals)
    if len(vals) >= 4:
        q = statistics.quantiles(vals, n=4)   # [p25, p50, p75]
        return med, q[0], q[2]
    return med, min(vals), max(vals)


def _segment_band(cur, line, leg, fc, tc, hour=None, dow=None):
    """Per-segment (median, low, high, source). Priority: same weekday-bucket +
    same hour → any day + same hour → same bucket ±1 h → any day ±1 h → any-hour
    live → schedule/traffic seed. The weekday split is a *soft* preference: it's
    only used when it has enough samples, otherwise we relax to all days so
    coverage never collapses on a thin dataset."""
    wd = _dow_class(dow) if dow is not None else None
    if hour is not None:
        nbr = [(hour - 1) % 24, hour, (hour + 1) % 24]
        tiers = ([([hour], wd), ([hour], None), (nbr, wd), (nbr, None)]
                 if wd is not None else [([hour], None), (nbr, None)])
        for hours, dows in tiers:
            b = _band(_live_values(cur, line, leg, fc, tc, hours, dows))
            if b:
                return (*b, "live")
    b = _band(_live_values(cur, line, leg, fc, tc, None))
    if b:
        return (*b, "live")
    for h in ([hour, -1] if hour is not None else [-1]):
        row = cur.execute(
            "SELECT seconds FROM seed WHERE line=? AND leg=? AND from_code=? "
            "AND to_code=? AND hour=?", (line, leg, fc, tc, h)).fetchone()
        if row:
            return row[0], row[0], row[0], "seed"
    return None, None, None, None


def hist_eta_band(line: str, leg: str, near_sira, home_sira, hour=None, memo=None,
                  dow=None):
    """Sum of per-segment times from the bus's current stop to the home stop, as
    (seconds, low, high, src). 'history' if any segment used live data, else
    'schedule'. None if coverage is poor (<60%) — caller falls back to distance."""
    if not leg or near_sira is None or home_sira is None or home_sira <= near_sira:
        return None
    legmap = route_legs(line).get(leg)
    if not legmap:
        return None
    total = hiqr_sq = 0.0
    covered = segments = live_used = 0
    with _db_lock:
        cur = db()
        for k in range(near_sira, home_sira):
            fc, tc = legmap["by_sira"].get(k), legmap["by_sira"].get(k + 1)
            if not (fc and tc):
                return None
            segments += 1
            key = (leg, fc, tc, hour, dow)
            if memo is not None and key in memo:
                b = memo[key]
            else:
                b = _segment_band(cur, line, leg, fc, tc, hour, dow)
                if memo is not None:
                    memo[key] = b
            m, l, h, src = b
            if m is not None:
                total += m
                hiqr_sq += ((h - l) / 2.0) ** 2   # per-segment half-IQR
                covered += 1
                if src == "live":
                    live_used += 1
    if segments == 0 or covered / segments < 0.6:
        return None
    if covered < segments:        # scale up to cover the few unknown hops
        f = segments / covered
        total *= f; hiqr_sq *= f
    # Combine per-segment spreads in QUADRATURE, not by direct sum: stop-to-stop
    # delays partly cancel (one slow, one fast), so journey variance = sum of
    # segment variances, and the band ∝ sqrt of that — far tighter and honest.
    # BAND_CORR_INFLATE adds a little back for correlated congestion on bad runs.
    half_iqr = math.sqrt(hiqr_sq) * BAND_CORR_INFLATE
    return total, max(0.0, total - half_iqr), total + half_iqr, \
        ("history" if live_used else "schedule")


def hist_eta_seconds(line: str, leg: str, near_sira, home_sira, hour=None, memo=None,
                     dow=None):
    """Point ETA only (seconds, src) — thin wrapper over hist_eta_band."""
    b = hist_eta_band(line, leg, near_sira, home_sira, hour, memo, dow)
    return (b[0], b[3]) if b else (None, None)


def _summ(xs):
    """Summarise a list of signed errors (minutes): count, mean-abs, bias, p90."""
    if not xs:
        return None
    absx = sorted(abs(x) for x in xs)
    return {"n": len(xs),
            "mae": round(statistics.mean(absx), 1),
            "bias": round(statistics.mean(xs), 1),
            "p90": round(absx[min(len(absx) - 1, int(len(absx) * 0.9))], 1)}


def _time_bucket(epoch) -> str:
    """Bucket a moment for time-dependent accuracy: weekend, weekday-rush
    (within RUSH_WINDOWS), or weekday-daytime. The optimism lean differs across
    these, so they're scored separately (and a future correction reads per-bucket)."""
    dt = datetime.fromtimestamp(epoch)
    if dt.weekday() >= 5:
        return "weekend"
    return "weekday-rush" if any(a <= dt.hour < b for a, b in RUSH_WINDOWS) \
        else "weekday-daytime"


def accuracy_stats(stop=None, days=30) -> dict:
    """Score logged predictions against actual arrivals (matched by kapino).
    error = predicted_arrival − actual_arrival, in minutes: a positive bias means
    buses tend to arrive *earlier* than predicted, negative means we run optimistic
    (bus arrives later than shown). 'near10' restricts to predictions made within
    10 min of arrival — the lead time that actually matters at the stop. 'by_bucket'
    splits that by time-of-week so the lean can be corrected per context later."""
    since = time.time() - days * 86400
    with _db_lock:
        cur = db()
        aq = "SELECT line,kapino,stop,ts FROM arr WHERE ts>?"
        ap = [since]
        if stop:
            aq += " AND stop=?"
            ap.append(str(stop))
        arrs = cur.execute(aq, ap).fetchall()
        preds = cur.execute("SELECT kapino,stop,pred_arr,made,src FROM pred "
                            "WHERE made>?", (since,)).fetchall()
    pidx: dict = {}
    for kap, st, pa, made, src in preds:
        pidx.setdefault((kap, st), []).append((made, pa, src))
    errs, near = [], []
    by_src: dict = {}
    by_bucket: dict = {}
    for line, kap, st, ts in arrs:
        for made, pa, src in pidx.get((kap, st), []):
            if not (ts - 3600 < made < ts):
                continue
            e = (pa - ts) / 60.0
            errs.append(e)
            by_src.setdefault(src, []).append(e)
            if (ts - made) / 60.0 <= 10:   # the lead time we'd actually correct
                near.append(e)
                by_bucket.setdefault(_time_bucket(ts), []).append(e)
    return {"overall": _summ(errs), "near10": _summ(near),
            "by_src": {k: _summ(v) for k, v in by_src.items()},
            "by_bucket": {k: _summ(v) for k, v in by_bucket.items()},
            "bucket_now": _time_bucket(time.time()),
            "matched_arrivals": len(arrs), "predictions": len(preds)}


def backtest_accuracy(line, stop, n=3000, target_lead=600) -> dict:
    """Estimate ETA error from the *historical* hop data (no logged predictions
    needed). Builds the ~`target_lead`-seconds-out journey to `stop`, then Monte-
    Carlo-simulates it `n` times by drawing a real recorded time for each segment,
    comparing each simulated actual journey to the model's prediction (sum of
    medians). error = predicted − actual minutes (− = optimistic). Assumes
    segments vary independently, so it's an optimistic floor (real correlated-delay
    days are a bit worse) — labelled 'modelled' in the UI to stay honest."""
    import random
    legs = route_legs(line)
    leg = home_sira = None
    for lg, d in legs.items():
        if str(stop) in d["by_code"]:
            leg, home_sira = lg, d["by_code"][str(stop)]
            break
    if leg is None:
        return None
    lm = legs[leg]
    seg_samples, predicted, k, live_segs = [], 0.0, home_sira, 0
    with _db_lock:
        cur = db()
        while k - 1 >= 1 and predicted < target_lead:
            fc, tc = lm["by_sira"].get(k - 1), lm["by_sira"].get(k)
            if not (fc and tc):
                break
            vals = _live_values(cur, line, leg, fc, tc, None)
            if len(vals) >= MIN_SAMPLES:
                c = statistics.median(vals)
                live_segs += 1   # this hop has real observed spread
            else:   # no live spread for this hop — use the seed as a constant
                row = cur.execute("SELECT seconds FROM seed WHERE line=? AND leg=? "
                                  "AND from_code=? AND to_code=? AND hour=-1",
                                  (line, leg, fc, tc)).fetchone()
                if not row:
                    k -= 1
                    continue
                c, vals = row[0], [row[0]]
            seg_samples.append(vals)
            predicted += c
            k -= 1
    if not seg_samples or predicted <= 0:
        return None
    errs = [(predicted - sum(random.choice(s) for s in seg_samples)) / 60.0
            for _ in range(n)]
    s = _summ(errs)
    s["stops"], s["pred_min"] = len(seg_samples), round(predicted / 60)
    s["coverage"] = round(live_segs / len(seg_samples), 2)
    # Distinct calendar days of data behind this line/leg — the real trust signal.
    # A day of samples spans too narrow a slice of conditions to be believed; the
    # estimate only firms up (and usually widens) across many days.
    with _db_lock:
        row = db().execute("SELECT COUNT(DISTINCT substr(ts,1,10)) FROM hops "
                           "WHERE line=? AND leg=?", (line, leg)).fetchone()
    s["days"] = row[0] if row else 0
    return s


def collector_loop(lines: list, interval: int):
    """Background sampler for the map server: keep recording segment times.
    Samples fast during the rush windows, slower off-peak (see collect_interval_now)."""
    empties = 0
    last_hb = 0.0
    while True:
        present = False
        for ln in lines:
            try:
                record_hops(ln)
            except (Exception, SystemExit) as e:
                log.warning("collector record_hops(%s) failed: %s", ln, e)
            try:
                if api_live_buses(ln):   # cached (shared w/ record_hops) — no extra
                    present = True       # call; raises if İBB is down → not present
            except (Exception, SystemExit):
                pass
            try:
                positions_payload([ln])   # keep map-match + Kalman state fresh and
            except (Exception, SystemExit):  # _pos_cache warm even with no viewer,
                pass                       # so outage projection works on a cold open
            for st in ACCURACY_STOPS:   # log predictions continuously (not just
                try:                    # when someone's viewing) so the self-check
                    eta_payload(st, [ln])   # has a steady stream to match arrivals
                except (Exception, SystemExit):
                    pass
        empties = 0 if present else empties + 1
        nap = collect_sleep_secs(present, empties)
        now = time.time()
        if now - last_hb >= 300:        # heartbeat every ~5 min: proves it's alive
            last_hb = now               # and records the last known state if it dies
            try:
                hh = live_health()
                log.info("heartbeat samples=%s present=%s api(calls_1h=%s fail_1h=%s) "
                         "nap=%ss", db_stats().get("samples"), present,
                         hh.get("calls_1h"), hh.get("fail_1h"), nap)
            except Exception:
                pass
        time.sleep(nap)


def backup_db():
    """Consistent snapshot of the learned DB (the most valuable, slow-to-rebuild
    asset) via SQLite's online backup. Keeps the last BACKUP_KEEP, prunes older."""
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        dest = BACKUP_DIR / f"iett_segments_{datetime.now():%Y%m%d_%H%M}.db"
        with _db_lock:
            target = sqlite3.connect(dest)
            db().backup(target)
            target.close()
        snaps = sorted(BACKUP_DIR.glob("iett_segments_*.db"))
        for old in snaps[:-BACKUP_KEEP]:
            old.unlink()
        cutoff = time.time() - 30 * 86400   # keep 30 days of accuracy logs
        with _db_lock:
            db().execute("DELETE FROM pred WHERE made<?", (cutoff,))
            db().execute("DELETE FROM arr WHERE ts<?", (cutoff,))
            db().execute("DELETE FROM dep WHERE obs_dep<?", (cutoff,))
            db().commit()
        return dest
    except Exception:
        return None


def backup_loop():
    """Daily DB backup. Runs one immediately, then every BACKUP_INTERVAL."""
    while True:
        backup_db()
        time.sleep(BACKUP_INTERVAL)


def db_stats() -> dict:
    with _db_lock:
        cur = db()
        total = cur.execute("SELECT COUNT(*) FROM hops").fetchone()[0]
        segs = cur.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM hops GROUP BY line,leg,from_code,"
            "to_code HAVING COUNT(*)>=?)", (MIN_SAMPLES,)).fetchone()[0]
        by_line = cur.execute(
            "SELECT line, COUNT(*) FROM hops GROUP BY line").fetchall()
        seed_sched = cur.execute(
            "SELECT COUNT(*) FROM seed WHERE hour=-1").fetchone()[0]
        seed_traf = cur.execute(
            "SELECT COUNT(*) FROM seed WHERE hour>=0").fetchone()[0]
    return {"samples": total, "learned_segments": segs, "by_line": dict(by_line),
            "seed_schedule": seed_sched, "seed_traffic": seed_traf}


def seed_count(line: str) -> int:
    with _db_lock:
        return db().execute("SELECT COUNT(*) FROM seed WHERE line=?",
                            (line,)).fetchone()[0]


def _lan_ip() -> str:
    """Best-effort primary LAN IP of this machine."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def cmd_map(lines, interval: int = 10, port: int = 8731, lan: bool = False,
            open_browser: bool = True):
    global _quiet_fail
    _quiet_fail = True   # report API hiccups via the page, not console panels
    if isinstance(lines, str):
        lines = [lines]
    lines = [l.upper() for l in lines]
    primary = lines[0]
    title = " + ".join(lines)
    eta_cfg = MAP_ETA if (MAP_ETA and MAP_ETA.get("stop")) else None
    page = (MAP_PAGE.replace("__LINES_JSON__", json.dumps(lines))
                    .replace("__COLORS_JSON__", json.dumps(MAP_LINE_COLORS))
                    .replace("__ETA_JSON__", json.dumps(eta_cfg))
                    .replace("__FOCUS_JSON__", json.dumps(MAP_FOCUS))
                    .replace("__PRIMARY__", primary)
                    .replace("__TITLE__", title)
                    .replace("__INTERVAL__", str(interval)))

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"   # keep-alive; we always send Content-Length
        server_version = "iett"         # don't leak the Python/BaseHTTP version
        sys_version = ""

        def log_message(self, *a):  # quiet
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")   # always serve fresh
            self.end_headers()
            self.wfile.write(body)

        def _reply(self, code: int, msg: str):
            body = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            """Gate for the data routes. Accepts the session cookie (set by the
            custom /login form) OR HTTP Basic (for curl/tooling). On failure returns
            a bare 401 — NO WWW-Authenticate — so the browser never pops its native
            dialog; the page shows our own login overlay instead."""
            import base64, hmac
            for part in self.headers.get("Cookie", "").split(";"):
                part = part.strip()
                if part.startswith("iett_auth=") and hmac.compare_digest(part[10:], AUTH_COOKIE):
                    return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                    if hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(p, AUTH_PASS):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_POST(self):
            import hmac
            parsed = urlparse(self.path)
            if parsed.path == "/login":   # custom login: password -> session cookie
                n = int(self.headers.get("Content-Length", 0) or 0)
                pw = parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("password", [""])[0]
                if hmac.compare_digest(pw, AUTH_PASS):
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"iett_auth={AUTH_COOKIE}; Path=/; "
                                     "Max-Age=31536000; HttpOnly; Secure; SameSite=Lax")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._reply(401, "bad password")
                return
            if not self._check_auth():
                return
            # Remote self-update is DISABLED (UPDATE_ENABLED=False) so there's no
            # code-execution surface on a public URL. Deploy via SSH instead.
            import ast, os, shutil
            q = parse_qs(parsed.query)
            if not UPDATE_ENABLED or parsed.path != "/update":
                return self._reply(404, "not found")
            if q.get("token", [""])[0] != UPDATE_TOKEN:
                return self._reply(403, "forbidden")
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            target = os.path.abspath(sys.argv[0])
            tmp = target + ".new"
            try:
                with open(tmp, "wb") as f:
                    f.write(body)
                ast.parse(open(tmp, encoding="utf-8").read())   # reject broken pushes
            except Exception as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return self._reply(400, "rejected (invalid python): " + str(exc))
            shutil.copy(target, target + ".bak")   # keep a rollback copy
            os.replace(tmp, target)
            self._reply(200, "updated (" + str(len(body)) + " bytes) — restarting")
            threading.Thread(target=lambda: (time.sleep(1), os._exit(0)),
                             daemon=True).start()

        def do_GET(self):
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            # Auth-gate the DATA only. The static shell (page, manifest, icons,
            # service worker) stays public so Chrome can evaluate PWA installability
            # — Basic Auth on the shell breaks the install prompt. Nothing sensitive
            # is in the shell; positions/stats/etc. still require the password.
            if (parsed.path in ("/positions", "/eta", "/stats", "/accuracy",
                                "/route", "/schedule", "/logs") and not self._check_auth()):
                return
            if parsed.path == "/logs":   # tail the diagnostic log over the funnel
                try:                     #  (no SSH needed) — ?n=N lines, default 200
                    n = max(1, min(2000, int(q.get("n", ["200"])[0])))
                except ValueError:
                    n = 200
                try:
                    with open(LOG_DIR / "iett.log", encoding="utf-8",
                              errors="replace") as f:
                        tail = "".join(f.readlines()[-n:])
                except OSError as exc:
                    tail = f"(no log: {exc})"
                self._send(tail.encode("utf-8"), "text/plain; charset=utf-8")
                return
            if parsed.path == "/positions":
                lns = [x.upper() for x in (q.get("lines", [",".join(lines)])[0]).split(",")
                       if x.strip()] or lines
                want_stops = q.get("stops", ["0"])[0] == "1"
                filter_dir = q.get("dir", ["1"])[0] != "0"
                payload = positions_payload(lns, want_stops, primary, filter_dir)
                self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/eta":
                if eta_cfg:
                    stop = q.get("stop", [eta_cfg["stop"]])[0]
                    ln = [q.get("line", [eta_cfg["lines"][0]])[0]]
                    payload = eta_payload(stop, ln)
                else:
                    payload = {"rows": [], "error": "no ETA stop configured"}
                self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/stats":
                import os as _os
                try:
                    payload = db_stats()
                except Exception as exc:
                    payload = {"error": str(exc)}
                payload["pid"] = _os.getpid()   # (username dropped — don't leak it publicly)
                payload["api"] = live_health()   # upstream İBB success rate, last 1h
                payload["proj"] = projection_stats()   # between-update projection error
                payload["dep"] = origin_dep_report()   # origin-departure learning
                self._send(json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/accuracy":
                stop = q.get("stop", [None])[0]
                try:
                    payload = accuracy_stats(stop)
                    if stop:   # immediate estimate from historical hops
                        payload["backtest"] = backtest_accuracy(primary, stop)
                except Exception as exc:
                    payload = {"error": str(exc)}
                self._send(json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/route":
                try:
                    payload = {leg: info["geom"]
                               for leg, info in road_data(primary).items()}
                except (Exception, SystemExit):
                    payload = {}
                self._send(json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/schedule":
                if eta_cfg:
                    stop = q.get("stop", [eta_cfg["stop"]])[0]
                    ln = q.get("line", [eta_cfg["lines"][0]])[0]
                    payload = scheduled_arrivals(ln, stop, 6)
                else:
                    payload = {"rows": [], "error": "no stop"}
                self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif parsed.path == "/manifest.json":
                self._send(PWA_MANIFEST.encode("utf-8"), "application/manifest+json")
            elif parsed.path == "/icon.svg":
                self._send(PWA_ICON.encode("utf-8"), "image/svg+xml")
            elif parsed.path == "/icon-192.png":
                import base64 as _b64
                self._send(_b64.b64decode(ICON_192_B64), "image/png")
            elif parsed.path == "/icon-512.png":
                import base64 as _b64
                self._send(_b64.b64decode(ICON_512_B64), "image/png")
            elif parsed.path == "/sw.js":
                self._send(PWA_SW.encode("utf-8"), "application/javascript")
            else:
                self._send(page.encode("utf-8"), "text/html; charset=utf-8")

    class QuietServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            # A client dropping a keep-alive connection (phone backgrounding the
            # tab, network blip, WinError 10054) is normal — don't spam a traceback.
            et = sys.exc_info()[0]
            if et and issubclass(et, (ConnectionResetError, ConnectionAbortedError,
                                      BrokenPipeError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    bind_host = "0.0.0.0" if lan else "127.0.0.1"
    try:
        httpd = QuietServer((bind_host, port), Handler)
    except OSError as exc:
        console.print(f"[red]Port {port} is in use ({exc}). Try another port with "
                      f"--port.[/red]")
        sys.exit(1)

    open_url = f"http://127.0.0.1:{port}/"
    if lan:
        phone_url = f"http://{_lan_ip()}:{port}/"
        body = (f"On this computer: [bold]{open_url}[/bold]\n"
                f"On your phone (same wifi): [bold]{phone_url}[/bold]\n"
                f"line(s) [bold]{title}[/bold], refreshes every {interval} s.\n"
                f"[dim]Press Ctrl-C to stop.[/dim]")
        console.print(Panel(body, border_style="green", title="live map"))
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(phone_url)
            console.print("[dim]Scan with your phone:[/dim]")
            qr.print_ascii(invert=True)
        except Exception:
            console.print("[dim](For a QR code: pip install qrcode — or just type the "
                          "address above into your phone.)[/dim]")
    else:
        console.print(Panel(f"Live map: [bold]{open_url}[/bold]\n"
                            f"line(s) [bold]{title}[/bold], refreshes every {interval} s.\n"
                            f"[dim]To open it on your phone, add --lan.[/dim]\n"
                            f"[dim]Press Ctrl-C to stop.[/dim]", border_style="green"))
    if eta_cfg:
        def _eta_background():
            for ln in eta_cfg["lines"]:        # one-off schedule baseline
                if seed_count(ln) == 0:
                    try:
                        n = seed_schedule(ln)
                        if n:
                            console.print(f"[dim]seeded {n} schedule segments for "
                                          f"{ln} (baseline ETAs ready).[/dim]")
                    except (Exception, SystemExit):
                        pass
            collector_loop(lines, COLLECT_INTERVAL)   # then keep learning live
        threading.Thread(target=_eta_background, daemon=True).start()
        threading.Thread(target=backup_loop, daemon=True).start()   # daily DB backup
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(open_url), daemon=True).start()
    log.info("map server up: lines=%s port=%s lan=%s samples=%s url=%s",
             ",".join(lines), port, lan, db_stats().get("samples"), open_url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]map stopped.[/dim]")
        log.info("map server stopped (KeyboardInterrupt)")
    except Exception:
        log.exception("map server crashed in serve_forever")   # full traceback
        raise
    finally:
        httpd.shutdown()


def cmd_collect(lines, interval: int = COLLECT_INTERVAL):
    """Run the segment-time collector in the foreground (learns ETAs over time).
    Samples fast in rush windows, slower off-peak (the `interval` arg is ignored
    in favour of the adaptive schedule)."""
    lines = [l.upper() for l in lines]
    rush = ", ".join(f"{a:02}:00–{b:02}:00" for a, b in RUSH_WINDOWS)
    console.print(Panel(f"Learning stop-to-stop times for [bold]{', '.join(lines)}[/bold]\n"
                        f"[dim]rush windows ({rush}) sampled every {RUSH_INTERVAL}s, "
                        f"off-peak every {OFFPEAK_INTERVAL}s\ndatabase: {SEG_DB}\n"
                        f"Leave this running. Ctrl-C to stop.[/dim]",
                        title="ETA collector", border_style="cyan"))
    try:
        while True:
            n = 0
            for ln in lines:
                try:
                    n += record_hops(ln)
                except (Exception, SystemExit):
                    pass
            s = db_stats()
            iv = collect_interval_now()
            tag = "RUSH" if iv == RUSH_INTERVAL else "off-peak"
            console.print(f"[dim]{datetime.now():%H:%M:%S}[/dim]  +{n} samples  ·  "
                          f"total {s['samples']}  ·  learned {s['learned_segments']}  "
                          f"[dim]({tag}, {iv}s)[/dim]")
            time.sleep(iv)
    except KeyboardInterrupt:
        console.print("\n[dim]collector stopped.[/dim]")


def cmd_seed(lines):
    """Write the official-schedule baseline (SEFER_SURESI ÷ distance) for lines."""
    for ln in [l.upper() for l in lines]:
        try:
            n = seed_schedule(ln)
            console.print(f"line {ln}: seeded [bold]{n}[/bold] schedule segments"
                          if n else f"line {ln}: [yellow]no schedule duration "
                                    f"available — skipped[/yellow]")
        except (Exception, SystemExit) as exc:
            console.print(f"line {ln}: [red]failed[/red] ({exc})")


def cmd_seed_traffic(lines, csv_src=TRAFFIC_CSV_URL):
    """Build the time-of-day traffic prior for lines (streams ~135 MB). Resolves
    the newest month automatically unless an explicit CSV source is given."""
    if csv_src == TRAFFIC_CSV_URL:        # default → use the freshest month
        latest = latest_traffic_url()
        if latest:
            csv_src = latest
            console.print(f"[dim]using latest traffic month: "
                          f"{csv_src.rsplit('/', 1)[-1]}[/dim]")
        else:
            console.print("[yellow]couldn't resolve latest month; using the pinned "
                          "fallback CSV.[/yellow]")
    for ln in [l.upper() for l in lines]:
        console.print(f"[dim]line {ln}: streaming traffic data and matching to the "
                      f"route corridor…[/dim]")
        try:
            r = seed_traffic(ln, csv_src)
            console.print(f"line {ln}: month {r['month']} · matched "
                          f"[bold]{r['cells']}[/bold] geohash cells, wrote "
                          f"[bold]{r['hourly_rows']}[/bold] hourly baselines across "
                          f"{r['segments']} segments")
        except (Exception, SystemExit) as exc:
            console.print(f"line {ln}: [red]failed[/red] ({exc})")


def cmd_eta_stats():
    s = db_stats()
    console.print(Panel(f"database: [bold]{SEG_DB}[/bold]\n"
                        f"schedule baselines (hour -1): {s['seed_schedule']}\n"
                        f"traffic baselines (hourly):   {s['seed_traffic']}\n"
                        f"live samples: {s['samples']}\n"
                        f"live-learned segments (≥{MIN_SAMPLES} samples): "
                        f"{s['learned_segments']}\n"
                        f"live samples by line: {s['by_line'] or '—'}",
                        title="ETA database", border_style="cyan"))


# ──────────────────────────────────────────────────────────────────────────────
# Interactive menu
# ──────────────────────────────────────────────────────────────────────────────
def menu():
    console.print(Panel("[bold]İETT tracker[/bold] — official İBB SOAP API\n"
                        f"[dim]defaults: {', '.join(r['label'] for r in DEFAULT_ROUTES)}[/dim]",
                        border_style="cyan"))
    while True:
        console.print(
            "\n[bold cyan]1[/] live: my default routes (522 → Mecidiyeköy, M7 → Nurtepe)"
            "\n[bold cyan]2[/] watch my default routes (auto-refresh)"
            "\n[bold cyan]3[/] search stop by name"
            "\n[bold cyan]4[/] list stops on a line"
            "\n[bold cyan]5[/] live buses on a line"
            "\n[bold cyan]6[/] buses approaching a stop"
            "\n[bold cyan]7[/] watch a stop (auto-refresh)"
            "\n[bold cyan]8[/] live MAP of a line (opens browser)"
            "\n[bold cyan]0[/] quit")
        choice = Prompt.ask("choice", choices=list("012345678"), default="1")
        if choice == "0":
            return
        if choice == "1":
            cmd_defaults()
        elif choice == "2":
            cmd_watch_defaults(WATCH_DEFAULT_INTERVAL)
        elif choice == "3":
            cmd_search(Prompt.ask("stop name (e.g. çakmak)"))
        elif choice == "4":
            cmd_stops(Prompt.ask("line code", default="522").strip().upper())
        elif choice == "5":
            line = Prompt.ask("line code", default="522").strip().upper()
            direction = Prompt.ask("direction filter (text/G/D, empty = all)",
                                   default="").strip()
            cmd_live(line, direction or None)
        elif choice in ("6", "7"):
            code = resolve_stop(Prompt.ask("stop code or alias",
                                           default="cakmak").strip())
            lines = Prompt.ask("lines to check (comma-sep)",
                               default=",".join(r["line"] for r in DEFAULT_ROUTES))
            lines = [s.strip().upper() for s in lines.split(",") if s.strip()]
            if choice == "6":
                cmd_stop(code, lines)
            else:
                cmd_watch_stop(code, lines, WATCH_DEFAULT_INTERVAL)
        elif choice == "8":
            lines = Prompt.ask("line(s), comma-separated", default="522,M7")
            lines = [s.strip().upper() for s in lines.split(",") if s.strip()]
            cmd_map(lines)


# ──────────────────────────────────────────────────────────────────────────────
def migrate_data():
    """One-off: if the learned DB still lives in the old home-dir location, copy
    it next to the app — so a background service (any account) keeps the history."""
    import shutil
    old = Path.home() / ".iett_segments.db"
    if old.exists() and not SEG_DB.exists():
        try:
            shutil.copy(old, SEG_DB)
            console.print(f"[dim]migrated learned DB → {SEG_DB}[/dim]")
        except OSError:
            pass


def main():
    setup_logging("cli")
    migrate_data()
    p = argparse.ArgumentParser(
        description="İETT bus tracker (İBB official SOAP API). "
                    "Run without arguments for the interactive menu.")
    p.add_argument("--search", metavar="NAME", help="search all stops by name")
    p.add_argument("--stops", metavar="LINE", help="list stops on a line (e.g. 522)")
    p.add_argument("--live", metavar="LINE", nargs="?", const="__DEFAULTS__",
                   help="live bus positions on a line (no value = default routes)")
    p.add_argument("--watch", metavar="LINE", nargs="?", const="__DEFAULTS__",
                   help="like --live but refreshes continuously")
    p.add_argument("--defaults", action="store_true",
                   help="live view of default routes (522→Mecidiyeköy, M7→Nurtepe)")
    p.add_argument("--watch-defaults", action="store_true",
                   help="watch default routes, refreshing")
    p.add_argument("--map", metavar="LINES", help="open a live map of buses in your "
                   "browser; comma-separate for several lines (e.g. 522 or 522,M7)")
    p.add_argument("--port", type=int, default=8731,
                   help="local port for --map server (default %(default)s)")
    p.add_argument("--lan", action="store_true",
                   help="serve --map on your local network so a phone on the same "
                        "wifi can open it (prints a QR code)")
    p.add_argument("--no-open", action="store_true",
                   help="don't auto-open a browser (for headless/service use)")
    p.add_argument("--collect", metavar="LINES",
                   help="learn stop-to-stop travel times for these lines (comma-sep) "
                        "to sharpen ETAs over time, e.g. 522")
    p.add_argument("--eta-stats", action="store_true",
                   help="show the learned-ETA database stats")
    p.add_argument("--seed-schedule", metavar="LINES",
                   help="write the official-schedule ETA baseline for these lines "
                        "(comma-sep), e.g. 522")
    p.add_argument("--seed-traffic", metavar="LINES",
                   help="one-off: build a time-of-day traffic prior for these lines "
                        "from İBB hourly traffic data (streams ~135 MB), e.g. 522")
    p.add_argument("--traffic-csv", metavar="URL_OR_PATH", default=TRAFFIC_CSV_URL,
                   help="traffic CSV source for --seed-traffic (default: latest month)")
    p.add_argument("--stop", metavar="CODE", help="buses approaching a stop "
                   "(code or MY_STOPS alias)")
    p.add_argument("--watch-stop", metavar="CODE", help="continuously watch a stop")
    p.add_argument("--lines", metavar="L1,L2",
                   default=",".join(r["line"] for r in DEFAULT_ROUTES),
                   help="lines considered for --stop/--watch-stop (default: %(default)s)")
    p.add_argument("-d", "--direction", metavar="TEXT",
                   help="direction filter for --live/--watch (destination text, or G/D)")
    p.add_argument("--interval", type=int, default=WATCH_DEFAULT_INTERVAL,
                   help="refresh seconds for watch modes (default %(default)s)")
    p.add_argument("--refresh-cache", action="store_true",
                   help="re-download the full stop list cache")
    p.add_argument("--dump-methods", action="store_true",
                   help="list SOAP operations exposed by each İBB WSDL")
    args = p.parse_args()

    lines = [s.strip().upper() for s in args.lines.split(",") if s.strip()]

    if args.dump_methods:
        cmd_dump_methods()
    elif args.search:
        cmd_search(args.search, args.refresh_cache)
    elif args.stops:
        cmd_stops(args.stops.upper())
    elif args.eta_stats:
        cmd_eta_stats()
    elif args.seed_schedule:
        cmd_seed([x.strip() for x in args.seed_schedule.split(",") if x.strip()])
    elif args.seed_traffic:
        cmd_seed_traffic([x.strip() for x in args.seed_traffic.split(",") if x.strip()],
                         args.traffic_csv)
    elif args.collect:
        cl = [x.strip().upper() for x in args.collect.split(",") if x.strip()]
        c_int = COLLECT_INTERVAL if args.interval == WATCH_DEFAULT_INTERVAL else args.interval
        cmd_collect(cl, c_int)
    elif args.map:
        map_lines = [x.strip().upper() for x in args.map.split(",") if x.strip()]
        # map refreshes every 10 s by default; honour an explicit --interval
        map_interval = 10 if args.interval == WATCH_DEFAULT_INTERVAL else args.interval
        cmd_map(map_lines, max(5, min(map_interval, 60)), args.port, args.lan,
                open_browser=not args.no_open)
    elif args.live:
        if args.live == "__DEFAULTS__":
            cmd_defaults()
        else:
            cmd_live(args.live.upper(), args.direction)
    elif args.watch:
        if args.watch == "__DEFAULTS__":
            cmd_watch_defaults(args.interval)
        else:
            cmd_watch(args.watch.upper(), args.direction, args.interval)
    elif args.defaults:
        cmd_defaults()
    elif args.watch_defaults:
        cmd_watch_defaults(args.interval)
    elif args.stop:
        cmd_stop(resolve_stop(args.stop), lines)
    elif args.watch_stop:
        cmd_watch_stop(resolve_stop(args.watch_stop), lines, args.interval)
    else:
        menu()


if __name__ == "__main__":
    main()
