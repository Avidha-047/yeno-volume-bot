#!/usr/bin/env python3
"""Paper evaluator replica for the YeNo x Builderr Volume Bot Challenge.

Implements the published contract (yeno-evaluator-contract.md) as closely as it is described:
  * $10 start, BUY cash incl. fees <= $5, one position or pending BUY, YeNo BTC 5-minute markets only
  * fees on each side: protocol 0.07 * shares * price * (1 - price) (rounded to 5 dp) + 1% overlay on gross
  * every action executes against a LATER book update of its own outcome, at least 250 ms after the decision
  * L2 depth walk, minimum order size (BUY below it is rejected), partial fills, split exits stay one cycle
  * books older than 2 s rejected, no new entries inside the final 15 s, a SELL can fill when asks are gone
  * a position still open at the close settles (winner pays $1/share) and adds NO volume
  * volume = buy gross + sell gross of a fully closed cycle; the run stops at the first clean $1,000 crossing
Ranking stats: cash at crossing, max drawdown of settled cash, time to target.

Input: rows as written by recorder.py (or synth.py), grouped by market, in time order.
"""
from __future__ import annotations

import gzip
import json
import math
import os

PROTOCOL_RATE = 0.07
OVERLAY = 0.01
LATENCY = 0.25
STALE = 2.0
NO_ENTRY = 15.0
SHARE_STEP = 0.01
START_CASH = 10.0
MAX_BUY = 5.0
TARGET = 1000.0


def fees(fills):
    gross = sum(p * q for p, q in fills)
    protocol = sum(PROTOCOL_RATE * q * p * (1 - p) for p, q in fills)
    return round(protocol + 1e-12, 5) + round(gross * OVERLAY + 1e-12, 5)


def fill_buy(asks, max_cash):
    """Largest share count (in 0.01 steps) whose gross + fees fit in max_cash, walking the asks."""
    levels = sorted((p, q) for p, q in asks if 0 < p < 1 and q > 0)
    fills, shares = [], 0.0
    for p, q in levels:
        unit = p * (1 + OVERLAY) + PROTOCOL_RATE * p * (1 - p)
        spent = sum(pp * qq for pp, qq in fills) + fees(fills) if fills else 0.0
        room = max_cash - spent
        take = min(q, math.floor(room / unit / SHARE_STEP) * SHARE_STEP)
        if take <= 0:
            break
        fills.append((p, take))
        while fills and sum(pp * qq for pp, qq in fills) + fees(fills) > max_cash + 1e-9:   # rounding guard
            p0, q0 = fills[-1]
            q0 = round(q0 - SHARE_STEP, 2)
            fills[-1] = (p0, q0)
            if q0 <= 0:
                fills.pop()
        shares = sum(qq for _, qq in fills)
        if take < q:
            break
    return fills, shares


def fill_sell(bids, shares):
    fills, left = [], shares
    for p, q in sorted(((p, q) for p, q in bids if 0 < p < 1 and q > 0), reverse=True):
        take = min(q, left)
        fills.append((p, take))
        left = round(left - take, 6)
        if left <= 1e-9:
            break
    return fills


def load_rows(paths):
    rows = []
    for path in paths:
        files = [os.path.join(path, f) for f in sorted(os.listdir(path))] if os.path.isdir(path) else [path]
        for f in files:
            if not (f.endswith(".jsonl") or f.endswith(".jsonl.gz")) or os.path.basename(f).startswith("markets"):
                continue
            op = gzip.open if f.endswith(".gz") else open
            try:
                with op(f, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass                       # a line cut off when the recorder stopped
            except (EOFError, OSError):
                pass
    rows.sort(key=lambda r: r["t"])
    return rows


def load_outcomes(paths):
    out = {}
    for path in paths:
        f = os.path.join(path, "markets.jsonl") if os.path.isdir(path) else None
        if f and os.path.exists(f):
            for line in open(f, encoding="utf-8"):
                try:
                    m = json.loads(line)
                    if m.get("outcome") in ("up", "down"):
                        out[m["slug"]] = "YES" if m["outcome"] == "up" else "NO"
                except ValueError:
                    pass
    return out


def markets_from_rows(rows, outcomes=None, min_rows=100):
    """Group rows per market; drop markets with too little data (like the evaluator's data checks)."""
    by = {}
    for r in rows:
        if r.get("books") and r.get("btc"):
            by.setdefault(r["market"], []).append(r)
    mk = []
    for slug, rs in by.items():
        rs.sort(key=lambda r: r["t"])
        opening = next((r["open"] for r in rs if r.get("open")), None)
        if len(rs) < min_rows or not opening:
            continue
        win = (outcomes or {}).get(slug)
        if win is None:                                  # fall back to the last recorded price vs the opening
            win = "YES" if rs[-1]["btc"] >= opening["usd"] else "NO"
        mk.append({"slug": slug, "start": rs[0]["start"], "end": rs[0]["end"], "rows": rs, "open": opening, "winner": win})
    mk.sort(key=lambda m: m["start"])
    return mk


class Result(dict):
    __getattr__ = dict.get


def run(bot, markets, hours=24.0, verbose=False):
    """Replay markets (in order, as one continuous run) through bot.decide. Returns a Result."""
    cash, volume, cycles, peak, dd = START_CASH, 0.0, 0, START_CASH, 0.0
    pos = None          # {"market","outcome","shares","buy_gross","buy_fees","sell_gross","sell_fees"}
    pending = None      # (action dict, decided_at)
    invalid = rejected = settled = 0
    t0 = markets[0]["start"] if markets else 0
    log = []
    crossed_at = None
    for m in markets:
        if m["start"] - t0 >= hours * 3600:
            break
        rows = m["rows"]
        for i, r in enumerate(rows):
            now = r["t"]
            secs = m["end"] - now
            books = r["books"]
            # 1) execute a pending action on this later update of its own outcome
            if pending:
                act, at = pending
                side = act.get("outcome") if act["action"] == "BUY" else (pos or {}).get("outcome")
                b = books.get(side or "", {})
                if now - at >= LATENCY and b:
                    pending = None
                    if act["action"] == "BUY" and pos is None:
                        if secs <= NO_ENTRY:
                            rejected += 1
                        else:
                            cap = min(float(act.get("maxCashUsd") or 0), MAX_BUY, cash)
                            f, sh = fill_buy(b.get("asks") or [], cap)
                            mos = b.get("minOrderSize") or 5
                            if sh + 1e-9 < mos:
                                rejected += 1
                            else:
                                g, fe = sum(p * q for p, q in f), fees(f)
                                cash -= g + fe
                                pos = {"market": m["slug"], "outcome": side, "shares": round(sh, 2), "buy_gross": g,
                                       "buy_fees": fe, "sell_gross": 0.0, "sell_fees": 0.0, "opened": now}
                    elif act["action"] == "SELL" and pos is not None:
                        f = fill_sell(b.get("bids") or [], pos["shares"])
                        if f:
                            g, fe = sum(p * q for p, q in f), fees(f)
                            cash += g - fe
                            pos["sell_gross"] += g
                            pos["sell_fees"] += fe
                            pos["shares"] = round(pos["shares"] - sum(q for _, q in f), 2)
                            if pos["shares"] <= 1e-9:
                                cyc = pos["buy_gross"] + pos["sell_gross"]
                                volume += cyc
                                cycles += 1
                                pnl = pos["sell_gross"] - pos["sell_fees"] - pos["buy_gross"] - pos["buy_fees"]
                                log.append({"t": now, "market": m["slug"], "side": pos["outcome"], "pnl": round(pnl, 4),
                                            "volume": round(cyc, 4), "held": round(now - pos["opened"], 1)})
                                pos = None
                                peak = max(peak, cash)
                                dd = max(dd, peak - cash)
                                if volume >= TARGET:
                                    crossed_at = now
                                    break
                        else:
                            rejected += 1
            if crossed_at:
                break
            if pending:
                continue
            # 2) ask the bot (the evaluator asks when both books are fresh and nothing is pending)
            if not books.get("YES") or not books.get("NO") or secs <= 0:
                continue
            obs = {
                "schemaVersion": 1, "timestamp": now,
                "market": {"id": m["slug"], "secondsToClose": round(secs, 3)},
                "account": {"cashUsd": round(cash, 6), "eligibleVolumeUsd": round(volume, 6), "completedCycles": cycles,
                            "position": None if pos is None or pos["market"] != m["slug"] else {
                                "market_id": pos["market"], "outcome": pos["outcome"], "shares": pos["shares"],
                                "buy_gross_usd": round(pos["buy_gross"], 6), "buy_fees_usd": round(pos["buy_fees"], 6),
                                "sell_gross_usd": round(pos["sell_gross"], 6), "sell_fees_usd": round(pos["sell_fees"], 6)}},
                "rules": {"maximumBuyCashUsd": MAX_BUY, "targetVolumeUsd": TARGET, "evaluationWindowHours": hours,
                          "onePositionAtATime": True, "buyFeesIncludedInMaximum": True},
                "reference": {"btcMidUsd": r["btc"], "openingTargetUsd": m["open"]["usd"], "observedAt": now - 0.2,
                              "targetObservedAt": m["open"]["observedAt"], "targetProvisional": False},
                "books": {k: {"bids": v.get("bids") or [], "asks": v.get("asks") or [],
                              "minOrderSize": v.get("minOrderSize") or 5} for k, v in books.items() if k in ("YES", "NO")},
            }
            if now - m["open"]["observedAt"] < 0:
                obs["reference"]["targetProvisional"] = True
            try:
                d = bot.decide(obs)
            except Exception:
                d = None
            if not isinstance(d, dict) or d.get("action") not in ("HOLD", "BUY", "SELL"):
                invalid += 1
                continue
            if d["action"] == "BUY":
                if pos is not None or d.get("outcome") not in ("YES", "NO") or not (0 < float(d.get("maxCashUsd") or 0) <= MAX_BUY):
                    invalid += 1
                    continue
                pending = (d, now)
            elif d["action"] == "SELL":
                if pos is None:
                    invalid += 1
                    continue
                pending = (d, now)
        if crossed_at:
            break
        pending = None
        if pos is not None:                       # settlement: pays $1/share to the winner, adds no volume
            if pos["outcome"] == m["winner"]:
                cash += pos["shares"]
            settled += 1
            log.append({"t": m["end"], "market": m["slug"], "side": pos["outcome"], "pnl": None, "volume": 0,
                        "settled": pos["outcome"] == m["winner"]})
            pos = None
            peak = max(peak, cash)
            dd = max(dd, peak - cash)
    wins = [x for x in log if x.get("pnl") is not None and x["pnl"] > 0]
    closed = [x for x in log if x.get("pnl") is not None]
    return Result(qualified=crossed_at is not None, cash=round(cash, 4), volume=round(volume, 2), cycles=cycles,
                  max_drawdown=round(dd, 4), hours_to_target=round((crossed_at - t0) / 3600, 2) if crossed_at else None,
                  invalid=invalid, rejected=rejected, settled=settled, markets=len(markets),
                  win_rate=round(len(wins) / len(closed), 3) if closed else None, log=log)


def summary(res):
    return (f"{'QUALIFIED' if res['qualified'] else 'not qualified'} | cash ${res['cash']:.2f} | volume ${res['volume']:.0f} | "
            f"cycles {res['cycles']} | win rate {res['win_rate']} | max DD ${res['max_drawdown']:.2f} | "
            f"hours {res['hours_to_target']} | settled {res['settled']} | invalid {res['invalid']} | rejected {res['rejected']}")
