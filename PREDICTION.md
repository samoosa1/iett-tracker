# The prediction and correction pipeline

Technical reference for how an ETA is produced, corrected, logged and validated.
Companion documents: [GUIDE.md](GUIDE.md) is the same material in plain language,
[README.md](README.md) covers the data source, the learning model, the Kalman filter and
the original build.

> **README §4, §5 and §7 are stale (frozen 16 Jun 2026).** The auto-start mechanism and
> the config table both changed after that date. Current facts are in
> [Deployment as it actually is](#deployment-as-it-actually-is) at the bottom of this file.

---

## 1. Order of operations

Everything below happens inside `eta_payload(stop, lines)` for each request:

```
approaching(stop, lines)          # live buses with near_sira, target_sira, leg, dist_km
  └─ per bus: hist_eta_band(...)  # learned segment sum -> (secs, lo, hi, src)
       ├─ band found  -> eta_src = "history" (or "schedule" on seed data)
       └─ band None   -> eta_src = "estimate"  (dist_km / ETA_SPEED_KMH)
_apply_origin_wait(rows, lines)   # reclassify buses parked at the terminus
rows.sort(by eta_min)             # an origin bus is no longer "next" once corrected
_apply_bias_correction(rows)      # the correction stack (section 4)
log_predictions(stop, rows)       # writes the CORRECTED eta to the `pred` table
_flag_bunching(rows)              # mark buses <=2 min apart
```

Two ordering facts matter and are easy to get wrong:

1. **Origin-wait runs before bias correction.** So the correction sees the already
   rescheduled ETA, and `origin-wait` needs its own constant rather than inheriting the
   `history` one.
2. **Correction runs before logging.** The `pred` table therefore stores the number the
   user actually saw. This is what makes the system self-measuring: re-running the
   validation later scores the corrected output, so a correction that works drives its own
   residual toward zero.

## 2. The `eta_src` values

`eta_src` is the provenance tag on every prediction. It drives the UI badge, the accuracy
bucketing, and which bias constant applies.

| `eta_src` | Produced when | ETA formula | Corrected by |
| --- | --- | --- | --- |
| `history` | Learned segment data exists for the whole span (`home_sira > near_sira`) | Sum of per-segment medians, hour and weekday aware | floor + horizon ramp |
| `schedule` | Only seeded timetable data backs the span | Same sum, seed values | floor + horizon ramp |
| `origin-wait` | Bus sits at the route origin (`near_sira == 1`, `target_sira > 1`) | `(next scheduled departure - now) + origin→stop travel` | `ORIGIN_WAIT_BIAS` |
| `estimate` | No band available at all | `dist_km / ETA_SPEED_KMH` | `ESTIMATE_BIAS` |
| `projected` | İBB feed is down | Dead-reckon `s = s + v·age` along the route | not corrected |

## 3. Origin-wait

A bus at the terminus has a short *distance* to a near-origin stop but a long *wait*,
because it dwells to fill up before departing. Without this the ETA is badly wrong at any
stop close to an origin. On leg D the home stop (`302361`) is **sira 2 of 69**, one stop
past the terminus `301371`, so it is maximally exposed. On leg G the going stop (`260112`)
is sira 51 of 70 and is unaffected.

`_apply_origin_wait(rows, lines)`:

1. Select buses with `near_sira == 1` and `target_sira` not in `(None, 1)`.
2. Group them by `(line, leg)`.
3. Sort each group FIFO by `_origin_since[kapino]`, the epoch a bus was first confirmed at
   the origin, so the longest-waiting bus departs first.
4. Pull `_next_departures(line, leg, n)` and hand each bus a **distinct** slot. Without
   this every waiting bus claims the same departure and they collapse onto one minute.
5. `eta = (slot + learned_lateness - now) + travel`, band `[max(travel, eta-3), eta+3]`,
   `eta_conf = "low"`, `at_origin = True`.

Three terminal statuses: a slot was assigned (`ok`), the timetable is unavailable
(`unknown`, so just widen `eta_hi` by 6 and flag), or the day's runs are exhausted
(`no_more`, `eta_min = None`, shown honestly rather than invented).

**Learned departure lateness.** `record_hops` detects a real departure as the `sira 1 -> >1`
transition and writes it to `dep`, timestamping it as the **midpoint** between the last
at-origin fix and the first gone fix (capped at one ~60s batch) so the polling lag is not
baked into the very lateness being measured. `_match_scheduled_dep` pairs it with the
latest slot at or before `obs + 90s`, discarding anything more than 15 min late or 3 min
early as an unmatchable extra. `_origin_offset(line)` then applies the per-time-bucket
median **only** when a bucket has `n >= 10` and `IQR <= 4.0`, so a thin or noisy bucket
never produces a confident wrong number. `_cached_offset_stats` memoises the 21-day scan
for 5 minutes because `/stats` is polled roughly once a minute.

## 4. The bias-correction stack

All corrections live in `_apply_bias_correction(rows)` and are gated by `BIAS_CORRECT`.
Each one was measured against real arrivals before being written, and each targets a
different, separately-measured failure.

| Constant | Applies to | Value | Shape and why |
| --- | --- | --- | --- |
| `BIAS_OFFSETS` | `history`, `schedule` | `{weekday-daytime: 0.9, weekday-rush: 1.4, weekend: 0.0}` | Median-based sums run optimistic because travel times are right-skewed. Flat per time bucket. |
| `HORIZON_BIAS_ANCHORS` | `history`, `schedule` | `[(6,0.0),(8,1.9),(10,2.6),(13,3.3),(16,3.7)]` | Optimism **grows with lead time** as segment errors compound. Piecewise linear via `_horizon_bias()`, held flat past 16 min. |
| `ESTIMATE_BIAS` | `estimate` | `-1.7` | Distance/speed overshoots close in. Constant. |
| `ORIGIN_WAIT_BIAS` | `origin-wait` | `-0.9` | Measured **flat** at -0.85 to -1.0 across every lead from 2 to 25 min, so a constant, not a ramp. |

Total adjustment for a learned row is `BIAS_OFFSETS[bucket] + _horizon_bias(eta_min)`,
computed from the pre-correction `eta_min`. The band (`eta_lo`, `eta_hi`) shifts with the
point estimate. `eta_min = None` rows and `projected` rows are skipped.

**Why the horizon term is capped at 16 min.** Beyond roughly 25 minutes the residual is
dominated by dispatch and headway decisions that have not been made yet, not by travel
time. Chasing that with a point correction would add 9 or more minutes to a number whose
real uncertainty is irreducible. The cap plus the confidence band is the honest treatment.

**Why the corrections deliberately under-correct `history`.** The two error directions are
not symmetric for the user:

- Predicted **later** than reality: the user dawdles and **misses the bus**. Bad.
- Predicted **earlier** than reality: the user waits a little. Merely annoying.

So the anchors sit below the measured residual on purpose. Post-fix the 6 to 10 min band is
still about +1.3 optimistic, which is the intended landing spot, not an under-shoot to fix.

## 5. Sign conventions (read this before interpreting any number)

The two measurement surfaces use **opposite signs**. This has caused real confusion.

| Surface | Definition | Positive means |
| --- | --- | --- |
| `/accuracy` endpoint | `predicted_arrival - actual_arrival` | Predicted **later** than actual. Bus arrives early against the shown time. **Miss risk.** |
| Offline validation scripts | `actual_arrival - predicted_arrival` | Bus arrives **later** than predicted. Prediction was optimistic. User waits. |

Worked example: a `history` bin reading `+1.45` in the offline script means the bus turns
up 1.45 min after the app said, and the same population would read `-1.45` in `/accuracy`.
When `/accuracy` bias rose from +0.8 to +1.5 after the July 14 deploy, that was **not** the
horizon ramp overshooting; it was `origin-wait` running pessimistic and dominating the
`<=10 min` lead population that `/accuracy` aggregates.

## 6. Measured calibration

`history`, median signed error in minutes (offline convention: positive = bus later than
predicted), before and after the 14 Jul horizon ramp. After-figures from 15,048 matched
pairs collected 14 to 20 Jul 2026.

| Lead | Before | After |
| --- | --- | --- |
| 6 to 8 min | +2.26 | **+1.45** |
| 8 to 10 min | +3.04 | **+1.21** |
| 10 to 13 min | +3.89 | **+0.37** |
| 13 to 16 min | +4.28 | **+0.20** |
| 16 to 20 min | +5.80 | **+0.57** |
| 20 to 25 min | +9.20 | **+1.80** |
| 25+ min | +10.4 to +13 | +6.88 (capped by design) |

Median absolute error across the 6 to 20 min band is about 1.5 to 1.8 min. `/accuracy`
moved from mae 2.4 to 2.2 (daytime) and p90 5.8 to 4.1 over the same period.

`origin-wait` was flat at -0.85 to -1.0 across leads 2 to 25 (n=3,041) and got
`ORIGIN_WAIT_BIAS = -0.9` on 20 Jul. `estimate` reads -1.53 post-fix and was **deliberately
left alone**: arrival timestamps are interpolated between GPS samples so they land up to
~60s in the past, and every `estimate` prediction now sits in the 0 to 1 min bucket.
Showing "due" for a bus that is pulling in is correct, and correcting further would be
fitting to the instrumentation rather than to reality.

## 7. How to re-measure

The recipe, because the traps here are not obvious.

1. Build an index of `arr` rows keyed `(line, kapino, stop)`, each sorted by `ts`.
2. For each `pred` row, binary-search the **nearest** arrival to `pred_arr` for the same
   key. Discard matches more than 20 minutes out.
3. Report **medians**, not means. The distribution has a long right tail that drags a mean.
4. **Decompose before concluding.** Split by `src`, by lead-time bin, and by `stop`. Every
   real finding so far only became visible after decomposing:
   - The "0 to 3 min is worse than 3 to 6 min" anomaly was `estimate` (a different `src`)
     dominating that bucket, not a flaw in the learned model.
   - The "+6.2 at 1 to 2 min lead" anomaly was 100% one stop (`302361`), not a global bias.
5. **Check the date distribution against deploy dates before building a fix.** The +6.2
   anomaly turned out to be entirely pre-19-Jun data, from before origin-wait shipped. A
   long-window aggregate silently mixes pre-fix and post-fix behaviour, and it nearly
   caused a redundant feature to be built. Scope the query with `WHERE made > <deploy_ts>`.

## 8. Tables used for accuracy

SQLite at `iett_segments.db`, WAL mode with `synchronous=NORMAL` (the box shuts down
ungracefully, and WAL lets the web thread read while the collector writes).

| Table | Columns | Purpose |
| --- | --- | --- |
| `hops` | `line, leg, from_code, to_code, seconds, dow, hour, ts` | Learned segment times. The core asset. |
| `seed` | `line, leg, from_code, to_code, hour, seconds, source` | Timetable and traffic cold-start priors. |
| `pred` | `line, kapino, stop, pred_arr, made, eta_min, src` | Every ETA served, post-correction. |
| `arr` | `line, kapino, stop, ts` | Actual arrivals at tracked stops, interpolated between fixes. |
| `dep` | `line, leg, kapino, sched_dep, obs_dep, dow, hour` | Observed origin departures vs timetable. |

## 9. Config reference (additions since README §7)

| Constant | Value | Meaning |
| --- | --- | --- |
| `RUSH_INTERVAL` / `OFFPEAK_INTERVAL` | `40` / `40` | Both at the floor now. The 522 owns the whole İBB budget since the other collectors are off. ~90 calls/hr against a 100/hr cap. |
| `LIVE_TTL` | `40` | One shared upstream call per 40s across all consumers. This, not the poll interval, is the real governor. |
| `COLLECT_BACKOFF_MAX` | `900` | Exponential cooldown when calls fail. Failed calls still count against the quota, so going quiet is the only recovery. |
| `BIAS_CORRECT` | `True` | Master switch for section 4. |
| `BIAS_OFFSETS` | see §4 | Time-of-day floor. |
| `HORIZON_BIAS_ANCHORS` | see §4 | Lead-time ramp. |
| `ESTIMATE_BIAS` | `-1.7` | Estimate pull-back. |
| `ORIGIN_WAIT_BIAS` | `-0.9` | Origin-wait pull-back. |
| `MIN_SAMPLES` | `3` | Observations before a hop is trusted. |
| `BAND_CORR_INFLATE` | `1.4` | Widens the quadrature band for correlated congestion. |

Useful endpoints (all behind the in-page login): `/eta?stop=<code>`, `/positions`,
`/stats`, `/accuracy`, `/route`, `/schedule`, `/logs?n=N`.

## 10. Known and unfixed, by design

- **25+ minute leads are unreliable** (history +6.88, origin-wait +11.11). Headway-bound.
  The band carries it; the point estimate is deliberately not chased.
- **`calls_1h` is a lower bound.** `soap_post` retries up to `SOAP_RETRIES=3` real HTTP
  calls but `_record_live` counts one per fetch, so the true İBB call count during flaky
  periods is higher than `/stats` shows. Not a live risk (throttling is governed by the
  40s cadence and the backoff, not by this counter) but do not trust it for budget maths.
- **`fleet_collector.py` and `line_logger.py` are ~90% duplicated** and both disabled. If
  either is ever re-enabled, factor the budget-aware poller into one shared helper first
  and fix the open findings against them (24:xx daycode, retry undercount, quiet-fail,
  unbounded dicts, missing disk guard).

## Deployment as it actually is

This supersedes README §4 and §5, which describe the older and more fragile arrangement.
Verified on the live box 20 Jul 2026.

| Task | Runs as | Trigger | Starts on battery | Stops on battery | State |
| --- | --- | --- | --- | --- | --- |
| `IETTTrackerSys` | SYSTEM | boot | **yes** | no | **Running** |
| `IETTTracker` | user `Lenovo` | logon | no | yes | Ready (**stale leftover**) |
| `IETTFleet` | SYSTEM | boot | yes | no | Disabled |
| `IETTLine34G` | SYSTEM | boot | yes | no | Disabled |

What changed after the reliability crisis, and why each mattered:

- **SYSTEM task on a boot trigger**, not a user logon task. Survives without anyone
  logging in.
- **`-AllowStartIfOnBatteries`** via `Register-ScheduledTask`. `schtasks` defaults to
  refusing to start on battery, which is the single reason the box kept going dark. The old
  `IETTTracker` row above still shows that failure mode: `startsOnBattery=no`,
  `stopsOnBattery=yes`.
- **`powercfg` lid action and sleep timeouts set to 0**, so closing the lid does nothing.
- **Tailscale `UnattendedMode=always`**, otherwise the tailnet does not come back after a
  reboot until someone logs in.

`IETTTracker` is a leftover from the old scheme and should be removed. It is idle now, but
it is registered on a logon trigger and would launch a second instance.

**Deploy loop.** `service-run.bat` is a supervisor that relaunches the app about 5s after
it exits, so a deploy is: `scp` the new `iett_tracker.py` to the kit folder, then kill only
the python process whose command line matches `iett_tracker` (never the supervisor, and
never a bare `python` match, which would also kill the collectors if they were running).
The supervisor picks up the new code. Verify with `/stats` that the pid changed and
`fail_pct_1h` is 0.
