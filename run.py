#!/usr/bin/env python
"""
ORB + FVG on Angel One -- command line entry point.

    python run.py check                          connectivity + account sanity check
    python run.py symbols RELIANCE                find tradingsymbols and tokens
    python run.py backtest --symbol SBIN-EQ --days 30
    python run.py signals  --symbol SBIN-EQ       today's signals, no orders
    python run.py live     --symbol SBIN-EQ       dry run by default
    python run.py live     --symbol SBIN-EQ --real   place real orders

Any strategy input can be overridden without touching code:

    python run.py backtest --symbol SBIN-EQ --days 60 --set useFvg=false --set atrMult=2.0
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg import backtest as bt
from orbfvg.angel import AngelClient
from orbfvg.live import LiveRunner


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(config.LOG_DIR, "orbfvg_%s.log" % datetime.now().strftime("%Y%m%d")),
                encoding="utf-8",
            ),
        ],
    )
    logging.getLogger("SmartApi").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def coerce(current, text: str):
    """Cast an --set override to the type of the existing setting."""
    if isinstance(current, bool):
        low = text.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError("expected a boolean, got %r" % text)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def apply_overrides(settings, pairs) -> None:
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit("--set expects key=value, got %r" % pair)
        key, _, value = pair.partition("=")
        key = key.strip()
        if not hasattr(settings, key):
            raise SystemExit(
                "Unknown setting %r. Valid names: %s"
                % (key, ", ".join(sorted(vars(settings))))
            )
        setattr(settings, key, coerce(getattr(settings, key), value))


def build_settings(args):
    if getattr(args, "preset", None):
        config.PRESET = args.preset
    strategy, trade = config.load()
    apply_overrides(strategy, getattr(args, "set", None))
    strategy.validate()

    if getattr(args, "symbol", None):
        trade.symbol = args.symbol
    if getattr(args, "exchange", None):
        trade.exchange = args.exchange
    if getattr(args, "qty", None):
        trade.quantity = args.qty
    if getattr(args, "real", False):
        trade.dry_run = False
    if getattr(args, "no_broker_sl", False):
        trade.use_broker_sl = False
    trade.validate()
    return strategy, trade


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------
def cmd_check(args) -> int:
    strategy, trade = build_settings(args)
    client = AngelClient()
    profile = client.login()
    print("\n  Account    %s (%s)" % (profile.get("name"), profile.get("clientcode")))
    print("  Exchanges  %s" % ", ".join(profile.get("exchanges") or []))

    try:
        funds = client.funds()
        print("  Net funds  %s   available cash %s"
              % (funds.get("net", "?"), funds.get("availablecash", "?")))
    except Exception as exc:
        print("  Funds      unavailable (%s)" % exc)

    inst = client.instrument(trade.symbol, trade.exchange)
    print("\n  Instrument %s  token %s  tick %.2f  lot %d"
          % (inst.symbol, inst.token, inst.tick_size, inst.lotsize))
    try:
        print("  Last price %.2f" % client.ltp(trade.exchange, inst.symbol, inst.token))
    except Exception as exc:
        print("  Last price unavailable (%s)" % exc)

    print("\n  Strategy   opening range %s   signals %s   %s"
          % (strategy.orSess, strategy.sigSess, strategy.tzIn))
    print("  FVG gate   %s (lookback %d bars, min size %.2f x ATR)"
          % ("on" if strategy.useFvg else "off", strategy.fvgLB, strategy.fvgAtrX))
    print("  Stop       %s x%.2f     targets %gR / %gR / %gR"
          % (strategy.slMode, strategy.atrMult, strategy.r1, strategy.r2, strategy.r3))
    print("  Sizing     %s  qty %d  cap %d/trade"
          % (trade.sizing_mode, trade.quantity, trade.max_quantity))
    print("  Mode       %s\n" % ("DRY RUN" if trade.dry_run else "LIVE ORDERS"))
    client.logout()
    return 0


def cmd_symbols(args) -> int:
    from orbfvg.instruments import search

    matches = search(args.term, args.exchange, limit=args.limit)
    if not matches:
        print("No instrument matched %r" % args.term)
        return 1
    print("\n  %-28s %-10s %-6s %-8s %s" % ("SYMBOL", "TOKEN", "EXCH", "LOT", "TICK"))
    print("  " + "-" * 62)
    for inst in matches:
        print("  %-28s %-10s %-6s %-8d %.2f"
              % (inst.symbol, inst.token, inst.exch_seg, inst.lotsize, inst.tick_size))
    print("")
    return 0


def _load_bars(client, strategy, trade, days, date_from, date_to):
    tz = ZoneInfo(strategy.tzIn)
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1) \
        if date_to else datetime.now(tz)
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz) \
        if date_from else end - timedelta(days=days)
    inst = client.instrument(trade.symbol, trade.exchange)
    strategy.mintick = inst.tick_size
    bars = client.candles(trade.exchange, inst.token, trade.interval, start, end, tz=tz)
    return inst, bars


def cmd_backtest(args) -> int:
    strategy, trade = build_settings(args)
    client = AngelClient()
    client.login()
    inst, bars = _load_bars(client, strategy, trade, args.days, args.date_from, args.date_to)
    client.logout()

    if not bars:
        print("No candles returned for that window.")
        return 1

    result = bt.run(bars, strategy)
    print("")
    print(bt.format_report(result, inst.symbol, strategy))

    out = args.out or os.path.join(
        config.LOG_DIR, "backtest_%s_%s.csv" % (inst.symbol, datetime.now().strftime("%Y%m%d_%H%M%S"))
    )
    bt.export_trades(result, out)
    print("\n  Trades written to %s\n" % out)
    return 0


def cmd_signals(args) -> int:
    """Today's signals only -- the engine runs, nothing is ordered."""
    strategy, trade = build_settings(args)
    client = AngelClient()
    client.login()
    inst, bars = _load_bars(client, strategy, trade, 1, None, None)
    client.logout()
    if not bars:
        print("No candles returned.")
        return 1

    tz = ZoneInfo(strategy.tzIn)
    today = datetime.now(tz).date()
    result = bt.run(bars, strategy)
    todays = [e for e in result.events if e.time.date() == today]

    print("\n  %s -- %s" % (inst.symbol, today))
    print("  " + "-" * 60)
    if not todays:
        print("  No events today.")
    for event in todays:
        print("  %s  %-13s %s" % (event.time.strftime("%H:%M"), event.type.value, event.message))
    print("")
    return 0


def cmd_live(args) -> int:
    strategy, trade = build_settings(args)
    if not trade.dry_run:
        print("\n  *** LIVE MODE: real orders will be placed on %s ***" % trade.symbol)
        print("      product %s   sizing %s   qty %d   daily cap %d trades"
              % (trade.producttype, trade.sizing_mode, trade.quantity, trade.max_trades_per_day))
        if input("      Type LIVE to confirm: ").strip() != "LIVE":
            print("      Aborted.\n")
            return 1
    runner = LiveRunner(strategy, trade)
    runner.run(forever=args.forever)
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Session Opening Range + 5-minute FVG, on Angel One SmartAPI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, with_trade=True):
        p.add_argument("--symbol", help="tradingsymbol, e.g. SBIN-EQ")
        p.add_argument("--exchange", help="NSE, NFO, BSE, MCX")
        p.add_argument("--preset", choices=sorted(config.PRESETS), help="session preset")
        p.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="override any strategy input (repeatable)")
        if with_trade:
            p.add_argument("--qty", type=int, help="fixed quantity per trade")

    p = sub.add_parser("check", help="verify login, instrument and settings")
    common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("symbols", help="search the scrip master")
    p.add_argument("term")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("backtest", help="replay historical candles")
    common(p)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    p.add_argument("--out", help="CSV path for the trade list")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("signals", help="show today's signals without trading")
    common(p)
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("live", help="run the bot against the live session")
    common(p)
    p.add_argument("--real", action="store_true", help="place real orders (default: dry run)")
    p.add_argument("--forever", action="store_true", help="keep running across days")
    p.add_argument("--no-broker-sl", action="store_true",
                   help="do not park a protective SL-M order at the engine stop")
    p.set_defaults(func=cmd_live)

    args = parser.parse_args()
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
