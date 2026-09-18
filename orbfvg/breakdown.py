"""
Trend -> consolidation -> breakdown -> retest -> short.

A bull-trap reversal, and the mirror image of the ORB system in temperament:
that one buys strength breaking out, this one sells strength that has stopped
working. The shape, in the order the engine looks for it:

  1. TREND         price runs up hard -- the move that pulls buyers in
  2. CONSOLIDATION it stops going up and goes sideways instead
  3. BREAKDOWN     the floor of that range gives way on a close
  4. RETEST        price climbs back to the broken floor and fails there
  5. SHORT         entry on the failure, stop above it, target 2R

Short only. Step 5 has no long counterpart here by design: the setup is
specifically an up-move that has failed, so a symmetrical "buy the breakout"
arm would be a different idea wearing the same name.

Everything is expressed in percent of price rather than absolute rupees, so
one parameter set applies to a 90-rupee stock and a 4,000-rupee one. The
engine emits the same Events as the ORB strategy, so the backtester, the
portfolio simulator and the scanner all drive it unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from .pine import NA, ATR, SessionWindow, Series, na
from .strategy import Bar, Event, EventType

log = logging.getLogger("orbfvg.breakdown")


@dataclass
class Setup:
    """A consolidation whose floor has broken, waiting for its retest."""

    support: float          # the floor that broke
    resistance: float       # the ceiling of the range
    consol_start: int
    consol_end: int
    break_bar: int
    break_price: float
    retest_high: float = NA  # highest price reached during the retest


class BreakdownRetestStrategy:
    """Bar-by-bar state machine. Feed it closed bars in order.

    States: watching for a setup -> broken, waiting for a retest -> in a
    trade. A break that is never retested inside `retestBars` is abandoned,
    because by then the move has gone without us and chasing it is a
    different trade.
    """

    def __init__(self, settings):
        settings.validate()
        self.s = settings
        self.tz = ZoneInfo(settings.tzIn)
        self.sig_window = SessionWindow.parse(settings.sigSess)

        self.open = Series()
        self.high = Series()
        self.low = Series()
        self.close = Series()
        self.volume = Series()
        self.times: List[datetime] = []

        self._atr = ATR(settings.atrLen)
        self.atr = NA
        self.bar_index = -1

        self.sq_tz = None
        self.sq_minute = None
        if getattr(settings, "sqOffTime", ""):
            hh, mm = (int(x) for x in settings.sqOffTime.split(":"))
            self.sq_minute = hh * 60 + mm
            self.sq_tz = ZoneInfo(settings.sqOffTz)

        self.setup: Optional[Setup] = None
        self.pos = 0
        self.ePx = NA
        self.sPx = NA
        self.p1 = NA          # 1R, informational
        self.p2 = NA          # 2R, the exit
        self.h1 = False
        self.entry_time: Optional[datetime] = None
        self.trades_today = 0
        self._day: Optional[str] = None

        self.events: List[Event] = []
        self._bar_events: List[Event] = []

    # -- helpers -----------------------------------------------------------
    def _fmt(self, price) -> str:
        return "-" if na(price) else "%.2f" % price

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        self._bar_events.append(event)

    def _day_at(self, offset: int) -> Optional[str]:
        """Trading day of the bar `offset` bars back, or None before history."""
        idx = len(self.times) - 1 - offset
        if idx < 0:
            return None
        return self.times[idx].astimezone(self.sq_tz or self.tz).strftime("%Y-%m-%d")

    def _past_square_off(self, moment: datetime) -> bool:
        if self.sq_minute is None:
            return False
        local = moment.astimezone(self.sq_tz)
        return local.hour * 60 + local.minute >= self.sq_minute

    # -- pattern detection -------------------------------------------------
    def _find_consolidation(self, end_idx: int):
        """The sideways range under the swing high, ending at `end_idx`.

        Anchored to the peak rather than grown backwards until it stops being
        tight. Growing it blindly walks into the tail of the rally, and since
        those bars sit lower, the "support" ends up being a rally low well
        under the actual floor -- so the breakdown never triggers, or triggers
        far too late.

        Anchoring says what the chart says: price made a high, then went
        sideways beneath it. The floor is the lowest low since that high.
        """
        s = self.s
        back0 = self.bar_index - end_idx

        # Anchored to the bar being evaluated, not to the end of the window:
        # anchoring to the window would let yesterday's range qualify against
        # today's first bar, and the overnight gap alone would look like a
        # breakdown.
        today = self._day_at(0)
        peak_offset, peak = None, NA
        for k in range(s.consolMaxBars):
            value = self.high[back0 + k]
            # The range has to be one session's. A window spanning the close
            # would put yesterday's floor under today's price, and the gap
            # alone would read as a breakdown.
            if na(value) or self._day_at(back0 + k) != today:
                break
            # >= keeps the *earliest* bar of a flat top, so a range whose bars
            # share the same high is measured from its start, not its end.
            if na(peak) or value >= peak:
                peak, peak_offset = value, k
        if peak_offset is None:
            return None

        n = peak_offset + 1
        if n < s.consolMinBars or end_idx - n + 1 < 0:
            return None

        highs = [self.high[back0 + k] for k in range(n)]
        lows = [self.low[back0 + k] for k in range(n)]
        if any(na(v) for v in highs + lows):
            return None
        if any(self._day_at(back0 + k) != today for k in range(n)):
            return None
        hi, lo = max(highs), min(lows)
        mid = (hi + lo) / 2.0
        if mid <= 0 or (hi - lo) / mid * 100.0 > s.consolMaxPct:
            return None
        return (end_idx - n + 1, end_idx, hi, lo)

    def _had_uptrend(self, consol_start: int, consol_high: float) -> bool:
        """Did price run up into this range, and by enough to matter?"""
        s = self.s
        back_from = self.bar_index - consol_start
        today = self._day_at(0)
        lows = []
        for k in range(1, s.trendBars + 1):
            value = self.low[back_from + k]
            # Same reasoning as the range: yesterday's low is not this
            # session's rally.
            if na(value) or self._day_at(back_from + k) != today:
                break
            lows.append(value)
        if not lows:
            return False
        run_low = min(lows)
        if run_low <= 0:
            return False
        return (consol_high - run_low) / run_low * 100.0 >= s.trendPct

    # =====================================================================
    def on_bar(self, bar: Bar) -> List[Event]:
        self._bar_events = []
        s = self.s

        self.bar_index += 1
        moment = bar.time
        session_moment = moment.astimezone(self.tz)
        self.times.append(moment)
        self.open.push(bar.open)
        self.high.push(bar.high)
        self.low.push(bar.low)
        self.close.push(bar.close)
        self.volume.push(bar.volume)

        high, low, close = bar.high, bar.low, bar.close
        self.atr = self._atr.update(high, low, close)

        day = moment.strftime("%Y-%m-%d")
        if day != self._day:
            # A setup does not survive the overnight gap: the range it was
            # built from no longer describes where price is.
            self._day = day
            self.trades_today = 0
            self.setup = None

        in_session = self.sig_window.contains(session_moment)
        past_cut = self._past_square_off(moment)

        # -- manage an open trade first ------------------------------------
        if self.pos == -1:
            if (not self.h1) and low <= self.p1:
                self.h1 = True
                self._emit(Event(
                    EventType.TARGET_HIT, moment, self.bar_index,
                    "1R reached at %s" % self._fmt(self.p1),
                    side="SELL", price=self.p1, target_no=1))
            hit_target = low <= self.p2
            hit_stop = high >= self.sPx
            # Both in one bar: 5-minute data cannot say which came first, so
            # take the stop. Awarding the target instead would overstate the
            # result on exactly the bars where it matters most.
            if hit_target and not (hit_stop and s.pessimisticSameBar):
                self._emit(Event(
                    EventType.TARGET_HIT, moment, self.bar_index,
                    "target 2R hit at %s" % self._fmt(self.p2),
                    side="SELL", price=self.p2, target_no=2,
                    closes_position=True))
                self.pos = 0
            elif hit_stop:
                self._emit(Event(
                    EventType.STOP_HIT, moment, self.bar_index,
                    "stop hit at %s" % self._fmt(self.sPx),
                    side="SELL", price=self.sPx, closes_position=True))
                self.pos = 0
            elif past_cut:
                self._emit(Event(
                    EventType.SESSION_EXIT, moment, self.bar_index,
                    "square off at %s" % self._fmt(close),
                    side="SELL", price=close, closes_position=True))
                self.pos = 0
            return self._bar_events

        # -- look for the pattern ------------------------------------------
        if self.bar_index < s.trendBars + s.consolMinBars:
            return self._bar_events

        if self.setup is None:
            window = self._find_consolidation(self.bar_index - 1)
            if window:
                start, end, hi, lo = window
                floor = lo * (1.0 - s.breakBufPct / 100.0)
                if close < floor and self._had_uptrend(start, hi):
                    self.setup = Setup(support=lo, resistance=hi,
                                       consol_start=start, consol_end=end,
                                       break_bar=self.bar_index,
                                       break_price=close)
                    self._emit(Event(
                        EventType.RANGE_LOCKED, moment, self.bar_index,
                        "breakdown: range %s-%s broke at %s, waiting for the retest"
                        % (self._fmt(lo), self._fmt(hi), self._fmt(close))))
            return self._bar_events

        # -- a break is live; wait for price to come back to it -------------
        setup = self.setup
        age = self.bar_index - setup.break_bar
        if age > s.retestBars:
            self.setup = None
            return self._bar_events

        tolerance = setup.support * (1.0 - s.retestTolPct / 100.0)
        touched = high >= tolerance
        if not touched:
            return self._bar_events

        setup.retest_high = high if na(setup.retest_high) else max(setup.retest_high, high)

        # The retest has to fail, not just happen. Requiring the close back
        # under the broken floor is what separates a rejection from price
        # simply reclaiming the range and carrying on up.
        if s.requireRejection and close >= setup.support:
            return self._bar_events
        if not in_session or past_cut:
            return self._bar_events
        if self.trades_today >= s.maxTradesPerDay:
            return self._bar_events

        entry = close
        if s.stopMode == "ATR":
            stop = entry + self.atr * s.stopAtrMult
        else:
            stop = setup.retest_high * (1.0 + s.stopBufPct / 100.0)
        risk = stop - entry
        if not risk > 0:
            return self._bar_events
        if s.minRiskPct > 0 and risk / entry * 100.0 < s.minRiskPct:
            # Too tight to be tradable after costs and slippage.
            return self._bar_events

        self.ePx, self.sPx = entry, stop
        self.p1 = entry - risk
        self.p2 = entry - risk * s.targetR
        self.pos = -1
        self.h1 = False
        self.entry_time = moment
        self.trades_today += 1
        self.setup = None
        self._emit(Event(
            EventType.ENTRY, moment, self.bar_index,
            "SELL %s | stop %s | 1R %s | target %s"
            % (self._fmt(entry), self._fmt(stop), self._fmt(self.p1), self._fmt(self.p2)),
            side="SELL", price=entry, stop=stop,
            t1=self.p1, t2=self.p2, t3=self.p2, risk=risk))
        return self._bar_events

    # -- dashboard ---------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "status": "SHORT" if self.pos else "FLAT",
            "watching": self.setup is not None,
            "support": self.setup.support if self.setup else NA,
            "resistance": self.setup.resistance if self.setup else NA,
            "entry": self.ePx,
            "stop": self.sPx,
            "targets": (self.p1, self.p2),
            "trades_today": self.trades_today,
            "atr": self.atr,
        }
