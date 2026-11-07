#!/usr/bin/env python3
"""Tune the bot on RECORDED data (from recorder.py), one simulated 24-hour run per recorded day.

    python tune.py --data data                                     # current defaults
    python tune.py --data data --grid '{"edge":[0.01,0.02,0.03],"pace_gain":[0.2,0.4]}'

Pick settings that qualify on (nearly) every day first, then the highest median cash. Do not pick the single
best day: the official run is one unseen day.
"""
import argparse, itertools, json, statistics
import sim, bot as B

def windows(markets, hours=24.0):
    out, cur = [], []
    for m in markets:
        if cur and m["start"] - cur[0]["start"] >= hours * 3600:
            out.append(cur); cur = []
        cur.append(m)
    if cur and cur[-1]["end"] - cur[0]["start"] >= 0.5 * hours * 3600:
        out.append(cur)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", default=["data"])
    ap.add_argument("--grid", default="{}")
    ap.add_argument("--hours", type=float, default=24.0)
    a = ap.parse_args()
    rows = sim.load_rows(a.data)
    mk = sim.markets_from_rows(rows, sim.load_outcomes(a.data))
    wins = windows(mk, a.hours)
    print(f"{len(rows)} rows, {len(mk)} usable markets, {len(wins)} run(s) of up to {a.hours:g} h")
    grid = json.loads(a.grid)
    keys = list(grid)
    for combo in itertools.product(*grid.values()) if keys else [()]:
        params = dict(zip(keys, combo))
        res = [sim.run(B.Bot(**params), w, hours=a.hours) for w in wins]
        q = sum(r["qualified"] for r in res)
        print(f"{params or 'defaults'}: qualified {q}/{len(res)} | median cash ${statistics.median(r['cash'] for r in res):.2f} | "
              f"median volume ${statistics.median(r['volume'] for r in res):.0f}")
        for r in res:
            print("    " + sim.summary(r))

if __name__ == "__main__":
    main()
