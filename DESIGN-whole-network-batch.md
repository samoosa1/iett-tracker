# Whole-Network Tracking via Overnight Batch — Design Note (idea, not built)

Goal: learn/track ETAs for the whole İETT network (~800 lines, ~6,900 live buses)
on the 4 GB Lenovo, without melting it, by splitting **cheap real-time collection**
from **heavy overnight batch processing** (speed layer + batch layer).

Why this works: collection is ~free (one HTTP call + a file write), and overnight
the box is idle (buses sparse 00:00–06:00) and not racing the 60s cycle — so even a
thorough grind through the day's data fits in a 6-hour window.

---

## Components

### 1. Ingest — 24/7, cheap (the "dumb collector")
- Every ~60s: `GetFiloAracKonum_json` (all-fleet, **1 call** for the whole city).
- Write raw JSON to `fleet_raw/YYYY-MM-DD/HHMMSS.json.gz` (gzip ~430 KB → ~80 KB).
- ZERO processing. Fetch → compress → write. The box won't notice.
- Volume: ~1,440 snapshots/day × ~80 KB ≈ **~115 MB/day** compressed (pruned after
  processing; trivial on 86 GB free).

### 2. Batch processor — 00:15–06:00, heavy (the "night grind")
Triggered by a SYSTEM scheduled task at 00:15. Reads the day's snapshots in order:
- **A. Trajectory stitching** — group all observations by `KapiNo` → each bus's full
  day as a time-ordered (lat, lon, speed, t) path.
- **B. Line assignment** — fit each trajectory against every route polyline; lowest
  cumulative off-track distance wins. Segment the day into runs (a bus does several
  runs / sometimes different lines). **This is the key quality win:** disambiguating
  by whole-trajectory beats single-frame guessing on shared corridors.
- **C. Stop-crossing extraction** — map-match each run to its route, interpolate the
  moment it crossed each stop → hop records (stopA→stopB, seconds), exactly like the
  current `record_hops`, just derived offline.
- **D. Learning update** — insert hops into the per-line DB (reuse existing
  hops/seed/dep schema, keyed by `line`); recompute medians + origin-departure offsets.
- **E. Housekeeping** — prune processed raw (keep ~2 days), back up DB. Handle a
  backlog (if the box was off, process all unprocessed days).

### 3. Spatial index (built per batch run)
Grid-hash of all route polyline points → cell. Position → O(1) nearby-route lookup.
This is what avoids the naive O(buses × route_points) loop that crashed the box before.
Built from all-line stop coords (cached, refreshed weekly).

### 4. Runtime serving — real-time, cheap (during the day)
- Live positions from the latest all-fleet snapshot, filtered to the viewed line's
  corridor via the spatial index (~few hundred buses → cheap).
- ETAs from the overnight-built model (cheap median lookup — same engine as 522 today).
- Segment-time stats barely move day-to-day, so a nightly-refreshed model is plenty.

---

## The one real tension: the rate-limit budget
İBB cap = **100 req/hr**, and BOTH all-fleet and per-line calls count against it.
- all-fleet @ 60/hr leaves only ~40/hr for per-line calls.
- Options:
  - **(a) all-fleet only** — unlimited lines, but the clean 522 model degrades to
    inference too (all-fleet has no `yakinDurakKodu`).
  - **(b) hybrid** — keep the clean per-line 522 feed @ ~40/hr (every ~90s) + all-fleet
    @ 60/hr = 100/hr exactly. Tight but lets the line you ride stay high-quality while
    the rest of the network is batch-inferred. **Preferred.**

## Other tradeoffs / caveats
- **Label quality:** trajectory-fitting is good but not perfect on heavily-overlapping
  corridors. Medians are robust to some noise.
- **Cold start:** whole-network maturity takes weeks across all lines/buckets; lines you
  ride often mature first.
- **Box must stay on overnight** — already guaranteed (no-sleep + lid-OK + plugged in).
- **Isolation:** build as a separate module + its own DB file (`network.db`,
  `iett_network.py`) so a bug can't destabilize the running 522 service.

---

## Build order (incremental, low-risk — each step independently useful)
1. **Just collect.** Add the all-fleet dumper only (zero risk, ~free). Run a week,
   measure real disk/volume.
2. **Batch on the Mac first.** Write + run the processor on the Mac (beefy, no risk to
   PC) against collected data. **Critical validation:** does batch-derived 522 (inferred
   from all-fleet) match the existing clean per-line 522 model? If yes → the whole
   approach is proven. If batch-522 is way off → label inference is too noisy, rethink.
3. **Move batch to the PC.** Once validated + efficient, schedule it overnight as a
   SYSTEM task (same battery-allowed / at-startup pattern as IETTTrackerSys).
4. **Network serving.** Extend the map UI to pick any line, served from the network model.

The phase-2 validation (batch-522 vs clean-522) is the go/no-go gate for the whole idea.
