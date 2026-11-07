#!/usr/bin/env python3
"""Fair-value volume bot for the YeNo x Builderr Volume Bot Challenge (paper trading only).

Idea: near the close of a BTC five-minute Up/Down market, the chance that the current leader wins can be
computed from how far BTC is from the opening target and how volatile BTC has been:

    P(Up wins) = Phi( (BTC - target) / (sigma * sqrt(seconds left)) )

When the order book still offers the likely winner below that fair value by more than the cost of the round
trip (both fee layers + spread), the bot buys it and sells as the price converges, or exits early if the
signal weakens. Fees are smallest near prices of 0.9-0.99, which is where it trades.

Volume pacing: the bot tracks progress toward $1,000 against the 24-hour clock. Behind schedule, it accepts
smaller edges; ahead of schedule, it only takes the best setups, which keeps more cash (the ranking metric).
Standard library only. POST /decide on 127.0.0.1:8080 (HOST / PORT environment variables override).
"""
from __future__ import annotations

import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULTS = {
    "entry_max_seconds": 240.0,      # only enter in the last N seconds of a market
    "entry_min_seconds": 17.0,       # entries are blocked inside 15 s; keep a margin for the 250 ms delay
    "min_fair": 0.70,                # the side we buy must be at least this likely to win
    "max_ask": 0.97,                 # leave room for the price to rise
    "min_ask": 0.55,                 # fees are high near 0.5
    "edge": 0.03,                    # fair value minus all-in cost per share, at par with the schedule
    "edge_floor": -0.02,             # behind schedule, accept a small expected loss per share to buy volume
    "edge_cap": 0.09,                # never demand more than this, however far ahead
    "pace_gain": 0.10,               # how strongly being behind/ahead schedule moves the required edge
    "pace_hours": 22.0,              # aim to reach $1,000 by this hour (leaves a buffer inside 24 h)
    "max_round_trip_loss": 0.35,     # skip if selling straight back would lose more than this (USD)
    "take_profit_gap": 0.03,         # sell once the bid is within this of fair value (price has converged)
    "take_profit_price": 0.985,      # ... or once the bid reaches this price
    "stop_fair": 0.50,               # sell if the bought side's fair value falls below this
    "stop_loss_usd": 1.50,           # ... or the round trip would lose more than this
    "exit_seconds": 6.0,             # always be out with this many seconds left
    "cooldown": 1.0,                 # seconds between cycles
    "max_cycles_per_market": 4,
    "sigma_window": 600.0,           # seconds of BTC history used for volatility
    "sigma_floor": 2.0,              # USD per sqrt(second); protects against a too-quiet estimate
    "sigma_default": 6.0,
    "sigma_mult": 1.15,              # widen the estimate a little: real moves have fat tails
    "max_ref_age": 1.5,              # reject BTC reference older than this (seconds)
    "min_depth_share": 1.0,          # the best levels must hold our whole order
    "budget_margin": 0.995,          # spend slightly less than the cap so rounding never breaches $5
    "filler_min_fair": 0.0,          # >0: trades at a below-par edge need a side at least this likely (off by default)
    "low_cash": 0.0,                 # below this cash, require extra edge (off by default: it froze the bot)
    "low_cash_extra_edge": 0.02,
    "low_cash_stop_loss_usd": 0.6,    # in the low-cash zone, cut losers sooner (a stall below ~$5 ends the run)
}

PROTOCOL_RATE, OVERLAY = 0.07, 0.01


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def levels(rows: Any, reverse: bool) -> list[tuple[float, float]]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            p, q = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < p < 1 and q > 0:
            out.append((p, q))
    return sorted(out, reverse=reverse)


def fees(fills: list[tuple[float, float]]) -> float:
    gross = sum(p * q for p, q in fills)
    protocol = sum(PROTOCOL_RATE * q * p * (1 - p) for p, q in fills)
    return round(protocol + 1e-12, 5) + round(gross * OVERLAY + 1e-12, 5)


def buy_plan(asks, budget):
    """Shares we can buy for `budget` incl. fees, walking the asks: (shares, gross, fees) or None."""
    fills, spent = [], 0.0
    for p, q in levels(asks, reverse=False):
        unit = p * (1 + OVERLAY) + PROTOCOL_RATE * p * (1 - p)
        take = min(q, math.floor((budget - spent) / unit * 100) / 100)
        if take <= 0:
            break
        fills.append((p, take))
        spent = sum(a * b for a, b in fills) + fees(fills)
        if take < q:
            break
    while fills and sum(a * b for a, b in fills) + fees(fills) > budget:
        p, q = fills[-1]
        fills[-1] = (p, round(q - 0.01, 2))
        if fills[-1][1] <= 0:
            fills.pop()
    if not fills:
        return None
    return sum(q for _, q in fills), sum(p * q for p, q in fills), fees(fills)


def sell_value(bids, shares):
    """(gross, fees) for selling `shares` into the bids, or None if the book cannot take them all."""
    fills, left = [], shares
    for p, q in levels(bids, reverse=True):
        take = min(q, left)
        fills.append((p, take))
        left -= take
        if left <= 1e-9:
            return sum(a * b for a, b in fills), fees(fills)
    return None


class Bot:
    def __init__(self, **params: float) -> None:
        self.p = dict(DEFAULTS, **params)
        self.first_seen: float | None = None
        self.history: list[tuple[float, float]] = []
        self.market_id: str | None = None
        self.cycles_here = 0
        self.had_position = False
        self.last_flat = -1e18
        self.last_buy = -1e18

    # ---- volatility -------------------------------------------------------------------------------------------
    def _observe(self, t: float, px: float) -> None:
        if not self.history or t - self.history[-1][0] >= 1.0:
            self.history.append((t, px))
            cut = t - self.p["sigma_window"]
            while len(self.history) > 2 and self.history[0][0] < cut:
                self.history.pop(0)

    def sigma(self) -> float:
        h = self.history
        if len(h) < 30 or h[-1][0] - h[0][0] < 60:
            return self.p["sigma_default"]
        var = sum((b[1] - a[1]) ** 2 for a, b in zip(h, h[1:]))
        span = h[-1][0] - h[0][0]
        return max(self.p["sigma_floor"], math.sqrt(var / span) * self.p["sigma_mult"])

    # ---- decision ---------------------------------------------------------------------------------------------
    def decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(obs)
        except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
            return {"action": "HOLD"}

    def _decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        P = self.p
        now = float(obs["timestamp"])
        market, account, books = obs["market"], obs["account"], obs["books"]
        secs = float(market["secondsToClose"])
        pos = account.get("position")
        cash = float(account["cashUsd"])
        volume = float(account.get("eligibleVolumeUsd") or 0)
        rules = obs.get("rules") or {}
        target = float(rules.get("targetVolumeUsd") or 1000)
        cap = float(rules.get("maximumBuyCashUsd") or 5)
        if self.first_seen is None:
            self.first_seen = now

        if market["id"] != self.market_id:
            self.market_id, self.cycles_here = market["id"], 0
        if pos is None and self.had_position:
            self.cycles_here += 1
            self.last_flat = now
        self.had_position = pos is not None

        ref = obs.get("reference") if isinstance(obs.get("reference"), dict) else None
        fair_yes = None
        if ref and not ref.get("targetProvisional", True) and float(ref.get("targetObservedAt", 1e18)) <= now + 1e-9:
            btc, open_px = float(ref["btcMidUsd"]), float(ref["openingTargetUsd"])
            if now - float(ref.get("observedAt", -1e18)) <= P["max_ref_age"]:
                self._observe(float(ref.get("observedAt", now)), btc)
                fair_yes = phi((btc - open_px) / (self.sigma() * math.sqrt(max(secs, 0.5))))

        # ---- manage an open position ----
        if pos is not None:
            side = str(pos["outcome"])
            shares = float(pos["shares"])
            sv = sell_value(books.get(side, {}).get("bids"), shares)
            if sv is None:
                return {"action": "HOLD"} if secs > 1.0 else {"action": "SELL"}
            gross, fee = sv
            paid = float(pos["buy_gross_usd"]) + float(pos["buy_fees_usd"])
            got = float(pos.get("sell_gross_usd", 0)) - float(pos.get("sell_fees_usd", 0))
            pnl = got + gross - fee - paid
            best_bid = levels(books.get(side, {}).get("bids"), reverse=True)[0][0]
            fair = (fair_yes if side == "YES" else 1 - fair_yes) if fair_yes is not None else None
            if secs <= P["exit_seconds"]:
                return {"action": "SELL"}
            if best_bid >= P["take_profit_price"]:
                return {"action": "SELL"}
            if fair is not None and best_bid >= fair - P["take_profit_gap"] and pnl > 0:
                return {"action": "SELL"}
            if fair is not None and fair < P["stop_fair"]:
                return {"action": "SELL"}
            stop = P["stop_loss_usd"]
            if paid + float(account["cashUsd"]) < P["low_cash"]:
                stop = min(stop, P["low_cash_stop_loss_usd"])
            if pnl <= -stop:
                return {"action": "SELL"}
            return {"action": "HOLD"}

        # ---- look for an entry ----
        if fair_yes is None or volume >= target:
            return {"action": "HOLD"}
        if not (P["entry_min_seconds"] < secs <= P["entry_max_seconds"]):
            return {"action": "HOLD"}
        if self.cycles_here >= P["max_cycles_per_market"]:
            return {"action": "HOLD"}
        if now - self.last_flat < P["cooldown"] or now - self.last_buy < P["cooldown"]:
            return {"action": "HOLD"}
        side = "YES" if fair_yes >= 0.5 else "NO"
        fair = fair_yes if side == "YES" else 1 - fair_yes
        if fair < P["min_fair"]:
            return {"action": "HOLD"}
        book = books.get(side, {})
        asks = levels(book.get("asks"), reverse=False)
        if not asks or not (P["min_ask"] <= asks[0][0] <= P["max_ask"]):
            return {"action": "HOLD"}
        budget = min(cap, cash) * P["budget_margin"]
        plan = buy_plan(book.get("asks"), budget)
        if plan is None:
            return {"action": "HOLD"}
        shares, gross, fee = plan
        if shares < float(book.get("minOrderSize") or 5) + 0.05:
            return {"action": "HOLD"}
        # cost of getting out again: fees at the price we expect to sell near (fair, capped at 0.99)
        exit_px = min(0.99, fair)
        exit_fee = PROTOCOL_RATE * exit_px * (1 - exit_px) + OVERLAY * exit_px
        all_in = (gross + fee) / shares + exit_fee
        edge = fair - all_in
        need = self._required_edge(now, volume, target, cash)
        if edge < need:
            return {"action": "HOLD"}
        if edge < P["edge"] and fair < P["filler_min_fair"]:
            return {"action": "HOLD"}           # below-par edges only on near-certain sides, where reversals are rare
        back = sell_value(book.get("bids"), shares)
        if back is None or (back[0] - back[1]) - (gross + fee) < -P["max_round_trip_loss"]:
            return {"action": "HOLD"}
        self.last_buy = now
        return {"action": "BUY", "outcome": side, "maxCashUsd": round(min(budget, gross + fee + 0.0001), 6)}

    def _required_edge(self, now, volume, target, cash):
        P = self.p
        elapsed_frac = (now - (self.first_seen or now)) / (P["pace_hours"] * 3600)
        behind = min(1.0, elapsed_frac) - volume / target          # > 0: behind schedule
        need = P["edge"] - P["pace_gain"] * behind
        if cash < P["low_cash"]:
            need += P["low_cash_extra_edge"]
        return max(P["edge_floor"], min(P["edge_cap"], need))


BOT = Bot()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.dumps(BOT.decide(json.loads(self.rfile.read(length))), separators=(",", ":")).encode()
        except Exception:
            body = b'{"action":"HOLD"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
