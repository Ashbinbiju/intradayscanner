#!/usr/bin/env python
"""
Replay the watchlist as one trader would actually work it.

The per-symbol backtests answer "would this setup have worked on this stock".
They do not answer "would I have made money", because they quietly assume you
held every signal at once -- 13 open positions on one afternoon, in one case.

This walks every signal in chronological order across the whole watchlist and
takes them the way a person would: a limited number of positions at a time, a
limited number of trades in a day, first come first served. Everything that
fires while you are full is skipped and recorded as skipped.

    python portfolio.py --max-concurrent 3 --max-per-day 3

Ties matter: when several symbols trigger on the same 5-minute bar you can only
pick some of them, and the backtest has to pick somehow. The default is
alphabetical, which is arbitrary but reproducible; --tiebreak shows how much
that choice is worth.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg import backtest as bt
from sweep import GRID, COMBOS, load_cache, to_bars


def all_trades(store, bars_by_symbol, overrides):
    """Every qualifying trade across the universe, with its symbol attached."""
    settings, _ = config.load()
    for k, v in overrides.items():
        setattr(settings, k, v)
    settings.validate()

    out = []
    for symbol, bars in bars_by_symbol.items():
        dates = set(store["selection"].get(symbol, ()))
        if not dates:
            continue
        settings.mintick = store["meta"].get(symbol, {}).get("tick", 0.05)
        for t in bt.run(bars, settings).trades:
            if t.entry_time.strftime("%Y-%m-%d") in dates and t.exit == t.exit:
                t.symbol = symbol
                out.append(t)
    return out


def simulate(trades, max_concurrent=3, max_per_day=3, tiebreak="symbol", seed=0):
    """Walk the signals in time order, honouring the position and daily caps."""
    if tiebreak == "random":
        rng = random.Random(seed)
        keyed = [(t.entry_time, rng.random(), t) for t in trades]
    else:
        keyed = [(t.entry_time, t.symbol, t) for t in trades]
    keyed.sort(key=lambda x: (x[0], x[1]))

    open_until = []          # exit times of positions still live
    taken, skipped = [], []
    per_day = defaultdict(int)

    for entry_time, _, t in keyed:
        open_until = [x for x in open_until if x > entry_time]
        day = entry_time.strftime("%Y-%m-%d")
        if per_day[day] >= max_per_day:
            skipped.append((t, "daily cap"))
            continue
        if len(open_until) >= max_concurrent:
            skipped.append((t, "all slots busy"))
            continue
        taken.append(t)
        per_day[day] += 1
        open_until.append(t.exit_time)
    return taken, skipped


def pct(trades):
    return sum((t.exit - t.entry) * (1 if t.side == "BUY" else -1) / t.entry * 100
               for t in trades)


def summarise(trades):
    rs = [t.r_multiple for t in trades if t.r_multiple == t.r_multiple]
    if not rs:
        return dict(n=0, r=0.0, p=0.0, win=0.0, avg=0.0)
    wins = [r for r in rs if r > 0.01]
    losses = [r for r in rs if r < -0.01]
    decided = len(wins) + len(losses)
    return dict(n=len(rs), r=sum(rs), p=pct(trades), avg=sum(rs) / len(rs),
                win=100.0 * len(wins) / decided if decided else 0.0)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default=None)
    p.add_argument("--max-concurrent", type=int, default=3)
    p.add_argument("--max-per-day", type=int, default=3)
    p.add_argument("--config", default=None, help="label from sweep.py's grid")
    p.add_argument("--tiebreak", action="store_true",
                   help="re-run with random same-bar ordering to size that effect")
    p.add_argument("--detail", action="store_true", help="list every taken trade")
    p.add_argument("--draws", type=int, default=101, help="random orderings to average over")
    args = p.parse_args()

    store, path = load_cache(args.cache)
    bars_by_symbol = {s: to_bars(r) for s, r in store["candles"].items()}
    print("cache %s -- %d symbols\n" % (os.path.basename(path), len(bars_by_symbol)))

    grid = {l: o for l, o in GRID + COMBOS}
    configs = ([(args.config, grid[args.config])] if args.config else
               [("as-is indicator", {"atrMult": 1.5, "strongClose": 0.0}),
                ("shipped (atr3.0 + strong0.5)", {})])

    for label, overrides in configs:
        trades = all_trades(store, bars_by_symbol, overrides)
        print("=" * 78)
        print("  %s" % label)
        print("=" * 78)
        every = summarise(trades)
        print("  hold-everything    %3d trades  %+7.2fR  %+6.2f%%  win %.1f%%"
              % (every["n"], every["r"], every["p"], every["win"]))

        # Several symbols routinely trigger on the same 5-minute bar, and you
        # cannot take them all. Which one you happen to click is luck, so the
        # honest headline is the median over many orderings, not one of them.
        print("\n  %-22s %7s %9s %9s %9s %8s"
              % ("CAP (concurrent/day)", "TRADES", "MEDIAN R", "WORST R", "BEST R", "MED %"))
        for cap in (1, 2, 3, 4):
            draws = []
            for seed in range(args.draws):
                tk, _ = simulate(trades, cap, cap, tiebreak="random", seed=seed)
                s = summarise(tk)
                draws.append((s["r"], s["p"], s["n"], s["win"]))
            draws.sort()
            mid = draws[len(draws) // 2]
            print("  %-22s %7d %+9.2f %+9.2f %+9.2f %+8.2f"
                  % ("%d / %d" % (cap, cap), mid[2], mid[0], draws[0][0],
                     draws[-1][0], mid[1]))

        taken, skipped = simulate(trades, args.max_concurrent, args.max_per_day)
        by_day = defaultdict(list)
        for t in taken:
            by_day[t.entry_time.strftime("%Y-%m-%d")].append(t)
        print("\n  per day at max %d concurrent / %d per day:"
              % (args.max_concurrent, args.max_per_day))
        run_r = 0.0
        for day in sorted(by_day):
            ts = by_day[day]
            day_r = sum(t.r_multiple for t in ts)
            run_r += day_r
            print("    %s  %d trades  %+6.2fR  (running %+6.2f)   %s"
                  % (day, len(ts), day_r, run_r,
                     " ".join("%s%+.1f" % (t.symbol[:9], t.r_multiple) for t in ts)))

        if args.tiebreak:
            results = []
            for seed in range(25):
                tk, _ = simulate(trades, args.max_concurrent, args.max_per_day,
                                 tiebreak="random", seed=seed)
                results.append(summarise(tk)["r"])
            results.sort()
            print("\n  same-bar ordering sensitivity (25 random draws):"
                  "  worst %+.2fR   median %+.2fR   best %+.2fR"
                  % (results[0], results[len(results) // 2], results[-1]))

        if args.detail:
            print("\n  %-12s %-16s %-5s %10s %10s %-12s %7s"
                  % ("SYMBOL", "ENTRY", "SIDE", "PRICE", "EXIT", "REASON", "R"))
            for t in taken:
                print("  %-12s %-16s %-5s %10.2f %10.2f %-12s %+7.2f"
                      % (t.symbol, t.entry_time.strftime("%Y-%m-%d %H:%M"), t.side,
                         t.entry, t.exit, t.reason, t.r_multiple))
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
