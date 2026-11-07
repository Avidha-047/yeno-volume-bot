# Fair-Value Volume Bot – YeNo × Builderr Volume Bot Challenge

A paper-trading bot for YeNo BTC five-minute Up/Down markets. It answers `POST /decide` with `HOLD`, `BUY` or
`SELL`, uses the Python 3 standard library only, and never touches real money: Builderr's evaluator owns cash,
fills, fees and scoring.

```
python bot.py                 # serves POST /decide on 127.0.0.1:8080 (HOST / PORT override)
python yeno-smoke-test.py     # in a second terminal -> "contractSmokePassed": true
python -m unittest tests.test_all
```

## The problem in one line

Fees decide this challenge. The 1% YeNo overlay alone costs **$10 on $1,000 of volume, the whole starting cash**,
before the protocol fee `0.07 × shares × p × (1 − p)` and the spread. On real recorded markets the all-in cost is
**about 1.7–2.2% of every dollar traded**. A bot can only qualify if its trades earn back almost all of that, and
every cent it keeps is score (ranking = cash at the first clean $1,000 crossing).

## How the bot trades

| Rule | Setting | Why |
|---|---|---|
| **Fair value** `P(Up) = Φ((BTC − target) / (σ·√t))`, σ measured live from the reference feed (10-minute window, ×1.15 for fat tails) | – | A calibrated probability for each side, every request |
| **Buy only the favourite**, priced 0.55–0.97, in the last 17–240 s | fair ≥ 0.70 | The protocol fee per dollar, `0.07(1 − p)`, is lowest at high prices; entries are blocked inside 15 s |
| **All-in edge** = fair − (fill price + both fee layers + expected exit fee) | ≥ 3 cents | Trades only when the book is cheap against fair value after every cost |
| **Take profit** as soon as the bid is within 3 cents of fair value, or at 0.985 | – | Many small, likely wins instead of waiting for full convergence |
| **Wide stops**: sell if fair value < 0.50 or the round trip would lose > $1.50; always flat with 6 s left | – | Tight stops sold on brief dips that recovered; volume only counts for closed cycles |
| **Light pacing** toward $1,000 by hour 22: behind schedule the required edge drops slightly, ahead it rises | gain 0.10 | Qualifying first, then cash |
| **Depth and safety**: whole order must fit the book; skip if selling straight back loses > $0.35; BUY ≤ $5 incl. fees; stale (> 1.5 s) or provisional reference → HOLD; malformed input → HOLD; < 1 ms per decision | – | The evaluator fills 250 ms later on the later book; an invalid answer is fatal |

All settings are in `DEFAULTS` at the top of `bot.py`.

## Evidence

### Real markets (`results/`)

8.1 hours of live public Polymarket order books (63 usable markets, 57 with the official outcome) were recorded with
`recorder.py` and replayed through `sim.py`, a replica of the published evaluator (both fee layers, 250 ms latency on
the later own-outcome book, L2 depth, 5-share minimum, partial fills, 2 s staleness, no entries in the last 15 s,
settlement adds no volume, stop at the first clean $1,000 crossing). Each market's opening target was backed out of
its own order book; it matches the official winner in all but one market.

What the data showed (`results/market-measurements.txt`):
- the book moves with BTC with **no measurable lag** (correlation ≈ 0 at 0.5–7.5 s),
- favourites in the last 90 s are **not underpriced**,
- spreads are mostly 1 cent and BTC volatility was about $6 per √second.

24-hour runs built from these markets back to back, starting at six different markets (`day24.py`):

| Day built from market | Result | Cash at the $1,000 crossing | Hours to target |
|---|---|---|---|
| 0 | qualified | $8.99 | 21.8 |
| 8 | qualified | $6.37 | 21.8 |
| 15 | qualified | $4.88 | 21.3 |
| 31 | $998 – missed by $2 | – | – |
| 47 | qualified | $6.61 | 23.1 |
| 55 | qualified | $8.67 | 22.4 |

House Bot v1 on the same 8 hours: $119 volume, $4.30 cash. The best published diagnostic entry: $507.

**Limits, stated plainly:** the settings were tuned on the same 63 markets the table uses, and the six days reuse
them in different orders, so a new day will likely do somewhat worse. The main failure mode is an early losing run:
below about $5 of cash the 5-share minimum at high prices stops the bot.

### Synthetic markets (`synth.py`)

Used only to test code paths and compare strategies under stated assumptions (BTC random walk with volatility
regimes, a market that tracks fair value with a lag).

## Files

| File | Purpose |
|---|---|
| `bot.py` | **the bot** |
| `sim.py` | replica of the paper evaluator |
| `recorder.py` | records live public Polymarket books + BTC spot (read-only): `python recorder.py --check`, `python recorder.py` |
| `analyze.py`, `calib.py` | lag, volatility, implied target and calibration measurements on recorded data |
| `realtest2.py`, `day24.py`, `tune.py` | replay recorded markets through the simulator, with settings grids |
| `synth.py` | synthetic markets for code tests |
| `house_bot.py` | Builderr's House Bot v1 from the starter kit, unchanged, for comparison |
| `yeno-smoke-test.py`, `yeno-sample-observation.json` | Builderr's contract smoke test and sample request, unchanged |
| `tests/test_all.py` | unit tests: fees, $5 cap, decisions, settlement, volume accounting, speed |
