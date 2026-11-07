"""Measure the real market from recorded data: implied opening target, BTC volatility, reaction lag."""
import sys, math, statistics, json
import sim
from bot import phi

def inv_phi(p):
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if phi(mid) < p: lo = mid
        else: hi = mid
    return (lo + hi) / 2

def mid_yes(r):
    b = r["books"].get("YES") or {}
    if b.get("bids") and b.get("asks"):
        return (b["bids"][0][0] + b["asks"][0][0]) / 2
    return None

def sigma_of(rows):
    pts, last = [], None
    for r in rows:
        if last is None or r["t"] - last[0] >= 1.0:
            pts.append((r["t"], r["btc"])); last = pts[-1]
    var = sum((b[1] - a[1]) ** 2 for a, b in zip(pts, pts[1:]))
    return math.sqrt(var / (pts[-1][0] - pts[0][0]))

def implied_target(m, sigma):
    ks = []
    for r in m["rows"]:
        T = m["end"] - r["t"]
        p = mid_yes(r)
        if p and 0.15 < p < 0.85 and 30 < T < 280:
            ks.append(r["btc"] - inv_phi(p) * sigma * math.sqrt(T))
    return statistics.median(ks) if ks else None

def lag_profile(m, sigma, K, max_k=16):
    rs = [r for r in m["rows"] if mid_yes(r) is not None]
    fair = [phi((r["btc"] - K) / (sigma * math.sqrt(max(m["end"] - r["t"], 1)))) for r in rs]
    mids = [mid_yes(r) for r in rs]
    df = [b - a for a, b in zip(fair, fair[1:])]
    dm = [b - a for a, b in zip(mids, mids[1:])]
    out = []
    for k in range(max_k):
        xs, ys = df[:len(df) - k], dm[k:]
        n = min(len(xs), len(ys))
        if n < 30: break
        mx, my = sum(xs[:n]) / n, sum(ys[:n]) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs[:n], ys[:n]))
        vx = math.sqrt(sum((x - mx) ** 2 for x in xs[:n])); vy = math.sqrt(sum((y - my) ** 2 for y in ys[:n]))
        out.append(cov / (vx * vy) if vx and vy else 0)
    return out

if __name__ == "__main__":
    paths = sys.argv[1:] or ["data"]
    rows = sim.load_rows(paths); mk = sim.markets_from_rows(rows, sim.load_outcomes(paths), min_rows=50)
    allrows = [r for m in mk for r in m["rows"]]
    sigma = sigma_of(allrows)
    print(f"{len(mk)} markets; BTC volatility {sigma:.2f} USD per sqrt(second)  (~${sigma*math.sqrt(300):.0f} per 5 min)")
    prof = [0.0] * 16; cnt = 0
    for m in mk:
        K = implied_target(m, sigma)
        if K is None: continue
        fin = m["rows"][-1]["btc"]
        print(f"  {m['slug'][-10:]} recorded open {m['open']['usd']:.1f}  implied target {K:.1f}  diff {K - m['open']['usd']:+.1f}  "
              f"winner {m['winner']}  implied winner {'YES' if fin >= K else 'NO'}")
        lp = lag_profile(m, sigma, K)
        for i, v in enumerate(lp): prof[i] += v
        cnt += 1
    print("correlation of the book's move with the fair-value move k ticks (0.5 s each) earlier:")
    print("  " + "  ".join(f"{k*0.5:.1f}s:{v/cnt:.2f}" for k, v in enumerate(prof) if cnt))
