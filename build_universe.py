#!/usr/bin/env python
"""
Build our own tradable universe, and shortlist from it.

Replaces the third-party screener with something computed from candles we
already pull. Two commands:

    python build_universe.py daily
        One year of daily bars for every NSE equity (~2,650 symbols, about two
        minutes), summarised into data/universe_daily.json. Run pre-market,
        or weekly -- liquidity and daily volatility do not move fast.

    python build_universe.py shortlist --day 2026-08-28 --cutoff 12:25
        Rank the survivors on relative volume, distance moved and position in
        the day's range, using only bars that closed before the cut-off.

`daily` needs UPSTOX_TOKEN. Nothing here places an order.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg import universe as U


def cmd_daily(args) -> int:
    def progress(done, total, kept):
        print("  %d/%d fetched, %d usable" % (done, total, kept), flush=True)

    started = datetime.now()
    as_of = (datetime.strptime(args.as_of, "%Y-%m-%d").date()
             if args.as_of else None)
    stats = U.build_daily(workers=args.workers, per_second=args.rate,
                          lookback_days=args.lookback, limit=args.limit,
                          progress=progress, as_of=as_of)
    path = U.save_daily(stats, args.out or U.DAILY_CACHE, as_of=as_of)
    took = (datetime.now() - started).total_seconds()

    filters = U.Filters(min_turnover_cr=args.min_turnover,
                        min_price=args.min_price, max_price=args.max_price,
                        min_atr_pct=args.min_atr, max_atr_pct=args.max_atr)
    keep = U.eligible(stats, filters)

    print("\n  Fetched   %d symbols in %.0fs -> %s" % (len(stats), took, path))
    print("  Eligible  %d after filters" % len(keep))
    print("            turnover >= %.1f cr, price %.0f-%.0f, ATR %.1f-%.1f%%"
          % (filters.min_turnover_cr, filters.min_price, filters.max_price,
             filters.min_atr_pct, filters.max_atr_pct))
    print("\n  %-14s %10s %10s %8s %8s" % ("SYMBOL", "CLOSE", "TURNOVER", "ATR%", "RANGE%"))
    print("  " + "-" * 56)
    for s in keep[:20]:
        print("  %-14s %10.2f %9.1fcr %7.2f%% %7.2f%%"
              % (s.symbol, s.close, s.turnover_cr, s.atr_pct, s.range_pct))
    print("  ... and %d more\n" % max(0, len(keep) - 20))
    return 0


def cmd_shortlist(args) -> int:
    stats, built_at = U.load_daily()
    if not stats:
        print("No daily universe yet. Run:  python build_universe.py daily")
        return 1

    filters = U.Filters(min_turnover_cr=args.min_turnover,
                        min_price=args.min_price, max_price=args.max_price,
                        min_atr_pct=args.min_atr, max_atr_pct=args.max_atr)
    pool = U.eligible(stats, filters)
    print("  universe built %s, %d symbols, %d eligible"
          % ((built_at or "?")[:16], len(stats), len(pool)))
    if args.pool_limit:
        pool = pool[:args.pool_limit]
        print("  capped to the %d most liquid" % len(pool))

    def progress(done, total, kept):
        print("  %d/%d scanned" % (done, total), flush=True)

    picks = U.shortlist(pool, args.day, cutoff=args.cutoff, top_n=args.top,
                        min_rel_volume=args.min_rel_volume,
                        min_move_pct=args.min_move, workers=args.workers,
                        per_second=args.rate, progress=progress)

    print("\n  %s as of %s -- %d candidates" % (args.day, args.cutoff, len(picks)))
    print("  %-13s %5s %9s %8s %7s %8s %7s %6s"
          % ("SYMBOL", "SIDE", "LTP", "MOVE%", "RELVOL", "POS", "RANGE%", "SCORE"))
    print("  " + "-" * 74)
    for c in picks:
        print("  %-13s %5s %9.2f %+7.2f%% %7.2fx %7.2f %6.2f%% %6.3f"
              % (c.symbol, c.direction, c.ltp, c.move_pct, c.rel_volume,
                 c.range_position, c.day_range_pct, c.score))

    out = args.out or os.path.join(
        config.LOG_DIR, "shortlist_%s_%s.csv" % (args.day, args.cutoff.replace(":", "")))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "direction", "ltp", "move_pct", "rel_volume",
                         "range_position", "day_range_pct", "turnover_cr",
                         "atr_pct", "score"])
        for c in picks:
            writer.writerow([c.symbol, c.direction, c.ltp, c.move_pct,
                             c.rel_volume, c.range_position, c.day_range_pct,
                             c.turnover_cr, c.atr_pct, c.score])
    print("\n  %d symbols -> %s\n" % (len(picks), out))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def filters(sp):
        sp.add_argument("--min-turnover", type=float, default=5.0,
                        help="median daily turnover, rupees crore")
        sp.add_argument("--min-price", type=float, default=50.0)
        sp.add_argument("--max-price", type=float, default=20000.0)
        sp.add_argument("--min-atr", type=float, default=1.5, help="daily ATR %%")
        sp.add_argument("--max-atr", type=float, default=12.0)
        sp.add_argument("--workers", type=int, default=6)
        sp.add_argument("--rate", type=float, default=12.0, help="requests/second")

    sp = sub.add_parser("daily", help="rebuild the daily universe")
    filters(sp)
    sp.add_argument("--lookback", type=int, default=120, help="calendar days")
    sp.add_argument("--limit", type=int, default=0, help="stop after N symbols")
    sp.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="build the universe as it looked on this date")
    sp.add_argument("--out", default=None, help="write to this path instead")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("shortlist", help="rank candidates for one day")
    filters(sp)
    sp.add_argument("--day", default=datetime.now(U.IST).strftime("%Y-%m-%d"))
    sp.add_argument("--cutoff", default="12:25",
                    help="only bars closing before this are used")
    sp.add_argument("--top", type=int, default=40)
    sp.add_argument("--min-rel-volume", type=float, default=1.0)
    sp.add_argument("--min-move", type=float, default=1.0, help="abs %% move")
    sp.add_argument("--pool-limit", type=int, default=0,
                    help="scan only the N most liquid eligible names")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_shortlist)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
