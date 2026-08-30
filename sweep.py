#!/usr/bin/env python
"""
Compare fakeout filters offline against the cached candles.

Every configuration runs over the same symbols and the same qualifying dates,
so the only thing that changes between rows is the setting under test. Build
the cache first:

    python build_cache.py --category TOP_MOMENTUM
    python sweep.py

Add `--config name` to run a single one, or `--list` to see the grid.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from orbfvg import backtest as bt
from orbfvg.strategy import Bar

IST = ZoneInfo("Asia/Kolkata")

# Each entry is (label, {setting: value}). The empty dict is the indicator as-is.
GRID = [
    ("baseline (as-is)",            {}),

    # -- wait for the level to hold ------------------------------------
    ("confirmBars=2",               {"confirmBars": 2}),
    ("confirmBars=3",               {"confirmBars": 3}),

    # -- demand conviction on the breakout bar -------------------------
    ("strongClose=0.5",             {"strongClose": 0.5}),
    ("strongClose=0.65",            {"strongClose": 0.65}),
    ("strongClose=0.8",             {"strongClose": 0.8}),
    ("volMult=1.2",                 {"volMult": 1.2}),
    ("volMult=1.5",                 {"volMult": 1.5}),
    ("volMult=2.0",                 {"volMult": 2.0}),

    # -- push the level further away -----------------------------------
    ("bufPct=10",                   {"bufPct": 10.0}),
    ("bufPct=15",                   {"bufPct": 15.0}),
    ("bufPct=20",                   {"bufPct": 20.0}),

    # -- skip thin ranges ----------------------------------------------
    ("minRangePct=0.5",             {"minRangePct": 0.5}),
    ("minRangePct=1.0",             {"minRangePct": 1.0}),
    ("minRangePct=1.5",             {"minRangePct": 1.5}),

    # -- tighten the FVG gate (already in the indicator) ----------------
    ("fvgAtrX=0.30",                {"fvgAtrX": 0.30}),
    ("fvgAtrX=0.50",                {"fvgAtrX": 0.50}),
    ("useDisp=on",                  {"useDisp": True}),

    # -- give the trade more room --------------------------------------
    ("atrMult=2.0",                 {"atrMult": 2.0}),
    ("atrMult=2.5",                 {"atrMult": 2.5}),

    # -- take the reversal ---------------------------------------------
    ("reentrySameBar",              {"reentrySameBar": True}),
]

# Combinations of whatever the single-setting pass rewarded.
COMBOS = [
    ("atr2.5 + range1.0",           {"atrMult": 2.5, "minRangePct": 1.0}),
    ("atr2.5 + vol1.5",             {"atrMult": 2.5, "volMult": 1.5}),
    ("atr2.5 + fvg0.5",             {"atrMult": 2.5, "fvgAtrX": 0.50}),
    ("atr2.5 + strong0.5",          {"atrMult": 2.5, "strongClose": 0.5}),
    ("atr2.5 + confirm2",           {"atrMult": 2.5, "confirmBars": 2}),
    ("atr2.5 + range0.5 + vol1.5",  {"atrMult": 2.5, "minRangePct": 0.5, "volMult": 1.5}),
    ("atr2.5 + range1.0 + vol1.5",  {"atrMult": 2.5, "minRangePct": 1.0, "volMult": 1.5}),
    ("atr2.5 + range0.5 + reentry", {"atrMult": 2.5, "minRangePct": 0.5,
                                     "reentrySameBar": True}),
    ("atr2.0 + range0.5 + vol1.2",  {"atrMult": 2.0, "minRangePct": 0.5, "volMult": 1.2}),
]


def load_cache(path=None):
    if path is None:
        files = sorted(f for f in os.listdir(config.DATA_DIR) if f.startswith("candles_"))
        if not files:
            raise SystemExit("No cache found. Run build_cache.py first.")
        path = os.path.join(config.DATA_DIR, files[-1])
    with open(path, "rb") as fh:
        return pickle.load(fh), path


def to_bars(rows):
    return [
        Bar(time=datetime.fromisoformat(t), open=o, high=h, low=l, close=c, volume=v)
        for t, o, h, l, c, v in rows
    ]


def run_config(store, bars_by_symbol, overrides):
    """Run one configuration and return its qualifying trades."""
    settings, _ = config.load()
    for k, v in overrides.items():
        setattr(settings, k, v)
    settings.validate()

    trades = []
    for symbol, bars in bars_by_symbol.items():
        dates = set(store["selection"].get(symbol, ()))
        if not dates:
            continue
        settings.mintick = store["meta"].get(symbol, {}).get("tick", 0.05)
        for t in bt.run(bars, settings).trades:
            if t.entry_time.strftime("%Y-%m-%d") in dates and t.exit == t.exit:
                t.symbol = symbol
                trades.append(t)
    return trades


def stats(trades):
    rs = [t.r_multiple for t in trades if t.r_multiple == t.r_multiple]
    if not rs:
        return dict(n=0, total=0.0, avg=0.0, win=0.0, pf=0.0, dd=0.0, t3=0, sl=0)
    wins = [r for r in rs if r > 0.01]
    losses = [r for r in rs if r < -0.01]
    decided = len(wins) + len(losses)
    gl = abs(sum(losses))
    peak = eq = worst = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return dict(
        n=len(rs), total=sum(rs), avg=sum(rs) / len(rs),
        win=100.0 * len(wins) / decided if decided else 0.0,
        pf=(sum(wins) / gl) if gl else float("inf"), dd=worst,
        t3=sum(1 for t in trades if t.reason == "T3"),
        sl=sum(1 for t in trades if t.reason == "SL"),
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default=None)
    p.add_argument("--config", default=None, help="run only this label")
    p.add_argument("--list", action="store_true")
    p.add_argument("--per-day", action="store_true", help="show the per-day split")
    p.add_argument("--combos", action="store_true", help="also run the combination grid")
    args = p.parse_args()

    if args.list:
        for label, ov in GRID:
            print("  %-24s %s" % (label, ov or "(indicator defaults)"))
        return 0

    store, path = load_cache(args.cache)
    bars_by_symbol = {s: to_bars(r) for s, r in store["candles"].items()}
    total_bars = sum(len(b) for b in bars_by_symbol.values())
    print("cache %s -- %d symbols, %d bars\n" % (os.path.basename(path),
                                                 len(bars_by_symbol), total_bars))

    full = GRID + (COMBOS if args.combos else [])
    grid = [(l, o) for l, o in full if args.config is None or l == args.config]
    base = None
    print("  %-24s %6s %8s %7s %7s %7s %7s %5s %5s"
          % ("CONFIG", "TRADES", "TOTAL R", "AVG R", "WIN%", "PF", "MAXDD", "T3", "SL"))
    print("  " + "-" * 92)
    for label, overrides in grid:
        trades = run_config(store, bars_by_symbol, overrides)
        s = stats(trades)
        if base is None:
            base = s["total"]
        delta = "" if label.startswith("baseline") else "  (%+.1f)" % (s["total"] - base)
        print("  %-24s %6d %+8.2f %+7.2f %6.1f%% %7s %+7.1f %5d %5d%s"
              % (label, s["n"], s["total"], s["avg"], s["win"],
                 "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"],
                 s["dd"], s["t3"], s["sl"], delta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
