import sys, copy, json, itertools
import sim, analyze, bot as B
paths = sys.argv[1:2]; rot = int(sys.argv[2]); grids = [json.loads(g) for g in sys.argv[3:]]
rows = sim.load_rows(paths); mk = sim.markets_from_rows(rows, sim.load_outcomes(paths), min_rows=50)
sigma = analyze.sigma_of([r for m in mk for r in m["rows"]])
for m in mk:
    K = analyze.implied_target(m, sigma)
    if K: m["open"] = dict(m["open"], usd=K)
mk = mk[rot % len(mk):] + mk[:rot % len(mk)]   # start the day at a different market
# tile the real markets back to back into one 24-hour day (each copy shifted in time)
day, shift, k = [], 0, 0
while len(day) < 288:
    for m in mk:
        if len(day) >= 288: break
        off = (len(day) * 300) - (m["start"] - mk[0]["start"])
        c = copy.deepcopy(m); c["slug"] = f"{m['slug']}-c{len(day)}"
        d = len(day) * 300 + mk[0]["start"] - m["start"]
        c["start"] += d; c["end"] += d
        c["open"] = dict(c["open"], observedAt=c["open"]["observedAt"] + d)
        for r in c["rows"]:
            r["t"] += d; r["market"] = c["slug"]
        day.append(c)
for p in grids:
    r = sim.run(B.Bot(**p), day, hours=24)      # sim.run only reads the markets
    print(p, sim.summary(r), flush=True)
