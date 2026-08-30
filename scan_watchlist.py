#!/usr/bin/env python
"""
Backtest the strategy across a Supabase watchlist.

Picks the symbols that were sitting on a given scanner list during a chosen
time window, then runs the ORB + FVG engine on each of them and reports only
the trades taken on the day that symbol qualified.

The default window is 12:00-12:45 IST, which is the run-up to the London
opening range the strategy uses (12:30-12:45 IST). A symbol that was on the
momentum list going into that range is a candidate for that afternoon; one
that only showed up in the morning, or after the range had already broken,
is not.

    set SUPABASE_URL=https://<project>.supabase.co
    set SUPABASE_KEY=<key>
    python scan_watchlist.py --category TOP_MOMENTUM

Credentials are read from the environment; nothing is written to disk.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import config
from orbfvg import backtest as bt
from orbfvg.angel import AngelClient

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("scan")


# ---------------------------------------------------------------------------
#  Supabase
# ---------------------------------------------------------------------------
def fetch_watchlist(url: str, key: str, category: str, table: str = "watchlist_snapshots"):
    """Page through the snapshot table for one category."""
    headers = {"apikey": key, "Authorization": "Bearer " + key}
    query = (
        "%s/rest/v1/%s?select=date,time,symbol,ltp,price_change_pct"
        "&category=eq.%s&order=id.asc" % (url.rstrip("/"), table, category)
    )
    rows, offset, page = [], 0, 1000
    while True:
        h = dict(headers, Range="%d-%d" % (offset, offset + page - 1))
        response = requests.get(query, headers=h, timeout=60)
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return rows


def qualifying(rows, start_time: str, end_time: str):
    """symbol -> set of dates it was listed inside the time window."""
    by_symbol = defaultdict(set)
    for r in rows:
        t = (r.get("time") or "")[:5]
        if start_time <= t <= end_time:
            by_symbol[r["symbol"]].add(r["date"])
    return by_symbol


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--category", default="TOP_MOMENTUM")
    p.add_argument("--from-time", default="12:00", help="window start, IST HH:MM")
    p.add_argument("--to-time", default="12:45", help="window end, IST HH:MM")
    p.add_argument("--history-from", default=None, help="candle start YYYY-MM-DD")
    p.add_argument("--history-to", default=None, help="candle end YYYY-MM-DD")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override a strategy input (repeatable)")
    p.add_argument("--out", default=None, help="CSV path for the trade list")
    p.add_argument("--table", default="watchlist_snapshots")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("SmartApi").setLevel(logging.CRITICAL)

    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_KEY in the environment.")
        return 2

    # -- who was on the list, and when ------------------------------------
    rows = fetch_watchlist(url, key, args.category, args.table)
    by_symbol = qualifying(rows, args.from_time, args.to_time)
    if not by_symbol:
        print("No %s entries between %s and %s." % (args.category, args.from_time, args.to_time))
        return 1

    all_dates = sorted({d for ds in by_symbol.values() for d in ds})
    symbol_days = sum(len(v) for v in by_symbol.values())
    print("%s listed between %s and %s IST" % (args.category, args.from_time, args.to_time))
    print("  %d symbols, %d symbol-days, %s -> %s"
          % (len(by_symbol), symbol_days, all_dates[0], all_dates[-1]))

    hist_from = args.history_from or (
        datetime.strptime(all_dates[0], "%Y-%m-%d") - timedelta(days=12)).strftime("%Y-%m-%d")
    hist_to = args.history_to or (
        datetime.strptime(all_dates[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    start = datetime.strptime(hist_from, "%Y-%m-%d").replace(tzinfo=IST)
    end = datetime.strptime(hist_to, "%Y-%m-%d").replace(tzinfo=IST)
    print("  candles %s -> %s (extra history warms up ATR)\n" % (hist_from, hist_to))

    # -- strategy settings -------------------------------------------------
    strategy, _ = config.load()
    for pair in args.set or []:
        k, _, v = pair.partition("=")
        cur = getattr(strategy, k.strip())
        if isinstance(cur, bool):
            v = v.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int) and not isinstance(cur, bool):
            v = int(v)
        elif isinstance(cur, float):
            v = float(v)
        setattr(strategy, k.strip(), v)
    strategy.validate()
    print("  preset %s %s / %s   trail %s   square-off %s\n"
          % (strategy.tzIn, strategy.orSess, strategy.sigSess,
             strategy.trailMode if strategy.useTrail else "off",
             strategy.sqOffTime or "off"))

    # -- run ---------------------------------------------------------------
    client = AngelClient()
    client.login()
    trades, unresolved, nodata = [], [], []

    for n, symbol in enumerate(sorted(by_symbol), 1):
        dates = by_symbol[symbol]
        try:
            inst = client.instrument(symbol, args.exchange)
        except LookupError:
            unresolved.append(symbol)
            continue
        try:
            bars = client.candles(args.exchange, inst.token, "FIVE_MINUTE", start, end, tz=IST)
        except Exception as exc:
            log.warning("  %s candle fetch failed: %s", symbol, exc)
            nodata.append(symbol)
            continue
        if not bars:
            nodata.append(symbol)
            continue

        strategy.mintick = inst.tick_size
        result = bt.run(bars, strategy)
        for t in result.trades:
            if t.entry_time.strftime("%Y-%m-%d") in dates:
                t.symbol = symbol
                trades.append(t)
        if n % 25 == 0:
            print("  ... %d/%d symbols scanned" % (n, len(by_symbol)))

    client.logout()
    report(trades, unresolved, nodata, args, strategy)
    return 0


# ---------------------------------------------------------------------------
def report(trades, unresolved, nodata, args, strategy) -> None:
    closed = [t for t in trades if t.exit == t.exit]
    print("\n" + "=" * 78)
    print("  %s  %s-%s IST" % (args.category, args.from_time, args.to_time))
    print("=" * 78)
    if unresolved:
        print("  not on %s: %s" % (args.exchange, ", ".join(sorted(unresolved)[:15])))
    if nodata:
        print("  no candles: %s" % ", ".join(sorted(nodata)[:15]))
    if not closed:
        print("  No trades were taken.")
        return

    rs = [t.r_multiple for t in closed if t.r_multiple == t.r_multiple]
    wins = [r for r in rs if r > 0.01]
    losses = [r for r in rs if r < -0.01]
    scratches = [r for r in rs if -0.01 <= r <= 0.01]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    decided = len(wins) + len(losses)

    print("  Trades        %d   (%d long / %d short)"
          % (len(closed), sum(1 for t in closed if t.side == "BUY"),
             sum(1 for t in closed if t.side == "SELL")))
    print("  Win rate      %.1f%%   (%d win / %d loss / %d scratch)"
          % (100.0 * len(wins) / decided if decided else 0.0,
             len(wins), len(losses), len(scratches)))
    print("  Total R       %+.2f      Average %+.2fR" % (sum(rs), sum(rs) / len(rs)))
    print("  Profit factor %s" % ("inf" if gross_l == 0 else "%.2f" % (gross_w / gross_l)))
    print("  Best / worst  %+.2fR / %+.2fR" % (max(rs), min(rs)))
    exits = defaultdict(int)
    for t in closed:
        exits[t.reason] += 1
    print("  Exits         %s" % "   ".join("%s %d" % (k, v) for k, v in sorted(exits.items())))

    # per-day
    by_day = defaultdict(list)
    for t in closed:
        by_day[t.entry_time.strftime("%Y-%m-%d")].append(t)
    print("\n  %-12s %7s %9s %s" % ("DATE", "TRADES", "R", "SYMBOLS"))
    print("  " + "-" * 74)
    for day in sorted(by_day):
        ts = by_day[day]
        print("  %-12s %7d %+9.2f %s"
              % (day, len(ts), sum(t.r_multiple for t in ts),
                 " ".join(t.symbol for t in ts)[:44]))

    print("\n  %-14s %-16s %-5s %9s %9s %9s %-12s %7s"
          % ("SYMBOL", "ENTRY", "SIDE", "PRICE", "STOP", "EXIT", "REASON", "R"))
    print("  " + "-" * 92)
    for t in sorted(closed, key=lambda x: x.entry_time):
        print("  %-14s %-16s %-5s %9.2f %9.2f %9.2f %-12s %+7.2f"
              % (t.symbol, t.entry_time.strftime("%Y-%m-%d %H:%M"), t.side,
                 t.entry, t.stop, t.exit, t.reason, t.r_multiple))

    out = args.out or os.path.join(
        config.LOG_DIR, "watchlist_%s_%s.csv"
        % (args.category, datetime.now().strftime("%Y%m%d_%H%M%S")))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "side", "entry_time", "entry", "stop", "t1", "t2", "t3",
                    "exit_time", "exit", "reason", "targets_hit", "r_multiple", "points"])
        for t in sorted(closed, key=lambda x: x.entry_time):
            w.writerow([t.symbol, t.side, t.entry_time.strftime("%Y-%m-%d %H:%M"),
                        "%.2f" % t.entry, "%.2f" % t.stop, "%.2f" % t.t1,
                        "%.2f" % t.t2, "%.2f" % t.t3,
                        t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                        "%.2f" % t.exit, t.reason, t.targets_hit,
                        "%.3f" % t.r_multiple, "%.2f" % t.points])
    print("\n  Trades written to %s\n" % out)


if __name__ == "__main__":
    sys.exit(main())
