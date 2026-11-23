#!/usr/bin/env python3
"""Quick & shoddy: how far are buses from a stop?
Usage: python3 haldun_alagas.py [LINE ...] [--stop "name words"]
Default: lines 14B 14ES, stop 'Haldun Alagaş'. Prints debug info so we can see
where it goes wrong."""
import sys
import iett_tracker as t

args = sys.argv[1:]
words = ["haldun", "alagas"]
if "--stop" in args:
    i = args.index("--stop")
    words = t.norm(args[i + 1]).split()
    del args[i:i + 2]
LINES = [a.upper() for a in args] or ["14B", "14ES"]

for line in LINES:
    print(f"\n===== Line {line} =====")
    route = t.cached_route(line)
    print(f"route rows: {len(route)}")
    if route:
        print("sample route row:", route[0])
    hits = {}
    for r in route:
        name = t.norm(r.get("DURAKADI", ""))
        if all(w in name for w in words):
            hits[str(r.get("DURAKKODU"))] = (r.get("DURAKADI"), r.get("YON"), r.get("SIRANO"))
    print("matching stops on this line:", hits or "NONE")
    try:
        buses = t.api_live_buses(line)
    except (Exception, SystemExit) as e:
        print("live fetch FAILED:", e)
        continue
    print(f"live buses: {len(buses)}")
    for b in buses[:3]:
        print("  sample bus:", b)
    for code, (name, leg, sira) in hits.items():
        rows = t.approaching(code, [line])
        print(f"-- {name} (code {code}, leg {leg}, stop #{sira}) --")
        if not rows:
            print("  no buses approaching (all passed it / wrong direction)")
        for r in rows:
            d = f"{r['dist_km']:.2f} km" if r["dist_km"] is not None else "? km"
            print(f"  bus {r['kapino']:8} {r['stops_away']} stops away, {d} straight-line, "
                  f"near '{r['near_name']}' (seen {r['seen']})")
