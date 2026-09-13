#!/usr/bin/env python
"""
Does our own selection beat the third-party screener for this strategy?

A screener is only worth having if the stocks it picks trade better than the
ones you would otherwise have watched. So this runs both selections over the
same days, through the same engine, with the same position limits, and puts
the numbers side by side.

    python validate_universe.py --days 2026-08-13:2026-08-28

Benchmark: the TOP_MOMENTUM list recorded between 12:00 and 12:45, kept in the
candle cache from before the third-party API was dropped.

Ours: build_universe's shortlist, ranked at a cut-off strictly before the
opening range forms, so it uses nothing that was unavailable at the time.

Both are then scored the way a person would actually trade them -- a few
trades a day, first come first served, median over many same-bar orderings,
because which of two simultaneous signals you happen to take is luck.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg import backtest as bt
from orbfvg import universe as U
from orbfvg.feed import MarketData
from orbfvg.strategy import Bar

IST = U.IST
CACHE = os.path.join(config.DATA_DIR, "validate_candles.pkl")


def load_cache():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            pass
    return {"candles": {}, "meta": {}}


def save_cache(store):
    tmp = CACHE + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(store, fh)
    os.replace(tmp, CACHE)


def to_bars(rows):
    return [Bar(time=datetime.fromisoformat(t), open=o, high=h, low=l,
                close=c, volume=v) for t, o, h, l, c, v in rows]


def ensure_candles(symbols, start, end, store, feed):
    """Fetch anything not already cached, reusing the old cache where possible."""
    legacy = {}
    legacy_path = os.path.join(config.DATA_DIR, "candles_2026-08-01_2026-08-31.pkl")
    if os.path.exists(legacy_path):
        with open(legacy_path, "rb") as fh:
            old = pickle.load(fh)
        legacy = old.get("candles", {})
        legacy_meta = old.get("meta", {})
    else:
        legacy_meta = {}

    missing = [s for s in symbols if s not in store["candles"]]
    for n, symbol in enumerate(missing, 1):
        if symbol in legacy:
            store["candles"][symbol] = legacy[symbol]
            store["meta"][symbol] = legacy_meta.get(symbol, {"tick": 0.05})
            continue
        try:
            inst = feed.instrument(symbol, "NSE")
            bars = feed.candles("NSE", symbol, "FIVE_MINUTE", start, end, tz=IST)
        except Exception as exc:
            print("    %s: %s" % (symbol, str(exc)[:60]))
            continue
        if not bars:
            continue
        store["candles"][symbol] = [
            (b.time.isoformat(), b.open, b.high, b.low, b.close, b.volume)
            for b in bars]
        store["meta"][symbol] = {"tick": inst.tick_size}
        if n % 25 == 0:
            print("    fetched %d/%d" % (n, len(missing)), flush=True)
    return store


def trades_for(selection, store, settings):
    """selection: {day: [symbols]} -> every qualifying trade."""
    out = []
    for day, symbols in selection.items():
        for symbol in symbols:
            rows = store["candles"].get(symbol)
            if not rows:
                continue
            settings.mintick = store["meta"].get(symbol, {}).get("tick", 0.05)
            for t in bt.run(to_bars(rows), settings).trades:
                if t.entry_time.strftime("%Y-%m-%d") == day and t.exit == t.exit:
                    t.symbol = symbol
                    out.append(t)
    return out


def score(trades, label, caps=(2, 3), draws=101):
    from portfolio import simulate, summarise

    full = summarise(trades)
    print("\n  %s" % label)
    print("    every signal      %3d trades  %+7.2fR  %+6.2f%%  win %.1f%%"
          % (full["n"], full["r"], full["p"], full["win"]))
    results = {}
    for cap in caps:
        draw = []
        for seed in range(draws):
            taken, _ = simulate(trades, cap, cap, tiebreak="random", seed=seed)
            s = summarise(taken)
            draw.append((s["r"], s["p"], s["n"], s["win"]))
        draw.sort()
        mid = draw[len(draw) // 2]
        results[cap] = mid
        print("    cap %d/day median  %3d trades  %+7.2fR  %+6.2f%%  win %.1f%%"
              % (cap, mid[2], mid[0], mid[1], mid[3]))
    return full, results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", default="2026-08-13:2026-08-28")
    p.add_argument("--cutoff", default="12:25")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--pool-limit", type=int, default=0)
    p.add_argument("--min-rel-volume", type=float, default=1.0)
    p.add_argument("--min-move", type=float, default=1.0)
    p.add_argument("--rate", type=float, default=15.0)
    p.add_argument("--universe", default=None,
                   help="daily universe file, ideally built as-of before the window")
    args = p.parse_args()

    first, last = args.days.split(":")
    d0 = datetime.strptime(first, "%Y-%m-%d").date()
    d1 = datetime.strptime(last, "%Y-%m-%d").date()

    # -- benchmark selection, straight from the old cache -------------------
    legacy_path = os.path.join(config.DATA_DIR, "candles_2026-08-01_2026-08-31.pkl")
    if not os.path.exists(legacy_path):
        print("No legacy cache, so there is nothing to compare against.")
        return 1
    with open(legacy_path, "rb") as fh:
        legacy = pickle.load(fh)
    benchmark = defaultdict(list)
    for symbol, days in (legacy.get("selection") or {}).items():
        for day in days:
            if first <= day <= last:
                benchmark[day].append(symbol)

    # -- our selection ------------------------------------------------------
    stats, built_at = U.load_daily(args.universe or U.DAILY_CACHE)
    if not stats:
        print("No daily universe. Run:  python build_universe.py daily")
        return 1
    pool = U.eligible(stats)
    if args.pool_limit:
        pool = pool[:args.pool_limit]
    print("universe as-of %s -- %d symbols, %d eligible, scanning %d"
          % ((built_at or "?")[:10], len(stats), len(U.eligible(stats)), len(pool)))

    days = []
    day = d0
    while day <= d1:
        key = day.strftime("%Y-%m-%d")
        if day.weekday() < 5 and key in benchmark:
            days.append(key)
        day += timedelta(days=1)

    def progress(done, total, _):
        print("  ranked %d/%d symbols" % (done, total), flush=True)

    ranked = U.shortlist_many(pool, days, cutoff=args.cutoff, top_n=args.top,
                              min_rel_volume=args.min_rel_volume,
                              min_move_pct=args.min_move,
                              per_second=args.rate, progress=progress)
    ours = {d: [c.symbol for c in ranked[d]] for d in days}
    for key in days:
        print("  %s  ours %2d   benchmark %2d   overlap %2d"
              % (key, len(ours[key]), len(benchmark[key]),
                 len(set(ours[key]) & set(benchmark[key]))), flush=True)

    # -- candles for both sets ---------------------------------------------
    every = sorted({s for v in ours.values() for s in v}
                   | {s for v in benchmark.values() for s in v})
    print("\nfetching candles for %d symbols..." % len(every))
    store = ensure_candles(
        every, datetime(2026, 8, 1, tzinfo=IST), datetime(2026, 8, 31, tzinfo=IST),
        load_cache(), MarketData())
    save_cache(store)

    settings, _ = config.load()
    bench_trades = trades_for({k: v for k, v in benchmark.items()}, store, settings)
    our_trades = trades_for(ours, store, settings)

    print("\n" + "=" * 74)
    print("  Same days, same engine, same limits")
    print("=" * 74)
    score(bench_trades, "THIRD-PARTY  TOP_MOMENTUM 12:00-12:45")
    score(our_trades, "OURS  turnover/ATR filtered, ranked at %s" % args.cutoff)
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
