"""
Supertrend flip with Entry / SL / TP1-TP3 -- as an intraday engine.

Signal logic is the Pine indicator "Supertrend with Entry, TP1, TP2 & TP3"
with its default inputs: a BUY when ``ta.supertrend(factor, atrPeriod)`` turns
up, a SELL when it turns down, entry at the signal candle's close, stop
``slAtrMult x ATR`` away and targets at 1R / 2R / 3R. The indicator's optional
filters and re-entries (all off by default there) are not ported.

What is added so it can be traded MIS, none of it in the indicator:

  * signals run on ``tfMinutes`` candles (15 by default) built here from the
    5-minute feed, aligned to ``sessionStart``; stops and targets are checked
    on every 5-minute bar, which resolves more intrabar order than the 15m
    candle alone could
  * entries only when the signal candle closes inside ``entryFrom-entryTo``
  * everything flat by ``sqOffTime``
  * a choice of exit methods (``exitMode``), and an opposite signal closing
    the trade (``exitOnFlip``, which the indicator also does by replacing it)

The Supertrend itself runs over all history, across days, exactly as it does
on a TradingView chart -- only the trading is confined to the session. It emits
the same Events as the ORB engine, so the backtester, portfolio and scanner
drive it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .pine import NA, ATR, na
from .strategy import Bar, Event, EventType


class SupertrendCalc:
    """Pine ``ta.supertrend(factor, atrPeriod)``, one candle at a time.

    Returns (supertrend, direction): direction -1 is an uptrend, +1 a
    downtrend, the same sign convention as Pine.
    """

    def __init__(self, factor: float, period: int):
        self.factor = factor
        self._atr = ATR(period)
        self.atr = NA
        self._prev_atr = NA
        self._prev_close = NA
        self._lower = NA
        self._upper = NA
        self._st = NA
        self.direction: Optional[int] = None
        self.value = NA

    def update(self, high: float, low: float, close: float) -> Tuple[float, int]:
        atr = self._atr.update(high, low, close)
        src = (high + low) / 2.0
        upper = src + self.factor * atr if not na(atr) else NA
        lower = src - self.factor * atr if not na(atr) else NA
        prev_lower = 0.0 if na(self._lower) else self._lower
        prev_upper = 0.0 if na(self._upper) else self._upper
        pc = self._prev_close
        # Pine: lowerBand := lowerBand > prevLowerBand or close[1] < prevLowerBand ? lowerBand : prevLowerBand
        if not ((not na(lower) and lower > prev_lower) or (not na(pc) and pc < prev_lower)):
            lower = prev_lower
        if not ((not na(upper) and upper < prev_upper) or (not na(pc) and pc > prev_upper)):
            upper = prev_upper
        if na(self._prev_atr):
            direction = 1
        elif self._st == self._upper:
            direction = -1 if close > upper else 1
        else:
            direction = 1 if close < lower else -1
        st = lower if direction == -1 else upper

        self._prev_atr = atr
        self._prev_close = close
        self._lower, self._upper, self._st = lower, upper, st
        self.atr, self.direction, self.value = atr, direction, st
        return st, direction


@dataclass
class _Candle:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    last_time: datetime         # open time of the last feed bar folded in


def _minutes(text: str) -> int:
    hh, _, mm = text.partition(":")
    return int(hh) * 60 + int(mm)


class SupertrendStrategy:
    """Bar-by-bar state machine. Feed it closed feed bars (5-minute) in order."""

    def __init__(self, settings):
        settings.validate()
        self.s = settings
        self.tz = ZoneInfo(settings.tzIn)
        self.calc = SupertrendCalc(settings.factor, settings.atrPeriod)
        self.tf = timedelta(minutes=settings.tfMinutes)
        self.feed_bar = timedelta(minutes=settings.barMinutes)
        self.anchor = _minutes(settings.sessionStart)
        self.entry_from = _minutes(settings.entryFrom)
        self.entry_to = _minutes(settings.entryTo)
        self.sq_minute = _minutes(settings.sqOffTime) if settings.sqOffTime else None
        self.sq_tz = ZoneInfo(settings.sqOffTz)

        self.bar_index = -1
        self.candle: Optional[_Candle] = None
        self.prev_direction: Optional[int] = None
        self.history: List[Tuple[datetime, float, int]] = []   # (candle end, line, dir)
        self.last_close = NA
        self.last_time: Optional[datetime] = None
        self._day = None

        self.pos = 0
        self.ePx = self.sPx = self.iSPx = NA
        self.p1 = self.p2 = self.p3 = NA
        self.hits = [False, False, False]
        self.entry_index = -1
        self.trades_today = 0

        self.events: List[Event] = []
        self._bar_events: List[Event] = []

    # -- helpers -----------------------------------------------------------
    def _fmt(self, price) -> str:
        return "-" if na(price) else "%.2f" % price

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        self._bar_events.append(event)

    def _side(self) -> str:
        return "BUY" if self.pos == 1 else "SELL"

    def _bucket_start(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.tz)
        minute = local.hour * 60 + local.minute
        offset = (minute - self.anchor) % self.s.tfMinutes
        return (local - timedelta(minutes=offset)).replace(second=0, microsecond=0)

    def _close(self, moment, price, kind, message, reason=None) -> None:
        self._emit(Event(kind, moment, self.bar_index, message, side=self._side(),
                         price=price, closes_position=True, reason=reason))
        self.pos = 0

    def _move_stop(self, moment, new_stop: float, why: str) -> None:
        tighter = new_stop > self.sPx if self.pos == 1 else new_stop < self.sPx
        if na(new_stop) or not tighter:
            return
        self.sPx = new_stop
        self._emit(Event(EventType.STOP_MOVED, moment, self.bar_index,
                         "stop -> %s (%s)" % (self._fmt(new_stop), why),
                         side=self._side(), stop=new_stop))

    # -- trade management on every feed bar ---------------------------------
    def _manage(self, bar: Bar) -> None:
        s, d = self.s, self.pos
        moment = bar.time
        # Gapped through the stop: the fill is the open, not the stop price.
        if d * (bar.open - self.sPx) <= 0:
            self._close(moment, bar.open, EventType.STOP_HIT,
                        "gap through stop, out @ %s" % self._fmt(bar.open))
            return
        stop_hit = bar.low <= self.sPx if d == 1 else bar.high >= self.sPx
        if stop_hit and s.pessimisticSameBar:
            self._close(moment, self.sPx, EventType.STOP_HIT, "SL hit @ %s" % self._fmt(self.sPx))
            return

        closing_target = {"T3": 3, "T1": 1}.get(s.exitMode)
        targets = (self.p1, self.p2, self.p3)
        for n in (1, 2, 3):
            if self.hits[n - 1]:
                continue
            level = targets[n - 1]
            if not (bar.high >= level if d == 1 else bar.low <= level):
                break
            self.hits[n - 1] = True
            closes = closing_target == n
            fill = bar.open if d * (bar.open - level) > 0 else level
            self._emit(Event(EventType.TARGET_HIT, moment, self.bar_index,
                             "T%d hit @ %s" % (n, self._fmt(fill)), side=self._side(),
                             price=fill, target_no=n, closes_position=closes))
            if closes:
                self.pos = 0
                return

        if stop_hit:          # optimistic same-bar mode: targets first, then the stop
            self._close(moment, self.sPx, EventType.STOP_HIT, "SL hit @ %s" % self._fmt(self.sPx))
            return
        if s.exitMode == "Step trail":
            ladder = (self.iSPx, self.ePx, self.p1, self.p2)[sum(self.hits)]
            self._move_stop(moment, ladder, "T%d reached" % sum(self.hits))

    # -- one signal candle ----------------------------------------------------
    def _on_candle(self, c: _Candle) -> None:
        s = self.s
        st, direction = self.calc.update(c.high, c.low, c.close)
        self.history.append((c.end, st, direction))
        prev, self.prev_direction = self.prev_direction, direction
        flip = 0
        if prev is not None and direction != prev:
            flip = 1 if direction < 0 else -1
        moment = c.last_time

        if self.pos and s.exitMode == "ST trail" and not flip:
            self._move_stop(moment, st, "supertrend")

        if self.pos and flip and flip != self.pos and s.exitOnFlip:
            self._close(moment, c.close, EventType.SESSION_EXIT,
                        "opposite signal, out @ %s" % self._fmt(c.close), reason="FLIP")

        if not flip or self.pos:
            return
        local_end = c.end.astimezone(self.tz)
        end_minute = local_end.hour * 60 + local_end.minute
        if not (self.entry_from <= end_minute <= self.entry_to):
            return
        if self._past_square_off(moment):
            return
        if (flip == 1 and not s.allowL) or (flip == -1 and not s.allowS):
            return
        if s.maxTradesPerDay and self.trades_today >= s.maxTradesPerDay:
            return
        atr = self.calc.atr
        if na(atr) or atr <= 0:
            return

        entry = c.close
        stop = entry - flip * atr * s.slAtrMult
        risk = abs(entry - stop)
        self.pos = flip
        self.ePx, self.sPx, self.iSPx = entry, stop, stop
        self.p1, self.p2, self.p3 = (entry + flip * risk * r for r in (s.r1, s.r2, s.r3))
        self.hits = [False, False, False]
        self.entry_index = self.bar_index
        self.trades_today += 1
        self._emit(Event(
            EventType.ENTRY, moment, self.bar_index,
            "%s %s | SL %s | T1 %s T2 %s T3 %s (supertrend %s)"
            % (self._side(), self._fmt(entry), self._fmt(stop), self._fmt(self.p1),
               self._fmt(self.p2), self._fmt(self.p3), self._fmt(st)),
            side=self._side(), price=entry, stop=stop,
            t1=self.p1, t2=self.p2, t3=self.p3, risk=risk))

    def _past_square_off(self, moment: datetime) -> bool:
        if self.sq_minute is None:
            return False
        local = moment.astimezone(self.sq_tz)
        return local.hour * 60 + local.minute >= self.sq_minute

    # =====================================================================
    def on_bar(self, bar: Bar) -> List[Event]:
        self._bar_events = []
        self.bar_index += 1
        moment = bar.time
        day = moment.astimezone(self.tz).date()

        # A candle the feed never finished (a missing bar) is closed out as
        # soon as a bar from a later slot arrives, so the signal is late
        # rather than lost.
        start = self._bucket_start(moment)
        if self.candle is not None and start != self.candle.start:
            self._on_candle(self.candle)
            self.candle = None

        if day != self._day:
            if self.pos and self.sq_minute is not None and self.last_time is not None:
                self._close(self.last_time, self.last_close, EventType.SESSION_EXIT,
                            "square off @ %s (session ended)" % self._fmt(self.last_close))
            self._day = day
            self.trades_today = 0

        if self.pos and self.bar_index > self.entry_index:
            self._manage(bar)

        if self.candle is None:
            self.candle = _Candle(start, start + self.tf, bar.open, bar.high, bar.low,
                                  bar.close, moment)
        else:
            c = self.candle
            c.high, c.low, c.close, c.last_time = max(c.high, bar.high), min(c.low, bar.low), bar.close, moment
        if moment + self.feed_bar >= self.candle.end:
            self._on_candle(self.candle)
            self.candle = None

        if self.pos and self._past_square_off(moment):
            self._close(moment, bar.close, EventType.SESSION_EXIT,
                        "square off @ %s" % self._fmt(bar.close))

        self.last_close, self.last_time = bar.close, moment
        return self._bar_events

    # -- dashboard ---------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "status": "LONG" if self.pos == 1 else "SHORT" if self.pos == -1 else "FLAT",
            "trend": "UP" if self.prev_direction == -1 else "DOWN" if self.prev_direction == 1 else "-",
            "supertrend": self.calc.value,
            "entry": self.ePx,
            "stop": self.sPx,
            "targets": (self.p1, self.p2, self.p3),
            "hits": tuple(self.hits),
            "trades_today": self.trades_today,
            "atr": self.calc.atr,
        }
