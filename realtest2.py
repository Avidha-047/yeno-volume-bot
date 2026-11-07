import sys, statistics, importlib, copy, json
import sim, analyze, bot as B
H = importlib.import_module("house_bot")
paths = sys.argv[1:2]; grid = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
rows = sim.load_rows(paths); mk = sim.markets_from_rows(rows, sim.load_outcomes(paths), min_rows=50)
sigma = analyze.sigma_of([r for m in mk for r in m["rows"]])
for m in mk:
    K = analyze.implied_target(m, sigma)
    if K: m["open"] = dict(m["open"], usd=K)
hours = sum(m["end"] - m["start"] for m in mk) / 3600
def show(name, bot):
    r = sim.run(bot, copy.deepcopy(mk), hours=48)
    c = [x for x in r["log"] if x.get("pnl") is not None]
    pnl = sum(x["pnl"] for x in c); vol = r["volume"]
    print(f"{name:<44} vol ${vol:5.0f} ({vol/hours:4.0f}/h)  cash ${r['cash']:6.2f}  cycles {r['cycles']:3}  wins {sum(1 for x in c if x['pnl']>0):3}/{len(c):<3} "
          f"pnl per $100 vol {100*pnl/vol if vol else 0:+.2f}  settled {r['settled']}", flush=True)
    return r
print(f"{len(mk)} markets, {hours:.1f} h of market time; BTC sigma {sigma:.2f}")
if grid is None:
    show("house bot v1", H.HouseBot())
    show("ours (defaults)", B.Bot())
else:
    import itertools
    for combo in itertools.product(*grid.values()):
        p = dict(zip(grid, combo)); show(str(p), B.Bot(**p))
