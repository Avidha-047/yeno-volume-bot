"""Is the favourite underpriced near the close? Win rate vs ask, by seconds left. One sample per market per bucket."""
import sys, collections
import sim
paths = sys.argv[1:]
rows = sim.load_rows(paths); mk = sim.markets_from_rows(rows, sim.load_outcomes(paths), min_rows=50)
span = (mk[-1]["end"] - mk[0]["start"]) / 3600
print(f"{len(rows)} rows, {len(mk)} usable markets over {span:.1f} h; outcomes from Polymarket for {sum(1 for m in mk if m['slug'] in sim.load_outcomes(paths))}")
T_BUCKETS = [(15, 45), (45, 90), (90, 150), (150, 240)]
P_BUCKETS = [(0.55, 0.70), (0.70, 0.80), (0.80, 0.88), (0.88, 0.93), (0.93, 0.97), (0.97, 0.995)]
stats = collections.defaultdict(lambda: [0, 0, 0.0])      # n, wins, sum(ask)
for m in mk:
    seen = set()
    for r in m["rows"]:
        T = m["end"] - r["t"]
        tb = next((b for b in T_BUCKETS if b[0] <= T < b[1]), None)
        if not tb: continue
        for side in ("YES", "NO"):
            asks = (r["books"].get(side) or {}).get("asks") or []
            if not asks: continue
            a = asks[0][0]
            pb = next((b for b in P_BUCKETS if b[0] <= a < b[1]), None)
            if not pb or (tb, pb) in seen: continue
            seen.add((tb, pb))
            s = stats[(tb, pb)]; s[0] += 1; s[1] += m["winner"] == side; s[2] += a
print("seconds left | ask bucket | markets | win rate | avg ask | edge (win rate - ask)")
for tb in T_BUCKETS:
    for pb in P_BUCKETS:
        n, w, sa = stats[(tb, pb)]
        if n: print(f"  {tb[0]:>3}-{tb[1]:<3}s   {pb[0]:.2f}-{pb[1]:.3f}   {n:>4}    {w/n:6.1%}   {sa/n:.3f}   {w/n - sa/n:+.3f}")
