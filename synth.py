#!/usr/bin/env python3
"""Synthetic BTC five-minute Up/Down markets, in the recorder's row format.

ONLY for testing code paths and comparing strategies under stated assumptions. It is a model, not the market:
real tuning must use recorded data. Assumptions (all adjustable):
  * BTC follows a random walk whose volatility switches between calm / normal / wild regimes
  * the market price tracks the fair probability Phi(gap / (sigma * sqrt(t))) with a lag (seconds), plus noise
  * spreads 1-3 cents, depth 10-400 shares per level, prices on a 0.01 tick, clipped to [0.01, 0.99]
"""
from __future__ import annotations

import math
import random


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def make_day(seed=0, hours=24.0, tick=0.5, start=1_790_000_100, lag=4.0, noise=0.015, price=80_000.0):
    rng = random.Random(seed)
    regimes = [(3.0, 0.35), (6.0, 0.45), (11.0, 0.20)]          # (USD per sqrt(second), share of time)
    sigma = 6.0
    rows, outcomes = [], {}
    t = float(start)
    n_markets = int(hours * 12)
    for k in range(n_markets):
        m_start = start + 300 * k
        if rng.random() < 0.15:                                 # regime change between markets
            r = rng.random()
            acc = 0
            for s, w in regimes:
                acc += w
                if r <= acc:
                    sigma = s * rng.uniform(0.8, 1.25)
                    break
        slug = f"btc-updown-5m-{m_start}"
        open_px = price
        opening = {"usd": open_px, "observedAt": m_start + 0.6, "sources": ["synthetic"]}
        mkt_p = 0.5
        spread_base = rng.choice([0.01, 0.01, 0.02, 0.03])
        t = m_start + 0.3
        while t < m_start + 300:
            price += rng.gauss(0, sigma * math.sqrt(tick))
            if rng.random() < 0.002:                            # occasional jump
                price += rng.gauss(0, sigma * 6)
            left = m_start + 300 - t
            fair = phi((price - open_px) / (sigma * math.sqrt(max(left, 0.5))))
            alpha = 1 - math.exp(-tick / lag)
            mkt_p += alpha * (fair - mkt_p)
            mid = min(0.995, max(0.005, mkt_p + rng.gauss(0, noise)))
            spread = spread_base if 0.05 < mid < 0.95 else 0.01
            yb = max(0.01, min(0.98, math.floor((mid - spread / 2) * 100) / 100))
            ya = max(yb + 0.01, min(0.99, math.ceil((mid + spread / 2) * 100) / 100))
            def side(bid, ask):
                bids = [[round(bid - 0.01 * i, 2), round(rng.uniform(10, 400), 2)] for i in range(5) if bid - 0.01 * i > 0.0]
                asks = [[round(ask + 0.01 * i, 2), round(rng.uniform(10, 400), 2)] for i in range(5) if ask + 0.01 * i < 1.0]
                if rng.random() < 0.05:
                    asks = asks[:1] if asks else asks
                    if asks:
                        asks[0][1] = round(rng.uniform(1, 8), 2)
                return {"bids": bids, "asks": asks, "minOrderSize": 5}
            books = {"YES": side(yb, ya), "NO": side(round(1 - ya, 2), round(1 - yb, 2))}
            rows.append({"t": round(t, 3), "market": slug, "start": m_start, "end": m_start + 300, "btc": round(price, 2),
                         "open": opening if t >= opening["observedAt"] else None, "books": books})
            t += tick * rng.uniform(0.8, 1.2)
        outcomes[slug] = "YES" if price >= open_px else "NO"
    return rows, outcomes
