"""Unit tests for the load-bearing pure functions: the bias-correction stack,
the horizon ramp, the Kalman update, and the holiday bucketing. These break
SILENTLY on an innocent edit (a wrong number still produces a plausible ETA),
which is exactly why they earn tests when the rest of this codebase doesn't.

Run:  python3 test_tracker.py    (stdlib only; no pytest, no network, no DB)

Third-party deps (requests/rich) are stubbed so the module imports offline.
"""
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import datetime as D
from unittest.mock import MagicMock

_BLOCK = {"requests", "rich", "pypdf", "PIL", "openpyxl", "docx", "numpy", "bs4", "lxml"}


class _Stub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        return importlib.machinery.ModuleSpec(name, self) if top in _BLOCK else None

    def create_module(self, spec):
        m = MagicMock(name=spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        pass


def _load():
    sys.meta_path.insert(0, _Stub())
    spec = importlib.util.spec_from_file_location("iett_tracker", "iett_tracker.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


t = _load()

_passed = _failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ── horizon ramp ────────────────────────────────────────────────────────────
def test_horizon_bias():
    # anchor points reproduce exactly
    for lead, want in t.HORIZON_BIAS_ANCHORS:
        check(f"horizon anchor {lead}", approx(t._horizon_bias(lead), want))
    # flat cap past the last anchor, flat-held below the first
    last_x, last_y = t.HORIZON_BIAS_ANCHORS[-1]
    first_x, first_y = t.HORIZON_BIAS_ANCHORS[0]
    check("horizon capped past last anchor", approx(t._horizon_bias(99), last_y))
    check("horizon held below first anchor", approx(t._horizon_bias(0), first_y))
    # close-in leans NEGATIVE (subtracts time) so the bus can't beat the number;
    # it crosses zero and turns positive further out
    check("horizon negative close in", t._horizon_bias(3) < 0)
    check("horizon positive far out", t._horizon_bias(12) > 0)
    # monotonic non-decreasing across the ramp
    vals = [t._horizon_bias(x) for x in range(0, 40)]
    check("horizon monotonic", all(b >= a - 1e-9 for a, b in zip(vals, vals[1:])))
    # linear interpolation midway between the first two anchors
    (x0, y0), (x1, y1) = t.HORIZON_BIAS_ANCHORS[0], t.HORIZON_BIAS_ANCHORS[1]
    check("horizon interpolates", approx(t._horizon_bias((x0 + x1) / 2), (y0 + y1) / 2))
    check("horizon None-safe", t._horizon_bias(None) == 0.0)


# ── the four-source bias stack ──────────────────────────────────────────────
def test_bias_stack():
    off = t.BIAS_OFFSETS.get(t._time_bucket(__import__("time").time()), 0.0)

    rows = [
        {"eta_src": "history", "eta_min": 8, "eta_lo": 6, "eta_hi": 11},
        {"eta_src": "estimate", "eta_min": 2, "eta_lo": None, "eta_hi": None},
        {"eta_src": "origin-wait", "eta_min": 12, "eta_lo": 9, "eta_hi": 15},
        {"eta_src": "projected", "eta_min": 7},
        {"eta_src": "history", "eta_min": None},   # 'no more runs' — must be skipped
    ]
    before = [dict(r) for r in rows]
    t._apply_bias_correction(rows)

    # history: floor + horizon ramp, band shifts with the point
    exp_hist = max(0, round(before[0]["eta_min"] + off + t._horizon_bias(8)))
    check("history point corrected", rows[0]["eta_min"] == exp_hist)
    check("history band tracks point",
          rows[0]["eta_hi"] == max(0, round(before[0]["eta_hi"] + off + t._horizon_bias(8))))
    # estimate: its own constant, pulled toward 'due'
    check("estimate pulled back",
          rows[1]["eta_min"] == max(0, round(2 + t.ESTIMATE_BIAS)))
    # origin-wait: its own flat constant
    check("origin-wait pulled back",
          rows[2]["eta_min"] == max(0, round(12 + t.ORIGIN_WAIT_BIAS)))
    # projected (outage dead-reckon) is left ALONE
    check("projected untouched", rows[3]["eta_min"] == 7)
    # None eta never crashes and stays None
    check("None eta preserved", rows[4]["eta_min"] is None)

    # corrections never flip a bus to a negative ETA
    edge = [{"eta_src": "estimate", "eta_min": 0}]
    t._apply_bias_correction(edge)
    check("no negative ETA", edge[0]["eta_min"] >= 0)

    # master switch off = identity
    t.BIAS_CORRECT = False
    try:
        r = [{"eta_src": "history", "eta_min": 10, "eta_lo": 8, "eta_hi": 13}]
        t._apply_bias_correction(r)
        check("BIAS_CORRECT off is identity", r[0]["eta_min"] == 10)
    finally:
        t.BIAS_CORRECT = True


# ── Kalman update ───────────────────────────────────────────────────────────
def test_kalman():
    t._kf_state.clear()
    key = ("522", "G", "TESTBUS")
    # first fix just seeds state at the measurement
    s0 = t._kf_update("522", "G", "TESTBUS", 1000.0, 100.0)
    check("kf seeds at first measurement", approx(s0[0], 1000.0, 1.0))

    # a forward move produces a positive along-route speed
    s1 = t._kf_update("522", "G", "TESTBUS", 1100.0, 110.0)   # +100 m in 10 s
    check("kf tracks forward position", 1000.0 <= s1[0] <= 1100.0)
    check("kf speed forward-only (>=0)", s1[1] >= 0.0)

    # a >500 m jump is rejected by the innovation gate: state does NOT teleport
    s2 = t._kf_update("522", "G", "TESTBUS", 5000.0, 120.0)
    check("kf innovation gate rejects a teleport", s2[0] < 3000.0)

    # a backward measurement must not yield a negative (reversing) speed
    t._kf_state.clear()
    t._kf_update("522", "D", "REV", 2000.0, 200.0)
    sb = t._kf_update("522", "D", "REV", 1980.0, 210.0)   # slight backward
    check("kf never reports reversing speed", sb[1] >= 0.0)


# ── holiday bucketing (new) ─────────────────────────────────────────────────
def test_holidays():
    # a known fixed national holiday (Republic Day, 29 Oct) on a weekDAY
    rep = D.datetime(2026, 10, 29, 9, 0)   # a Thursday
    check("29 Oct is a holiday", t.is_holiday(rep))
    check("holiday maps to Sunday dow", t.effective_dow(rep) == 6)
    check("holiday daycode is P", t._daycode(rep) == "P")
    check("holiday -> weekend bucket",
          t._time_bucket(rep.timestamp()) == "weekend")

    # a Bayram date from the explicit list
    check("bayram listed", t.is_holiday(D.datetime(2026, 5, 28, 8, 0)))

    # an ORDINARY weekday: every holiday path is a no-op
    wd = D.datetime(2026, 7, 23, 9, 0)     # a Thursday, not a holiday
    check("ordinary day not a holiday", not t.is_holiday(wd))
    check("ordinary dow unchanged", t.effective_dow(wd) == wd.weekday())
    check("ordinary daycode I", t._daycode(wd) == "I")

    # real Saturday and Sunday still code correctly
    sat = D.datetime(2026, 7, 25, 12, 0)
    sun = D.datetime(2026, 7, 26, 12, 0)
    check("Saturday -> C", t._daycode(sat) == "C")
    check("Sunday -> P", t._daycode(sun) == "P")


# ── accuracy metric de-dups per bus (not per logged prediction) ─────────────
def test_accuracy_dedup():
    import sqlite3
    import time as _time
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE arr(line TEXT,kapino TEXT,stop TEXT,ts REAL)")
    conn.execute("CREATE TABLE pred(line TEXT,kapino TEXT,stop TEXT,"
                 "pred_arr REAL,made REAL,eta_min REAL,src TEXT)")
    now = _time.time()

    # Bus A: ONE arrival, but a burst of 10 predictions logged every minute as it
    # crawled in (leads 3..12 min), each predicting +3 min late.
    arr_a = now - 200
    for lead in range(3, 13):
        conn.execute("INSERT INTO pred VALUES('522','BUSA','S',?,?,?,'history')",
                     (arr_a + 180, arr_a - lead * 60, lead))
    conn.execute("INSERT INTO arr VALUES('522','BUSA','S',?)", (arr_a,))

    # Bus B: a single accurate prediction (+1 min) at an 8-min lead.
    arr_b = now - 4000
    conn.execute("INSERT INTO pred VALUES('522','BUSB','S',?,?,8,'history')",
                 (arr_b + 60, arr_b - 8 * 60))
    conn.execute("INSERT INTO arr VALUES('522','BUSB','S',?)", (arr_b,))
    conn.commit()

    orig = t.db
    t.db = lambda: conn
    try:
        res = t.accuracy_stats(days=30)
    finally:
        t.db = orig

    # 11 raw predictions across 2 buses -> scored as 2, not 11
    check("dedup: 2 buses scored not 11", res["buses_scored"] == 2)
    check("dedup: raw prediction count preserved", res["predictions"] == 11)
    check("dedup: near10 counts each bus once", res["near10"]["n"] == 2)
    # bias = mean of the two representatives (+3, +1) = +2, NOT the burst-weighted 2.8
    check("dedup: burst does not dominate bias", abs(res["near10"]["bias"] - 2.0) < 0.11)


# ── adaptive corrections: hour bias + gated last-bus feedback ────────────────
def test_adaptive():
    import sqlite3
    import time as _time
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE arr(line TEXT,kapino TEXT,stop TEXT,ts REAL)")
    conn.execute("CREATE TABLE pred(line TEXT,kapino TEXT,stop TEXT,pred_arr REAL,"
                 "made REAL,eta_min REAL,src TEXT,raw_arr REAL)")
    now = _time.time()

    def bus(stop, kap, ts, err_min):
        # one arrival + one raw prediction at an 8-min lead, off by err_min
        conn.execute("INSERT INTO arr VALUES('522',?,?,?)", (kap, stop, ts))
        conn.execute("INSERT INTO pred VALUES('522',?,?,?,?,8,'history',?)",
                     (kap, stop, ts + err_min * 60 + 999, ts - 480, ts + err_min * 60))

    # Stop CORR: 80 buses, 12 min apart, error drifting slowly in blocks of 20
    # (−3 → +3 → −3 → +3), so consecutive buses share their error (high lag-1
    # correlation). The LAST block ran +3: raw prediction − actual = +3 means the bus
    # arrived 3 min EARLIER than raw said (raw pessimistic) → correction must SUBTRACT.
    for i in range(80):
        err = 3.0 if (i // 20) % 2 == 1 else -3.0
        bus("CORR", f"C{i}", now - (80 - i) * 720, err)
    # Stop NOISE: 80 buses, alternating ±3 → lag-1 correlation ≈ −1 → feedback OFF
    for i in range(80):
        bus("NOISE", f"N{i}", now - (80 - i) * 720, 3.0 if i % 2 else -3.0)
    conn.commit()

    orig = t.db
    t.db = lambda: conn
    t._adapt_cache.clear()
    try:
        sc = t._adaptive_state("CORR")
        check("adaptive: feedback enabled where errors correlate", sc["feedback"])
        check("adaptive: recent residuals captured", len(sc["recent"]) == t.FEEDBACK_N)
        sn = t._adaptive_state("NOISE")
        check("adaptive: feedback gated OFF where errors don't correlate", not sn["feedback"])
        check("adaptive: hour bias learned (some hour populated)", len(sc["hour"]) > 0)

        # the correction moves the shown ETA the right way and keeps the raw value
        rows = [{"eta_src": "history", "eta_min": 10, "eta_lo": 8, "eta_hi": 13},
                {"eta_src": "origin-wait", "eta_min": 10},
                {"eta_src": "estimate", "eta_min": 4}]
        shift = t._apply_adaptive(rows, "CORR")
        check("adaptive: learned row shifted down (raw ran pessimistic)", rows[0]["eta_min"] < 10)
        check("adaptive: raw_min kept for logging", rows[0]["raw_min"] == 10)
        check("adaptive: band follows the point", rows[0]["eta_hi"] < 13)
        check("adaptive: origin-wait untouched", rows[1]["eta_min"] == 10 and "raw_min" not in rows[1])
        check("adaptive: estimate untouched", rows[2]["eta_min"] == 4)
        check("adaptive: reports the shift applied", shift < 0)

        # master switch off = identity
        t.ADAPT_CORRECT = False
        try:
            r = [{"eta_src": "history", "eta_min": 10}]
            check("adaptive: switch off is identity", t._apply_adaptive(r, "CORR") == 0.0
                  and r[0]["eta_min"] == 10)
        finally:
            t.ADAPT_CORRECT = True
        # a non-accuracy stop through the public entry point: adaptive not applied
        r = [{"eta_src": "history", "eta_min": 10, "eta_lo": 8, "eta_hi": 13}]
        t._apply_bias_correction(r, "999999")
        check("adaptive: skipped for a non-accuracy stop", "raw_min" not in r[0])
    finally:
        t.db = orig
        t._adapt_cache.clear()


# ── sub-stop position: which segment, how far along ─────────────────────────
def test_locate_segment():
    by_sira = {1: "A", 2: "B", 3: "C", 4: "D"}
    s_idx = {"A": 0.0, "B": 1000.0, "C": 1600.0, "D": 2600.0}
    # bus 250 m past B, İETT says nearest stop is B (sira 2): on segment 2→3, 42% done
    k, f = t._locate_segment(1250.0, 2, by_sira, s_idx)
    check("segment: start sira", k == 2)
    check("segment: fraction", approx(f, 250 / 600, 1e-9))
    # nearest stop reported AHEAD of the bus (C, sira 3) while it is still on 2→3:
    # must resolve to the segment BEHIND the reported stop
    k, f = t._locate_segment(1500.0, 3, by_sira, s_idx)
    check("segment: nearest-ahead resolves to previous segment", k == 2 and f > 0.8)
    # exactly at a stop → fraction 0 of the next segment
    k, f = t._locate_segment(1600.0, 3, by_sira, s_idx)
    check("segment: at stop = start of next", k == 3 and approx(f, 0.0))
    # bus far from the reported stop's neighbourhood (route doubles back): None
    check("segment: off-neighbourhood is None", t._locate_segment(2550.0, 1, by_sira, s_idx) is None)
    # missing geometry for a stop → None, never a crash
    check("segment: missing stop metres is None", t._locate_segment(500.0, 1, by_sira, {"A": 0.0}) is None)


if __name__ == "__main__":
    for fn in (test_horizon_bias, test_bias_stack, test_kalman, test_holidays,
               test_accuracy_dedup, test_adaptive, test_locate_segment):
        fn()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
