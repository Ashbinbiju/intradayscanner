"""
Live-mode bookkeeping for the scanner, kept out of the Streamlit script so it
can be tested.

Everything here works on the trades `backtest.run` produces and the day's
5-minute bars; nothing touches Streamlit.

  * settle_open_trades   close what the engine left open after square-off
  * display_time         the clock time a trader should see for an event
  * reached_targets      T1/T2/T3 touched so far, read from the bars
  * split_open           taken vs skipped trades that are still running
  * open_r               R of an open trade at the last price
  * UniverseLock         the day's locked watchlist, saved to disk
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

FEED_BAR = timedelta(minutes=5)

# Reasons whose event is stamped with the moment it happened (the OPEN of a
# 5-minute bar). Everything else fires somewhere inside a bar and is stamped
# with that bar's open, so the trader-facing time is the bar's close.
AT_OPEN_EXITS = {"SESSION_END", "SESSION_END_EST"}


def _is_open(trade) -> bool:
    return trade.exit != trade.exit  # NaN


def _hhmm(text: str) -> int:
    hh, _, mm = text.partition(":")
    return int(hh) * 60 + int(mm)


# ---------------------------------------------------------------------------
#  Times
# ---------------------------------------------------------------------------
def display_time(moment: Optional[datetime], at_open: bool) -> str:
    """HH:MM a trader should see.

    Events fired *inside* a 5-minute bar are stamped with that bar's open
    (as Pine does), so they show the bar's close: an ORB entry on the 09:30
    close is stamped 09:25 and shown 09:30.

    Trend Breakout fills and square-offs happen *at* a bar's open and are
    stamped with exactly that moment, so adding five minutes would show a
    13:00 fill as 13:05 and a 15:00 square-off as 15:05.
    """
    if moment is None:
        return "open"
    return (moment if at_open else moment + FEED_BAR).strftime("%H:%M")


def entry_display(trade, fills_at_open: bool) -> str:
    return display_time(trade.entry_time, fills_at_open)


def exit_display(trade, fills_at_open: bool) -> str:
    if trade.exit_time is None:
        return "open"
    return display_time(trade.exit_time, fills_at_open and trade.reason in AT_OPEN_EXITS)


# ---------------------------------------------------------------------------
#  Square-off safety net
# ---------------------------------------------------------------------------
def settle_open_trades(trades: Iterable, bars_by_symbol: Dict[str, list],
                       sq_off: str, tz, now: datetime) -> List:
    """Close any trade still open once the square-off has passed.

    The engine squares off at the open of the first bar at/after `sq_off`.
    In live mode that bar may not be in the feed yet when the last refresh
    runs (the feed lags a few seconds to a minute), and the page stops
    refreshing once the strategy is done -- which left taken trades "open"
    forever, with no exit and no R.

    Rules, per still-open trade:
      * a bar at/after the cut-off exists  -> exit at its open (SESSION_END)
      * otherwise, once the cut-off has passed by a full bar -> exit at the
        last close, marked SESSION_END_EST so it is clearly an estimate
      * otherwise leave it open

    Returns the trades it closed.
    """
    if not sq_off:
        return []
    cut = _hhmm(sq_off)
    settled = []
    for t in trades:
        if not _is_open(t):
            continue
        bars = bars_by_symbol.get(getattr(t, "symbol", None)) or []
        day = t.entry_time.astimezone(tz).date()
        same_day = [b for b in bars if b.time.astimezone(tz).date() == day]
        if not same_day:
            continue
        after = [b for b in same_day
                 if b.time.astimezone(tz).hour * 60 + b.time.astimezone(tz).minute >= cut
                 and b.time >= t.entry_time]
        if after:
            bar = after[0]
            t.exit_time, t.exit, t.reason = bar.time, bar.open, "SESSION_END"
            settled.append(t)
            continue
        local_now = now.astimezone(tz)
        cut_moment = local_now.replace(hour=cut // 60, minute=cut % 60, second=0, microsecond=0)
        past_cut = local_now.date() > day or local_now >= cut_moment + FEED_BAR
        if past_cut:
            last = same_day[-1]
            t.exit_time, t.exit, t.reason = last.time + FEED_BAR, last.close, "SESSION_END_EST"
            settled.append(t)
    return settled


# ---------------------------------------------------------------------------
#  Open trades
# ---------------------------------------------------------------------------
def open_r(trade, last_price: float) -> Optional[float]:
    risk = abs(trade.entry - trade.stop)
    if not risk or last_price != last_price:
        return None
    direction = 1 if trade.side == "BUY" else -1
    return (last_price - trade.entry) * direction / risk


def reached_targets(trade, bars: list) -> int:
    """Highest of T1/T2/T3 touched since the fill, read from the bars.

    The engine only books 1R/2R milestones when a whole signal candle (15m)
    closes, so a target touched two minutes ago would still read "next: T1".
    The live table reads the 5-minute bars instead.
    """
    hit = getattr(trade, "targets_hit", 0) or 0
    since = [b for b in bars if b.time >= trade.entry_time]
    if trade.exit_time is not None:
        since = [b for b in since if b.time <= trade.exit_time]
    if not since:
        return hit
    if trade.side == "BUY":
        best = max(b.high for b in since)
        levels = [trade.t1, trade.t2, trade.t3]
        touched = sum(1 for lv in levels if lv == lv and best >= lv)
    else:
        best = min(b.low for b in since)
        levels = [trade.t1, trade.t2, trade.t3]
        touched = sum(1 for lv in levels if lv == lv and best <= lv)
    return max(hit, touched)


def split_open(trades: Iterable, taken: Iterable) -> Tuple[list, list]:
    """(taken trades still open, skipped trades still open)."""
    taken_ids = {id(t) for t in taken}
    running = [t for t in trades if _is_open(t)]
    return ([t for t in running if id(t) in taken_ids],
            [t for t in running if id(t) not in taken_ids])


# ---------------------------------------------------------------------------
#  The day's locked watchlist
# ---------------------------------------------------------------------------
class UniverseLock:
    """The watchlist frozen when entries open, saved per day and per universe.

    Kept on disk rather than only in the browser session: a Streamlit
    reconnect starts a fresh session, and rebuilding the lock from whatever
    the screener lists *at reconnect time* silently replays the whole morning
    on a different set of stocks.
    """

    def __init__(self, data_dir: str, day: str, spec: dict):
        self.data_dir = data_dir
        self.day = day
        self.key = self.spec_key(spec)
        self.path = os.path.join(data_dir, "locked_%s_%s.json" % (day, self.key))

    @staticmethod
    def spec_key(spec: dict) -> str:
        """Same universe definition -> same key. Day-specific fields excluded."""
        relevant = {k: v for k, v in (spec or {}).items() if k not in ("day",)}
        if isinstance(relevant.get("buckets"), list):
            relevant["buckets"] = sorted(relevant["buckets"])
        blob = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    def load(self) -> Optional[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            return None
        data.setdefault("details", {})
        return data

    def save(self, symbols: List[str], details: dict, when: datetime) -> dict:
        data = {"day": self.day, "symbols": list(symbols),
                "details": _plain(details, symbols), "locked_at": when.isoformat()}
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # read-only host: the session copy still works
        return data

    def clear(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass


def _plain(details: dict, symbols: List[str]) -> dict:
    """Screener facts for the locked symbols, reduced to JSON-safe values."""
    out = {}
    for s in symbols:
        row = (details or {}).get(s)
        if not isinstance(row, dict):
            continue
        keep = {}
        for k, v in row.items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                keep[k] = v
            elif isinstance(v, (list, tuple)):
                keep[k] = [x for x in v if isinstance(x, (int, float, str, bool))]
        out[s] = keep
    return out
