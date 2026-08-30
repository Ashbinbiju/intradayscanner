"""
Replay historical 5-minute candles through the engine.

This exists to answer one question before any money moves: does the Python
port produce the same signals as the indicator on TradingView?  Run it over a
window you can also see on a chart and compare entries bar by bar.

Exits are scored the way the indicator marks them -- the trade is closed by T3,
by the stop, or by the end of the signal window, and T1/T2 are recorded as
touched but do not reduce the position.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .strategy import Bar, Event, EventType, ORBFVGStrategy


@dataclass
class BacktestTrade:
    side: str
    entry_time: datetime
    entry: float
    stop: float
    t1: float
    t2: float
    t3: float
    risk: float
    exit_time: Optional[datetime] = None
    exit: float = float("nan")
    reason: str = ""
    targets_hit: int = 0
    final_stop: float = float("nan")
    stop_moves: int = 0

    @property
    def r_multiple(self) -> float:
        if self.risk <= 0 or self.exit != self.exit:  # NaN check
            return float("nan")
        direction = 1 if self.side == "BUY" else -1
        return (self.exit - self.entry) * direction / self.risk

    @property
    def points(self) -> float:
        direction = 1 if self.side == "BUY" else -1
        return (self.exit - self.entry) * direction


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    bars: int = 0
    sessions: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    # -- stats ------------------------------------------------------------
    @property
    def closed(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.exit == t.exit]

    def summary(self) -> dict:
        closed = self.closed
        rs = [t.r_multiple for t in closed if t.r_multiple == t.r_multiple]
        # A trailed stop at entry exits at exactly 0R. Counting that as a loss
        # would understate the win rate, so scratches are scored separately.
        scratch_band = 0.01
        wins = [r for r in rs if r > scratch_band]
        losses = [r for r in rs if r < -scratch_band]
        scratches = [r for r in rs if -scratch_band <= r <= scratch_band]
        decided = len(wins) + len(losses)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "bars": self.bars,
            "sessions": self.sessions,
            "trades": len(closed),
            "longs": sum(1 for t in closed if t.side == "BUY"),
            "shorts": sum(1 for t in closed if t.side == "SELL"),
            "wins": len(wins),
            "losses": len(losses),
            "scratches": len(scratches),
            "win_rate": (100.0 * len(wins) / decided) if decided else 0.0,
            "total_r": sum(rs),
            "avg_r": (sum(rs) / len(rs)) if rs else 0.0,
            "best_r": max(rs) if rs else 0.0,
            "worst_r": min(rs) if rs else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "expectancy_r": (sum(rs) / len(rs)) if rs else 0.0,
            "max_drawdown_r": self._max_drawdown(rs),
            "t1_hits": sum(1 for t in closed if t.targets_hit >= 1),
            "t2_hits": sum(1 for t in closed if t.targets_hit >= 2),
            "t3_hits": sum(1 for t in closed if t.targets_hit >= 3),
            "stopped": sum(1 for t in closed if t.reason == "SL"),
            "trailed_out": sum(1 for t in closed if t.reason == "TRAIL"),
            "session_exits": sum(1 for t in closed if t.reason == "SESSION_END"),
            "trailed": sum(1 for t in closed if t.stop_moves > 0),
            "total_points": sum(t.points for t in closed),
        }

    @staticmethod
    def _max_drawdown(rs: List[float]) -> float:
        peak = 0.0
        equity = 0.0
        worst = 0.0
        for r in rs:
            equity += r
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst


def run(bars: List[Bar], settings) -> BacktestResult:
    """Feed every bar through a fresh engine and pair entries with exits."""
    strategy = ORBFVGStrategy(settings)
    result = BacktestResult(bars=len(bars))
    if bars:
        result.start, result.end = bars[0].time, bars[-1].time

    open_trade: Optional[BacktestTrade] = None
    sessions = set()

    for bar in bars:
        for event in strategy.on_bar(bar):
            result.events.append(event)

            if event.type is EventType.RANGE_LOCKED:
                sessions.add(event.time.date())

            elif event.type is EventType.ENTRY:
                open_trade = BacktestTrade(
                    side=event.side, entry_time=event.time, entry=event.price,
                    stop=event.stop, t1=event.t1, t2=event.t2, t3=event.t3,
                    risk=event.risk, final_stop=event.stop,
                )
                result.trades.append(open_trade)

            elif open_trade is not None:
                if event.type is EventType.STOP_MOVED:
                    open_trade.final_stop = event.stop
                    open_trade.stop_moves += 1
                elif event.type is EventType.TARGET_HIT:
                    open_trade.targets_hit = max(open_trade.targets_hit, event.target_no or 0)
                    if event.closes_position:
                        open_trade.exit_time = event.time
                        open_trade.exit = event.price
                        open_trade.reason = "T3"
                        open_trade = None
                elif event.type is EventType.STOP_HIT:
                    open_trade.exit_time = event.time
                    open_trade.exit = event.price
                    open_trade.reason = "TRAIL" if open_trade.stop_moves else "SL"
                    open_trade = None
                elif event.type is EventType.SESSION_EXIT:
                    open_trade.exit_time = event.time
                    open_trade.exit = event.price
                    open_trade.reason = "SESSION_END"
                    open_trade = None

    result.sessions = len(sessions)
    return result


# ---------------------------------------------------------------------------
#  Reporting
# ---------------------------------------------------------------------------
def format_report(result: BacktestResult, symbol: str, settings) -> str:
    s = result.summary()
    lines = []
    add = lines.append

    add("=" * 74)
    add("  ORB + FVG backtest    %s" % symbol)
    add("=" * 74)
    if result.start and result.end:
        add("  Period        %s  ->  %s"
            % (result.start.strftime("%Y-%m-%d %H:%M"), result.end.strftime("%Y-%m-%d %H:%M")))
    add("  Bars          %d over %d sessions" % (s["bars"], s["sessions"]))
    add("  Opening range %s   Signal window %s   TZ %s"
        % (settings.orSess, settings.sigSess, settings.tzIn))
    add("  FVG gate      %s   Stop %s   Targets %gR / %gR / %gR"
        % ("on" if settings.useFvg else "off", settings.slMode,
           settings.r1, settings.r2, settings.r3))
    filters = []
    if settings.strongClose > 0:
        filters.append("strongClose %.2f" % settings.strongClose)
    if settings.volMult > 0:
        filters.append("volMult %.2f x%d" % (settings.volMult, settings.volLen))
    if settings.minRangePct > 0:
        filters.append("minRangePct %.2f%%" % settings.minRangePct)
    if settings.confirmBars > 1:
        filters.append("confirmBars %d" % settings.confirmBars)
    if settings.reentrySameBar:
        filters.append("reentrySameBar")
    add("  Stop mult     %.2f x ATR(%d)%s"
        % (settings.atrMult, settings.atrLen,
           "   [Pine default is 1.50]" if settings.atrMult != 1.5 else ""))
    add("  Filters       %s" % (", ".join(filters) if filters else "none"))
    add("  Trailing      %s" % (
        "off (fixed stop, as the indicator)" if not settings.useTrail
        else "%s  (BE at T1 %s, step to T1 at T2 %s, ATR x%.1f after %gR)"
             % (settings.trailMode, "on" if settings.trailBE else "off",
                "on" if settings.trailStep else "off",
                settings.trailAtrMult, settings.trailStartR)))
    add("-" * 74)
    add("  Trades        %d   (%d long / %d short)" % (s["trades"], s["longs"], s["shorts"]))
    add("  Win rate      %.1f%%   (%d win / %d loss / %d scratch)"
        % (s["win_rate"], s["wins"], s["losses"], s["scratches"]))
    add("  Total R       %+.2f      Average %+.2fR" % (s["total_r"], s["avg_r"]))
    add("  Profit factor %s" % ("inf" if s["profit_factor"] == float("inf")
                                else "%.2f" % s["profit_factor"]))
    add("  Best / worst  %+.2fR / %+.2fR" % (s["best_r"], s["worst_r"]))
    add("  Max drawdown  %.2fR" % s["max_drawdown_r"])
    add("  Total points  %+.2f" % s["total_points"])
    add("-" * 74)
    add("  Exits         T3 %d   SL %d   trailed out %d   session end %d"
        % (s["t3_hits"], s["stopped"], s["trailed_out"], s["session_exits"]))
    add("  Targets hit   T1 %d   T2 %d   T3 %d" % (s["t1_hits"], s["t2_hits"], s["t3_hits"]))
    add("  Stop trailed  %d of %d trades" % (s["trailed"], s["trades"]))
    add("=" * 74)

    if result.closed:
        add("")
        add("  %-16s %-5s %9s %9s %9s %9s %-12s %6s"
            % ("ENTRY", "SIDE", "PRICE", "STOP", "TRAIL", "EXIT", "REASON", "R"))
        add("  " + "-" * 80)
        for t in result.closed:
            add("  %-16s %-5s %9.2f %9.2f %9.2f %9.2f %-12s %+6.2f"
                % (t.entry_time.strftime("%Y-%m-%d %H:%M"), t.side, t.entry,
                   t.stop, t.final_stop, t.exit, t.reason, t.r_multiple))
    else:
        add("")
        add("  No trades were taken in this window.")
    return "\n".join(lines)


def export_trades(result: BacktestResult, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "side", "entry_time", "entry", "stop", "final_stop", "stop_moves",
            "t1", "t2", "t3",
            "exit_time", "exit", "reason", "targets_hit", "r_multiple", "points",
        ])
        for t in result.trades:
            writer.writerow([
                t.side, t.entry_time.strftime("%Y-%m-%d %H:%M"),
                "%.2f" % t.entry, "%.2f" % t.stop,
                "%.2f" % t.final_stop, t.stop_moves, "%.2f" % t.t1,
                "%.2f" % t.t2, "%.2f" % t.t3,
                t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                "%.2f" % t.exit if t.exit == t.exit else "",
                t.reason, t.targets_hit,
                "%.3f" % t.r_multiple if t.r_multiple == t.r_multiple else "",
                "%.2f" % t.points if t.exit == t.exit else "",
            ])
    return path
