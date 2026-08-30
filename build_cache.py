#!/usr/bin/env python
"""
Fetch candles once and cache them, so parameter sweeps run offline.

Angel rate-limits historical data to 3 requests/second, which makes a
multi-config sweep across 160 symbols painfully slow if every run re-fetches.
This pulls each symbol once into data/candles_<from>_<to>.pkl; sweep.py then
loads that and tests as many configurations as you like at full speed.

    python build_cache.py --day 2026-08-28 --from-time 12:00 --to-time 12:45
    python build_cache.py --symbols NEWGEN,COFORGE,TEJASNET
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg.angel import AngelClient
from orbfvg import screener

IST = ZoneInfo("Asia/Kolkata")


def cache_path(hist_from: str, hist_to: str) -> str:
    return os.path.join(config.DATA_DIR, "candles_%s_%s.pkl" % (hist_from, hist_to))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None,
                   help="recorded snapshot day to take the universe from")
    p.add_argument("--symbols", default="", help="explicit comma-separated list")
    p.add_argument("--buckets", default=",".join(screener.BULLISH),
                   help="screener lists to draw from")
    p.add_argument("--from-time", default="12:00")
    p.add_argument("--to-time", default="12:45")
    p.add_argument("--history-from", default="2026-08-01")
    p.add_argument("--history-to", default="2026-08-31")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--extra", default="", help="comma-separated extra symbols")
    args = p.parse_args()

    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()]
    by_symbol = {}
    if args.symbols:
        symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        by_symbol = {s: [] for s in symbols}
    elif args.day:
        found = screener.symbols_from_history(
            config.DATA_DIR, args.day, buckets, args.from_time, args.to_time)
        by_symbol = {s: [args.day] for s in found}
        symbols = set(found)
        if not symbols:
            print("No snapshots recorded for %s. Run record_snapshot.py during "
                  "market hours to build the history." % args.day)
            return 1
    else:
        data = screener.fetch()
        symbols = set(screener.symbols(data, buckets))
        today = datetime.now(IST).strftime("%Y-%m-%d")
        by_symbol = {s: [today] for s in symbols}
        screener.save_snapshot(data, config.DATA_DIR)
    symbols = set(symbols)
    for s in filter(None, (x.strip().upper() for x in args.extra.split(","))):
        symbols.add(s)
        by_symbol.setdefault(s, set())

    start = datetime.strptime(args.history_from, "%Y-%m-%d").replace(tzinfo=IST)
    end = datetime.strptime(args.history_to, "%Y-%m-%d").replace(tzinfo=IST)
    print("caching %d symbols, %s -> %s" % (len(symbols), args.history_from, args.history_to))

    client = AngelClient()
    client.login()
    store = {"selection": {k: sorted(v) for k, v in by_symbol.items()}, "candles": {}, "meta": {}}
    missing = []

    for n, symbol in enumerate(sorted(symbols), 1):
        try:
            inst = client.instrument(symbol, args.exchange)
        except LookupError:
            missing.append(symbol)
            continue
        try:
            bars = client.candles(args.exchange, inst.token, "FIVE_MINUTE", start, end, tz=IST)
        except Exception as exc:
            print("  %s failed: %s" % (symbol, exc))
            missing.append(symbol)
            continue
        store["candles"][symbol] = [
            (b.time.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars
        ]
        store["meta"][symbol] = {"token": inst.token, "tick": inst.tick_size,
                                 "lot": inst.lotsize}
        if n % 25 == 0:
            print("  ... %d/%d" % (n, len(symbols)))

    client.logout()
    path = cache_path(args.history_from, args.history_to)
    with open(path, "wb") as fh:
        pickle.dump(store, fh)
    total = sum(len(v) for v in store["candles"].values())
    print("cached %d symbols, %d bars -> %s" % (len(store["candles"]), total, path))
    if missing:
        print("skipped: %s" % ", ".join(sorted(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
