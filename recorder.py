#!/usr/bin/env python3
"""Record live public data for YeNo/Polymarket BTC five-minute Up/Down markets.

Reads only public, unauthenticated endpoints and never trades:
  * Polymarket Gamma API  - which market is live now (slug btc-updown-5m-<start>), its YES/NO token ids
  * Polymarket CLOB API   - the L2 order book of both outcomes, polled about every 0.5 s
  * BTC/USD spot          - Coinbase, Kraken and Bitstamp tickers (median of those that answer)

Writes one JSON line per tick to data/YYYY-MM-DD.jsonl.gz (UTC, gzip) and one line per finished market to
data/markets.jsonl (token ids, opening reference, outcome once known). These files are what sim.py replays.

    python recorder.py --check      # 20-second connectivity test
    python recorder.py              # record until stopped (Ctrl+C); leave the laptop plugged in and awake
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/events?slug={slug}"
CLOB_BOOKS = "https://clob.polymarket.com/books"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id={token}"
SPOT = {
    "coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/ticker",
                 lambda d: (float(d["bid"]) + float(d["ask"])) / 2),
    "kraken": ("https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
               lambda d: (lambda t: (float(t["b"][0]) + float(t["a"][0])) / 2)(next(iter(d["result"].values())))),
    "bitstamp": ("https://www.bitstamp.net/api/v2/ticker/btcusd/",
                 lambda d: (float(d["bid"]) + float(d["ask"])) / 2),
}
UA = {"User-Agent": "yeno-paper-recorder/1.0 (research; read-only)", "Accept": "application/json"}
PERIOD = 300


def http_json(url, data=None, timeout=4.0):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers=dict(UA, **({"Content-Type": "application/json"} if data is not None else {})),
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _listish(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return [v]
    return v or []


def find_market(start):
    """YES/NO token ids for the market that opens at `start` (unix seconds, multiple of 300)."""
    slug = f"btc-updown-5m-{start}"
    events = http_json(GAMMA.format(slug=slug))
    if isinstance(events, dict):
        events = [events]
    if not events:
        return None
    ev = events[0]
    m = (ev.get("markets") or [{}])[0]
    tokens = _listish(m.get("clobTokenIds"))
    outcomes = [str(o).lower() for o in _listish(m.get("outcomes"))]
    if len(tokens) != 2:
        return None
    up = outcomes.index("up") if "up" in outcomes else 0
    return {"slug": slug, "start": start, "end": start + PERIOD, "yes": str(tokens[up]), "no": str(tokens[1 - up]),
            "condition_id": m.get("conditionId"), "min_order_size": m.get("orderMinSize"),
            "tick_size": m.get("orderPriceMinTickSize"), "event_raw": ev}


def _levels(rows):
    out = []
    for r in rows or []:
        try:
            p, s = (float(r["price"]), float(r["size"])) if isinstance(r, dict) else (float(r[0]), float(r[1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if 0 < p < 1 and s > 0:
            out.append([p, s])
    return out


def fetch_books(yes, no):
    try:
        got = http_json(CLOB_BOOKS, [{"token_id": yes}, {"token_id": no}])
        by = {str(b.get("asset_id")): b for b in got if isinstance(b, dict)}
    except Exception:
        by = {}
        for t in (yes, no):
            try:
                by[t] = http_json(CLOB_BOOK.format(token=t))
            except Exception:
                pass
    out = {}
    for side, t in (("YES", yes), ("NO", no)):
        b = by.get(t)
        if not b:
            continue
        bids = sorted(_levels(b.get("bids")), key=lambda x: -x[0])
        asks = sorted(_levels(b.get("asks")), key=lambda x: x[0])
        mos = b.get("min_order_size")
        out[side] = {"bids": bids[:15], "asks": asks[:15],
                     "minOrderSize": float(mos) if mos not in (None, "") else None,
                     "bookTs": b.get("timestamp"), "hash": b.get("hash")}
    return out


class Spot(threading.Thread):
    """Polls the spot tickers in the background; .mid() is the median of fresh quotes."""

    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval, self.quotes, self.lock = interval, {}, threading.Lock()

    def run(self):
        while True:
            for name, (url, parse) in SPOT.items():
                try:
                    v = parse(http_json(url, timeout=2.5))
                    with self.lock:
                        self.quotes[name] = (time.time(), v)
                except Exception:
                    pass
            time.sleep(self.interval)

    def mid(self, max_age=3.0):
        now = time.time()
        with self.lock:
            fresh = {k: v for k, (t, v) in self.quotes.items() if now - t <= max_age}
        return (statistics.median(fresh.values()) if fresh else None), fresh


def resolve_outcome(slug):
    try:
        ev = http_json(GAMMA.format(slug=slug))
        ev = ev[0] if isinstance(ev, list) and ev else ev
        m = (ev.get("markets") or [{}])[0]
        prices = [float(x) for x in _listish(m.get("outcomePrices"))]
        outcomes = [str(o).lower() for o in _listish(m.get("outcomes"))]
        if prices and max(prices) >= 0.99 and m.get("closed"):
            return outcomes[prices.index(max(prices))], m
    except Exception:
        pass
    return None, None


def record(out_dir, seconds=None, tick=0.5, quiet=False):
    os.makedirs(out_dir, exist_ok=True)
    spot = Spot()
    spot.start()
    t_stop = time.time() + seconds if seconds else None
    pending = []
    mfile = os.path.join(out_dir, "markets.jsonl")
    buf = []

    def flush(day):
        if buf:
            with gzip.open(os.path.join(out_dir, f"{day}.jsonl.gz"), "at", encoding="utf-8") as fh:
                fh.writelines(buf)
            buf.clear()
    try:
        return _loop(out_dir, spot, t_stop, tick, quiet, mfile, buf, flush, pending)
    finally:
        flush(time.strftime("%Y-%m-%d", time.gmtime()))


def _loop(out_dir, spot, t_stop, tick, quiet, mfile, buf, flush, pending):
    market, opening, n, buf_day = None, None, 0, None
    while t_stop is None or time.time() < t_stop:
        now = time.time()
        start = int(now) // PERIOD * PERIOD
        if market is None or market["start"] != start:
            if market:
                pending.append((market["end"] + 120, market, opening))
            try:
                market = find_market(start)
            except Exception as e:
                market = None
                if not quiet:
                    print(f"[{time.strftime('%H:%M:%S')}] market lookup failed: {e}", flush=True)
            opening = None
            if market and not quiet:
                print(f"[{time.strftime('%H:%M:%S')}] recording {market['slug']}", flush=True)
        if market is None:
            time.sleep(2)
            continue
        mid, quotes = spot.mid()
        if opening is None and mid is not None and now - market["start"] < 20:
            opening = {"usd": mid, "observedAt": now, "sources": sorted(quotes)}
        try:
            books = fetch_books(market["yes"], market["no"])
        except Exception:
            books = {}
        row = {"t": round(now, 3), "market": market["slug"], "start": market["start"], "end": market["end"],
               "btc": mid, "btcSources": {k: round(v, 2) for k, v in quotes.items()},
               "open": opening, "books": books}
        buf.append(json.dumps(row, separators=(",", ":")) + "\n")
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if len(buf) >= 60 or day != buf_day:
            flush(buf_day or day)
            buf_day = day
        n += 1
        for item in list(pending):
            due, m, op = item
            if time.time() >= due:
                outcome, raw = resolve_outcome(m["slug"])
                if outcome or time.time() > due + 1800:
                    pending.remove(item)
                    meta = {k: v for k, v in m.items() if k != "event_raw"}
                    meta.update(open=op, outcome=outcome,
                                gamma_final={k: (raw or {}).get(k) for k in ("outcomePrices", "closed", "umaResolutionStatus")},
                                event_meta=(m.get("event_raw") or {}).get("eventMetadata"))
                    with open(mfile, "a") as fh:
                        fh.write(json.dumps(meta) + "\n")
                elif item[0] == due:
                    pending[pending.index(item)] = (due + 60, m, op)
        if not quiet and n % 120 == 0:
            y = books.get("YES", {})
            print(f"[{time.strftime('%H:%M:%S')}] ticks={n} btc={mid} yes bid/ask="
                  f"{(y.get('bids') or [[None]])[0][0]}/{(y.get('asks') or [[None]])[0][0]}", flush=True)
        time.sleep(max(0.0, tick - (time.time() - now)))
    return n


def check():
    print("Checking public endpoints (read-only)...")
    ok = True
    start = int(time.time()) // PERIOD * PERIOD
    try:
        m = find_market(start)
        print(f"  Polymarket market lookup : {'OK ' + m['slug'] if m else 'NO MARKET FOUND for this window'}")
        ok &= bool(m)
    except Exception as e:
        print(f"  Polymarket market lookup : FAILED ({e})")
        m, ok = None, False
    if m:
        b = fetch_books(m["yes"], m["no"])
        good = all(b.get(s, {}).get("bids") or b.get(s, {}).get("asks") for s in ("YES", "NO"))
        print(f"  Polymarket order books   : {'OK' if good else 'EMPTY/FAILED'} "
              f"(YES best bid {(b.get('YES', {}).get('bids') or [[None]])[0][0]}, "
              f"best ask {(b.get('YES', {}).get('asks') or [[None]])[0][0]}; min order {b.get('YES', {}).get('minOrderSize')})")
        ok &= good
    for name, (url, parse) in SPOT.items():
        try:
            print(f"  BTC price {name:<9}      : OK {parse(http_json(url)):.2f}")
        except Exception as e:
            print(f"  BTC price {name:<9}      : failed ({type(e).__name__})")
    print("  Recording 20 seconds to data-check/ ...")
    n = record("data-check", seconds=20, quiet=True)
    print(f"  ticks recorded           : {n}")
    print("RESULT:", "PASS" if ok and n >= 10 else "FAIL")
    return 0 if ok and n >= 10 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default="data")
    ap.add_argument("--hours", type=float, help="stop after this many hours (default: run until Ctrl+C)")
    a = ap.parse_args()
    if a.check:
        sys.exit(check())
    try:
        record(a.out, seconds=a.hours * 3600 if a.hours else None)
    except KeyboardInterrupt:
        print("stopped; data saved")
