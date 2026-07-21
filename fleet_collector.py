"""Dumb all-fleet collector (phase 1 of the whole-network plan).

Fetches İBB's GetFiloAracKonum_json (~6,900 buses, one call for the whole city),
gzips the raw snapshot to fleet_raw/YYYY-MM-DD/HHMMSS.json.gz, and does ZERO
processing. Built to be a quiet tenant next to the 522 tracker:

  * BUDGET-AWARE — reads the running 522 service's /stats to see how many İBB
    calls it has made this hour, and only fetches if combined stays under the cap.
    It always yields to the 522 service, so it can never throttle the live feed.
  * memory-light — fetch, write, drop. No in-memory accumulation (unlike the
    corridor experiment that OOM'd the box).
  * disk-guarded — pauses if free space gets low.
  * self-backing-off — exponential backoff on any error.

Runs as its own process/task; importing iett_tracker only reuses its SOAP code
(no server starts on import).
"""
import sys, os, time, gzip, json, datetime, base64, shutil, urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import iett_tracker as t   # reuse soap_json / EP_FLEET / AUTH_PASS; no side effects

RAW_DIR   = os.path.join(APP_DIR, "fleet_raw")
LOGFILE   = os.path.join(APP_DIR, "logs", "fleet.log")
STATS_URL = "http://127.0.0.1:8731/stats"
AUTH_HDR  = "Basic " + base64.b64encode(("bus:" + t.AUTH_PASS).encode()).decode()

BUDGET        = 85     # combined İBB calls/hr ceiling (margin under the 100 cap)
BASE_INTERVAL = 60     # seconds between snapshots when there's budget
MIN_FREE_GB   = 5      # stop writing if disk falls below this
MAX_BACKOFF   = 600    # cap on error backoff

_my_calls = []         # epoch timestamps of my own İBB calls in the last hour


def log(msg):
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def service_calls_1h():
    """İBB calls the running 522 service made in the last hour (from its /stats).
    None if unreachable → caller treats it as 'assume busy' (conservative)."""
    try:
        req = urllib.request.Request(STATS_URL, headers={"Authorization": AUTH_HDR})
        d = json.load(urllib.request.urlopen(req, timeout=8))
        return int(d.get("api", {}).get("calls_1h", 0))
    except Exception:
        return None


def my_calls_1h():
    cut = time.time() - 3600
    while _my_calls and _my_calls[0] < cut:
        _my_calls.pop(0)
    return len(_my_calls)


def free_gb():
    try:
        return shutil.disk_usage(APP_DIR).free / 1e9
    except Exception:
        return 999.0


def snapshot():
    """One all-fleet call → gzip to disk. Returns bus count."""
    buses = t.soap_json(t.EP_FLEET, "GetFiloAracKonum_json")   # the İBB call
    _my_calls.append(time.time())
    if not buses:
        return 0
    now = datetime.datetime.now()
    day_dir = os.path.join(RAW_DIR, now.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    tmp = os.path.join(day_dir, now.strftime("%H%M%S") + ".json.gz.tmp")
    final = tmp[:-4]
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(buses, f, ensure_ascii=False)
    os.replace(tmp, final)   # atomic: a reader never sees a half-written file
    return len(buses)


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    log("=== fleet collector up (budget=%d/hr base=%ds pid=%d) ===" % (
        BUDGET, BASE_INTERVAL, os.getpid()))
    backoff = BASE_INTERVAL
    last_hb = 0.0
    while True:
        try:
            if free_gb() < MIN_FREE_GB:
                log("LOW DISK (%.1f GB free) — pausing 1h" % free_gb())
                time.sleep(3600)
                continue
            svc = service_calls_1h()
            mine = my_calls_1h()
            combined = (svc if svc is not None else BUDGET) + mine
            if combined >= BUDGET:           # yield to the live 522 feed
                if time.time() - last_hb >= 600:
                    last_hb = time.time()
                    log("yielding (svc=%s mine=%d combined=%d >= %d)" %
                        (svc, mine, combined, BUDGET))
                time.sleep(60)
                continue
            n = snapshot()
            backoff = BASE_INTERVAL          # reset on success
            if time.time() - last_hb >= 600: # ~10-min heartbeat
                last_hb = time.time()
                log("snapshot ok: %d buses | svc_calls_1h=%s mine=%d | free=%.0fGB" %
                    (n, svc, my_calls_1h(), free_gb()))
            time.sleep(BASE_INTERVAL)
        except (Exception, SystemExit) as e:
            log("fetch failed: %s — backoff %ds" % (e, backoff))
            time.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)


if __name__ == "__main__":
    main()
