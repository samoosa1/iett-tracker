# İETT Tracker

A live map and arrival predictor for a single İstanbul bus line, that you run yourself.

It learns how long each stop-to-stop hop really takes from live GPS, predicts arrivals from
that history, measures how wrong it was, and corrects itself. It shows a range instead of a
fake-precise number, and it keeps working when İETT's feed goes down, which is often.

Built for the **522**, configurable for any line.

> **Scope, honestly.** This is a personal tool that got carefully engineered, not a product.
> One line, one or two stops, one user, one always-on machine. It is İstanbul-only, because
> it talks to İBB's public transit API.

---

## Why this exists

Google Maps and Moovit do not show live positions for most İETT buses, only timetables. The
LED boards at the stops drop out and disagree with reality. İBB publishes the live GPS feed
publicly, the apps just do not surface it well.

So this pulls that feed directly, puts the buses on a map, and quietly records how long every
segment of the route actually takes. After a week or two of watching, its predictions beat
the timetable, because it has learned what your line does at 8am on a Tuesday.

---

## Quickstart

You need **Python 3.9+**. Nothing else installs globally.

```bash
git clone https://github.com/samoosa1/iett-tracker.git
cd iett-tracker
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python3 iett_tracker.py --map 522
```

Your browser opens on a live map of the 522. That is the whole install.

The first run knows nothing, so early ETAs come from the timetable and are marked as such.
Leave it running: it starts learning immediately and the predictions sharpen over a few days.

**A password is generated on first start**, printed to the console, and saved to
`.auth_secret` next to the script, so the page is not open to anyone who can reach the port
and the password survives restarts.

**To choose your own**, any of these work (first one found wins):

```bash
# 1. a .env file next to the script — easiest for an always-on service
cp .env.example .env && echo 'IETT_PASS=your-password-here' >> .env

# 2. a real environment variable — overrides the file
IETT_PASS=your-password-here python3 iett_tracker.py --map 522

# 3. or just edit .auth_secret and restart
```

Restart the app after changing it.

---

## Make it your line

Two steps: find your stop codes, then write them into a config file.

**1. Find your stops.** Every İETT stop has a numeric code. Ask the tool:

```bash
python3 iett_tracker.py --stops 500T        # every stop on a line, in order, with codes
python3 iett_tracker.py --search kadikoy    # search all stops by name
```

Note the codes for the stops you actually board at. Direction matters: a road usually has a
separate stop code for each side, and you want the one you stand on.

**2. Write your config.**

```bash
cp config.example.json config.json
```

Open it and edit. The minimum useful config is your line and one boarding stop:

```json
{
  "line": "500T",
  "focus": [
    {"line": "500T", "stop": "111111", "label": "→ Work", "at": "board at Kadıköy"}
  ],
  "eta": {"stop": "111111", "lines": ["500T"], "label": "KADIKÖY"},
  "app_name": "500T Tracker",
  "app_short_name": "500T"
}
```

Add a second `focus` entry for the way home and you get the there-and-back toggle. Every key
is optional and falls back to a default, so you can start small. `config.example.json`
documents all of them, and `config.json` is gitignored so your setup stays yours.

Then run it:

```bash
python3 iett_tracker.py --map 500T
```

**Two things worth knowing when picking stops.** The stops in `focus` are also the ones
prediction accuracy is scored against, so choose the ones you really use. And if your
boarding stop sits one or two stops from a route terminus, the tracker will correctly show
"waiting at origin" for buses parked there that have not set off yet, rather than pretending
they are a minute away.

---

## What you see

| On screen | Meaning |
| --- | --- |
| The big number | Minutes until the next bus reaches your stop |
| A range under it | The realistic spread, from actual observed variation |
| Coloured dot | Confidence, based on how much real data backs the number |
| `pos 40s ago` | Age of the bus's last GPS fix. İBB refreshes about once a minute, so 40 to 100 seconds is normal |
| Solid arrow | Live GPS fix |
| Hollow arrow | GPS lost. The bus is held at its last known spot rather than vanishing |
| A badge | Where the number came from: learned, timetable, waiting at origin, or estimated during an outage |

[GUIDE.md](GUIDE.md) explains all of this in plain language, including how much to trust the
number at different distances.

---

## How much to trust it

Measured against buses that actually turned up:

| Bus is | Median error | Verdict |
| --- | --- | --- |
| under 6 min away | about 1 min | Time your walk by it |
| 6 to 20 min away | about 1.5 min, near-unbiased | The useful range |
| 20 to 25 min away | drifting ~2 min optimistic | Usable, with salt |
| over 25 min away | 5 to 10 min out | Treat as a rumour, not a time |

The long-horizon weakness is not a bug waiting to be fixed. That far ahead the answer depends
on dispatch decisions nobody has made yet, so the uncertainty is irreducible; the app widens
the band instead of inventing confidence. [PREDICTION.md](PREDICTION.md) has the full
calibration and the method for re-measuring it on your own data.

---

## Running it 24/7

The app is just a Python process serving a web page, so any always-on machine works: an old
laptop, a Raspberry Pi, a home server.

```bash
python3 iett_tracker.py --map 522 --lan --port 8731 --no-open
```

`--lan` binds all interfaces so your phone can reach it, `--no-open` skips launching a
browser. Then open `http://<that-machine's-ip>:8731/` on your phone.

**To reach it from outside your home network**, the tidy answer is a private mesh VPN like
[Tailscale](https://tailscale.com): install it on the host and on your phone, then use the
host's Tailscale IP. No ports forwarded, nothing exposed to the internet.

**Do not put this on a public URL without thinking.** The generated password is the only
thing standing in front of it.

`deploy/` has ready-made setup scripts: `deploy/setup.sh` for Linux (systemd), and
`deploy/windows/` for Windows, where `install-service.bat` registers a boot task that
survives reboots and running on battery. See [deploy/README.md](deploy/README.md).

---

## How it works

One Python file. In `--map` mode it runs three things at once: a **collector** polling İBB
and learning, a **web server** serving the map and predictions, and background jobs for
backups and accuracy logging.

**The data source.** İBB's SOAP API. One method returns live positions for a line, others
return the route's stop list, geometry and timetable.

> **The rate limit is the whole ballgame.** The live-position service allows **100 requests
> per hour, per IP**, a number buried in a usage PDF. Early on this polled every 8 seconds,
> roughly 450/hr, and the constant "İBB outages" turned out to be self-inflicted throttling.
> Failed requests still count against the quota, so retrying while blocked keeps you blocked.
> The app now shares one upstream call per 40 seconds across every consumer and backs off
> hard when calls fail. That change took the failure rate from 100% to 0%. **If you fork
> this, do not speed up the polling.**

**It learns segment times.** Each poll shows where every bus is. Comparing consecutive polls
gives how long a bus took from stop A to stop B, stored with the hour and weekday. That
history is the core asset: it is what lets the app predict well even when the live feed dies.

**Predicting.** Sum the learned times for the segments between the bus and your stop,
preferring data from the same hour and weekday type, relaxing to broader data when a segment
is thin.

**Map-matching and a Kalman filter.** Raw GPS jitters and sits off the road. Each bus is
snapped onto the route line, turning its position into a single "distance along the route"
number, then smoothed with a small 1-D Kalman filter. That yields smooth motion, a real
per-bus speed, and a principled way to coast forward during an outage with widening
uncertainty.

**Degrading gracefully.** live → projected → headway → timetable. It never goes blank, and it
always tells you which one you are looking at.

**Checking itself.** Every prediction and every arrival is logged, then matched up to measure
real error. That is where the trust table above comes from, and it is how the bias
corrections were derived rather than guessed.

### Why the ETA is a range, and why that width

A prediction sums N segment times, each with its own spread. Adding the *ranges* assumes every
segment hits its slow end at the same instant, which balloons the band. Variances add instead,
so the spread grows with the square root of the segment count:

| Stops away | Adding ranges (wrong) | Adding variances (right) |
| --- | --- | --- |
| 1 | 1× | 1× |
| 4 | 4× | 2× |
| 9 | 9× | 3× |
| 20 | 20× | ~4.5× |

A small ×1.4 factor adds a little back, because a congested evening really does slow every
segment together. It is calibrated against measured error, not picked by feel.

---

## Configuration

Route and stops live in `config.json` (see [Make it your line](#make-it-your-line)).
Behaviour is tuned by constants near the top of `iett_tracker.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `IETT_USER` / `IETT_PASS` | `bus` / generated | Web login. Resolved in order: real environment variable, then `.env` beside the script, then `.auth_secret`, else generated and saved there |
| `--port` | `8731` | Listening port |
| `RUSH_INTERVAL` / `OFFPEAK_INTERVAL` | `40` / `40` | Seconds between polls. **Leave these alone**, see the rate limit above |
| `LIVE_TTL` | `40` | One shared upstream call per this many seconds. The real governor |
| `COLLECT_BACKOFF_MAX` | `900` | Cooldown ceiling when calls fail |
| `MIN_SAMPLES` | `3` | Observations before a segment is trusted |
| `BIAS_CORRECT` | `True` | Master switch for the self-correction stack |
| `UPDATE_ENABLED` | `False` | Remote self-update endpoint. Off, because "POST me a new Python file" is remote code execution. Deploy over SSH instead |
| `BACKUP_INTERVAL` / `BACKUP_KEEP` | daily / 7 | Database snapshots |

Useful endpoints, all behind the login: `/eta?stop=<code>`, `/positions`, `/stats`,
`/accuracy`, `/route`, `/schedule`, `/logs?n=N`.

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `api.fail_pct_1h` is high | You are rate-limited. The app self-throttles and recovers on its own. Do not poll faster, that is what causes it |
| ETAs show but no bus on the map | The bus lost GPS. It should render as a hollow arrow |
| Everything says "estimated" | İBB's gateway is having an episode. It backs off and returns by itself |
| Page will not load from your phone | Check the VPN, and that you used `--lan` and the right IP |
| A bus at the terminus shows a long wait | Correct. It has not departed yet |
| Predictions look wrong on a new line | Give it a few days. With no history it is running on the timetable seed |

Health check: open `/stats`. You want a rising `samples` and `api.fail_pct_1h` near zero.

---

## Documentation

| File | For |
| --- | --- |
| **README.md** | This file. Install, configure, run, and how it works |
| [GUIDE.md](GUIDE.md) | Plain language: reading the screen, trusting the number, what to do when it looks odd |
| [PREDICTION.md](PREDICTION.md) | Technical: the prediction pipeline, the bias-correction stack, measured calibration, how to re-measure |
| [PRODUCT.md](PRODUCT.md) | Design direction for the interface |
| [deploy/README.md](deploy/README.md) | Getting it running 24/7 |

---

## Lessons

Things that cost real time, in case they save you some.

**The rate limit was the whole ballgame**, as above. Most of the perceived flakiness was
self-inflicted, and retrying while blocked prolongs it.

**Measure before you fix.** An anomaly that looked like one bug decomposed into two unrelated
ones the moment it was split by prediction source, lead time, and stop. A single obvious fix
would have been wrong on both counts.

**Check dates against deploy boundaries.** One "bug" turned out to be entirely data from
before a feature shipped. Long-window averages silently blend old and new behaviour, and this
nearly caused a feature to be built twice.

**Write down your sign conventions.** Two measurement surfaces used opposite signs for error.
That single ambiguity made a successful fix look like it had overshot.

**Know which way is dangerous.** Predicting later than reality makes you miss the bus;
predicting earlier just makes you wait. Those are not equally bad, so the corrections
deliberately land on the waiting side. Optimising for mean absolute error alone would have
made the app worse at its only job.

**The good techniques are borrowed from real transit agencies.** Blending historical and
real-time data, map-matching, Kalman smoothing, headway prediction and scoring your own
accuracy are all standard practice. This is a single-route version of what city-scale systems
do.

---

## Notes

Uses İBB's public open-data API and stays within its published rate limit. No secrets are
committed: the password comes from `IETT_PASS` or the generated `.auth_secret`, and the
remote-update endpoint is disabled with no default token.

The learned database, logs and backups are gitignored. They are regenerable, and the database
records arrival times at whichever stops you track.

## Licence

MIT. See [LICENSE](LICENSE).
