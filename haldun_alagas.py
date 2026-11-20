#!/usr/bin/env python3
"""Quick & shoddy: how far are the 14B and 14ES from the Haldun Alagaş stop?
Reuses iett_tracker.py. Usage: python3 haldun_alagas.py"""
import iett_tracker as t

LINES = ["14B", "14ES"]

stops = [s for s in t.all_stops() if "haldun" in t.norm(s.get("SDURAKADI", ""))
         and "alagas" in t.norm(s.get("SDURAKADI", ""))]
if not stops:
    raise SystemExit("No stop matching 'Haldun Alagaş' found")

for s in stops:                       # several stops may share the name (one per side)
    code = str(s.get("SDURAKKODU"))
    print(f"\n== {s.get('SDURAKADI')}  (code {code}) ==")
    rows = t.approaching(code, LINES)
    if not rows:
        print("  no 14B/14ES buses approaching this stop (or line doesn't serve it)")
    for r in rows:
        d = f"{r['dist_km']:.2f} km" if r["dist_km"] is not None else "? km"
        print(f"  {r['line']:5} bus {r['kapino']:8} {r['stops_away']} stops away, "
              f"{d} straight-line, now near '{r['near_name']}' (seen {r['seen']})")
