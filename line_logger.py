"""Temporary per-line logger — samples one İETT line's live buses and appends a
compact JSONL record per sample, so we can study frequency / headways offline
(e.g. overnight Metrobüs performance). Same defensive shape as fleet_collector:
budget-aware (yields to the live 522 feed), memory-light, self-backing-off.

Usage: python line_logger.py 34G
"""
import sys, os, time, json, datetime, base64, urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import iett_tracker as t   # reuse soap_json / EP_FLEET / AUTH_PASS

LINE = (sys.argv[1] if len(sys.argv) > 1 else "34G").upper()
OUT   = os.path.join(APP_DIR, "logs", "line_%s.jsonl" % LINE)
LOG   = os.path.join(APP_DIR, "logs", "line_%s.log" % LINE)
STATS_URL = "http://127.0.0.1:8731/stats"
AUTH_HDR  = "Basic " + base64.b64encode(("bus:" + t.AUTH_PASS).encode()).decode()

BUDGET   = 88     # combined İBB calls/hr ceiling (margin under 100)
INTERVAL = 75     # seconds between samples when budget allows
MAX_BACKOFF = 600
_my = []


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def service_calls_1h():
    try:
        req = urllib.request.Request(STATS_URL, headers={"Authorization": AUTH_HDR})
        return int(json.load(urllib.request.urlopen(req, timeout=8))
                   .get("api", {}).get("calls_1h", 0))
    except Exception:
        return None


def my_calls_1h():
    cut = time.time() - 3600
    while _my and _my[0] < cut:
        _my.pop(0)
    return len(_my)


def sample():
    buses = t.soap_json(t.EP_FLEET, "GetHatOtoKonum_json", HatKodu=LINE)
    _my.append(time.time())
    rec = {"t": datetime.datetime.now().isoformat(timespec="seconds"),
           "n": len(buses),
           "buses": [{"k": b.get("kapino"), "near": b.get("yakinDurakKodu"),
                      "yon": b.get("yon"), "lat": b.get("enlem"),
                      "lon": b.get("boylam"), "saat": b.get("son_konum_zamani")}
                     for b in buses]}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(buses)


def main():
    os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
    log("=== line logger up: %s (budget=%d/hr base=%ds pid=%d) ===" % (
        LINE, BUDGET, INTERVAL, os.getpid()))
    backoff, last_hb = INTERVAL, 0.0
    while True:
        try:
            svc = service_calls_1h()
            combined = (svc if svc is not None else BUDGET) + my_calls_1h()
            if combined >= BUDGET:
                if time.time() - last_hb >= 600:
                    last_hb = time.time()
                    log("yielding (svc=%s mine=%d)" % (svc, my_calls_1h()))
                time.sleep(45)
                continue
            n = sample()
            backoff = INTERVAL
            if time.time() - last_hb >= 600:
                last_hb = time.time()
                log("sample ok: %d %s buses | svc_calls_1h=%s mine=%d" %
                    (n, LINE, svc, my_calls_1h()))
            time.sleep(INTERVAL)
        except (Exception, SystemExit) as e:
            log("fetch failed: %s — backoff %ds" % (e, backoff))
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)


if __name__ == "__main__":
    main()
