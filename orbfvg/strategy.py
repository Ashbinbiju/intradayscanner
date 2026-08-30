"""
Session Opening Range + 5-minute FVG -- a faithful Python port.

This is a direct translation of the Pine v6 indicator "Forex ORB + FVG --
Buy/Sell + Targets". The logic is unchanged; only the drawing calls are
dropped, replaced by events the broker layer can act on.

The bar is processed top-to-bottom in the same order Pine evaluates the
script, because that order is load-bearing:

  1. ATR and the per-bar pip size
  2. session membership -> orNew / sessEnd
  3. opening-range reset, accumulate, lock
  4. the FVG engine: detect, then prune mitigated/expired
  5. breakout levels and trigger crosses
  6. entry
  7. target / stop hit tracking
  8. end-of-session exit

Two details of the original are preserved deliberately, because they change
results and are easy to "fix" by accident:

  * `upTrig` compares the *previous* bar's source against the *current* bar's
    level (`srcH[1] <= lvlUp`, not `lvlUp[1]`).
  * On the bar that resolves a trade, targets are tested before the stop, and
    the stop test is skipped once T3 has closed the position. Same-bar
    ambiguity resolves in the target's favour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional
from zoneinfo import ZoneInfo

from .pine import (
    NA,
    ATR,
    Series,
    SessionWindow,
    format_pips,
    na,
    pine_max,
    pine_min,
)


# ---------------------------------------------------------------------------
#  Bars and events
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    """One 5-minute candle. ``time`` is the bar's OPEN, timezone-aware."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True  # Pine barstate.isconfirmed

    def __post_init__(self):
        if self.time.tzinfo is None:
            raise ValueError("Bar.time must be timezone-aware")


class EventType(str, Enum):
    RANGE_LOCKED = "RANGE_LOCKED"
    ENTRY = "ENTRY"
    TARGET_HIT = "TARGET_HIT"
    STOP_MOVED = "STOP_MOVED"
    STOP_HIT = "STOP_HIT"
    SESSION_EXIT = "SESSION_EXIT"


@dataclass
class Event:
    type: EventType
    time: datetime
    bar_index: int
    message: str
    side: Optional[str] = None          # "BUY" / "SELL"
    price: float = NA                   # entry price, or the exit price
    stop: float = NA
    t1: float = NA
    t2: float = NA
    t3: float = NA
    risk: float = NA                    # per-unit risk, in price
    target_no: Optional[int] = None     # 1, 2 or 3 for TARGET_HIT
    closes_position: bool = False       # broker should flatten on this event

    def __str__(self) -> str:
        return "[%s] %s" % (self.time.strftime("%Y-%m-%d %H:%M"), self.message)


@dataclass
class FVG:
    """One fair-value gap. ``bar`` is the bar_index it was detected on."""

    top: float
    bot: float
    bar: int


# ---------------------------------------------------------------------------
#  The strategy
# ---------------------------------------------------------------------------
class ORBFVGStrategy:
    """Bar-by-bar state machine. Feed it closed 5-minute bars in order.

    The same instance drives both the backtester and the live runner, so
    there is exactly one implementation of the rules.
    """

    def __init__(self, settings):
        settings.validate()
        self.s = settings
        self.tz = ZoneInfo(settings.tzIn)
        self.or_window = SessionWindow.parse(settings.orSess)
        self.sig_window = SessionWindow.parse(settings.sigSess)

        # --- price series (Pine's built-in open/high/low/close) ---
        self.open = Series()
        self.high = Series()
        self.low = Series()
        self.close = Series()
        self.volume = Series()
        self.atr_series = Series()
        self.times: List[datetime] = []

        self._atr = ATR(settings.atrLen)
        self.bar_index = -1

        # Hard square-off, in the exchange's timezone rather than the session's.
        self.sq_tz = None
        self.sq_minute = None
        if getattr(settings, "sqOffTime", ""):
            hh, mm = (int(x) for x in settings.sqOffTime.split(":"))
            self.sq_minute = hh * 60 + mm
            self.sq_tz = ZoneInfo(settings.sqOffTz)

        # --- session membership carried one bar back ---
        self._in_or_prev = False
        self._in_sig_prev = False

        # --- opening range (Pine: orH, orL, orLocked, orStart, tookL, tookS) ---
        self.orH = NA
        self.orL = NA
        self.orLocked = False
        self.orStart = NA
        self.tookL = False
        self.tookS = False

        # --- trade state (Pine: pos, ePx, sPx, p1..p3, h1..h3) ---
        self.pos = 0
        self.ePx = NA
        self.sPx = NA
        self.p1 = NA
        self.p2 = NA
        self.p3 = NA
        self.h1 = False
        self.h2 = False
        self.h3 = False

        # --- trailing stop (addition; inert when useTrail is False) ---
        self.iSPx = NA          # the stop as first set, so R is measured from it
        self.trailHigh = NA     # best price reached since entry
        self.trailLow = NA

        # --- FVG stores (Pine: buTop/buBot/buBar and beTop/beBot/beBar) ---
        self.bull_fvgs: List[FVG] = []
        self.bear_fvgs: List[FVG] = []

        # --- per-bar scratch, exposed for the dashboard ---
        self.pip = NA
        self.atr = NA
        self.inOR = False
        self.inSig = False
        self.orRange = NA
        self.rangeOk = False
        self.entry_time: Optional[datetime] = None

        self.events: List[Event] = []

    # -- helpers -----------------------------------------------------------
    def _pip_for(self, close_price: float) -> float:
        """Pine's `pip` switch. Note "Auto" is evaluated per bar, not once."""
        mode = self.s.pipMode
        if mode == "0.0001":
            return 0.0001
        if mode == "0.01":
            return 0.01
        if mode == "0.1":
            return 0.1
        if mode == "1":
            return 1.0
        return 0.01 if close_price > 20 else 0.0001

    def _to_pips(self, price_distance) -> str:
        return format_pips(price_distance, self.pip)

    def _long_stop(self, e: float) -> float:
        s = self.s
        if s.slMode == "ATR":
            return e - self.atr * s.atrMult
        if s.slMode == "Opposite range level":
            return self.orL
        if s.slMode == "FVG far edge":
            bull_bot = self.bull_fvg_bot
            return self.orL if na(bull_bot) else pine_min(bull_bot, e - s.mintick)
        return e - s.slPips * self.pip

    def _short_stop(self, e: float) -> float:
        s = self.s
        if s.slMode == "ATR":
            return e + self.atr * s.atrMult
        if s.slMode == "Opposite range level":
            return self.orH
        if s.slMode == "FVG far edge":
            bear_top = self.bear_fvg_top
            return self.orH if na(bear_top) else pine_max(bear_top, e + s.mintick)
        return e + s.slPips * self.pip

    @property
    def bull_fvg_bot(self) -> float:
        return self.bull_fvgs[-1].bot if self.bull_fvgs else NA

    @property
    def bear_fvg_top(self) -> float:
        return self.bear_fvgs[-1].top if self.bear_fvgs else NA

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        self._bar_events.append(event)

    def _enter_long(self, e: float, high: float, moment) -> bool:
        s = self.s
        st = self._long_stop(e)
        risk = e - st
        if not risk > 0:
            return False
        self.ePx, self.sPx = e, st
        self.p1 = e + risk * s.r1
        self.p2 = e + risk * s.r2
        self.p3 = e + risk * s.r3
        self.pos = 1
        self.h1 = self.h2 = self.h3 = False
        self.tookL = True
        self.entry_time = moment
        self.iSPx = st
        self.trailHigh = high
        self.trailLow = NA
        self._emit(
            Event(
                EventType.ENTRY, moment, self.bar_index, self._msg("BUY"),
                side="BUY", price=e, stop=st,
                t1=self.p1, t2=self.p2, t3=self.p3, risk=risk,
            )
        )
        return True

    def _enter_short(self, e: float, low: float, moment) -> bool:
        s = self.s
        st = self._short_stop(e)
        risk = st - e
        if not risk > 0:
            return False
        self.ePx, self.sPx = e, st
        self.p1 = e - risk * s.r1
        self.p2 = e - risk * s.r2
        self.p3 = e - risk * s.r3
        self.pos = -1
        self.h1 = self.h2 = self.h3 = False
        self.tookS = True
        self.entry_time = moment
        self.iSPx = st
        self.trailLow = low
        self.trailHigh = NA
        self._emit(
            Event(
                EventType.ENTRY, moment, self.bar_index, self._msg("SELL"),
                side="SELL", price=e, stop=st,
                t1=self.p1, t2=self.p2, t3=self.p3, risk=risk,
            )
        )
        return True

    def _close_carried_position(self, bar: Bar) -> None:
        """Square off a position the previous session left open.

        Only relevant when a square-off time is configured. With sqOffTime
        disabled the indicator's own overnight carry is preserved.
        """
        if self.sq_minute is None or self.pos == 0 or not self.times:
            return
        previous = self.times[-1]
        if bar.time.astimezone(self.sq_tz).date() == previous.astimezone(self.sq_tz).date():
            return
        last_close = self.close[0]
        self._emit(
            Event(
                EventType.SESSION_EXIT, previous, self.bar_index,
                "SQUARE OFF %s (session ended, no bar past %s)"
                % (self._fmt(last_close), self.s.sqOffTime),
                side="BUY" if self.pos == 1 else "SELL",
                price=last_close, closes_position=True,
            )
        )
        self.pos = 0

    def _past_square_off(self, moment: datetime) -> bool:
        """Is this bar at or past the square-off wall-clock time?"""
        if self.sq_minute is None:
            return False
        local = moment.astimezone(self.sq_tz)
        return local.hour * 60 + local.minute >= self.sq_minute

    # -- trailing stop -----------------------------------------------------
    def _update_trail(self, moment, high: float, low: float) -> None:
        """Tighten the stop after the bar's target/stop checks have run.

        Deliberately last in the bar: a stop raised by this bar's high must not
        also be tested against this bar's low, because the intrabar order of
        those two extremes is unknowable.  A tightened stop therefore takes
        effect from the next bar onward.

        The stop only ever moves toward price.  Targets never move.
        """
        s = self.s
        if not s.useTrail or self.pos == 0 or na(self.ePx) or na(self.iSPx):
            return

        risk = abs(self.ePx - self.iSPx)
        if risk <= 0:
            return
        new_stop = self.sPx
        reason = ""

        if self.pos == 1:
            self.trailHigh = high if na(self.trailHigh) else max(self.trailHigh, high)
            if s.trailMode in ("Targets", "Both"):
                if s.trailStep and self.h2 and not na(self.p1) and self.p1 > new_stop:
                    new_stop, reason = self.p1, "T2 hit, stop to T1"
                elif s.trailBE and self.h1 and self.ePx > new_stop:
                    new_stop, reason = self.ePx, "T1 hit, stop to breakeven"
            if s.trailMode in ("ATR", "Both") and not na(self.atr):
                if (self.trailHigh - self.ePx) >= risk * s.trailStartR:
                    candidate = self.trailHigh - self.atr * s.trailAtrMult
                    if candidate > new_stop:
                        new_stop, reason = candidate, "ATR trail"
            if new_stop > self.sPx:
                self._move_stop(moment, new_stop, reason)

        elif self.pos == -1:
            self.trailLow = low if na(self.trailLow) else min(self.trailLow, low)
            if s.trailMode in ("Targets", "Both"):
                if s.trailStep and self.h2 and not na(self.p1) and self.p1 < new_stop:
                    new_stop, reason = self.p1, "T2 hit, stop to T1"
                elif s.trailBE and self.h1 and self.ePx < new_stop:
                    new_stop, reason = self.ePx, "T1 hit, stop to breakeven"
            if s.trailMode in ("ATR", "Both") and not na(self.atr):
                if (self.ePx - self.trailLow) >= risk * s.trailStartR:
                    candidate = self.trailLow + self.atr * s.trailAtrMult
                    if candidate < new_stop:
                        new_stop, reason = candidate, "ATR trail"
            if new_stop < self.sPx:
                self._move_stop(moment, new_stop, reason)

    def _move_stop(self, moment, new_stop: float, reason: str) -> None:
        old = self.sPx
        self.sPx = new_stop
        self._emit(
            Event(
                EventType.STOP_MOVED, moment, self.bar_index,
                "stop %s -> %s (%s)" % (self._fmt(old), self._fmt(new_stop), reason),
                side="BUY" if self.pos == 1 else "SELL",
                price=new_stop, stop=new_stop,
            )
        )

    def _msg(self, side: str) -> str:
        """Pine's alert text `msg(side)`."""
        return (
            "%s @ %s | SL %s (%s pips) | T1 %s | T2 %s | T3 %s"
            % (
                side,
                self._fmt(self.ePx),
                self._fmt(self.sPx),
                self._to_pips(abs(self.ePx - self.sPx)),
                self._fmt(self.p1),
                self._fmt(self.p2),
                self._fmt(self.p3),
            )
        )

    def _fmt(self, price) -> str:
        """Pine `str.tostring(px, format.mintick)`."""
        if na(price):
            return "-"
        decimals = max(0, len(str(self.s.mintick).split(".")[-1])) if "." in str(self.s.mintick) else 0
        return "%.*f" % (decimals, price)

    # =====================================================================
    #  One bar
    # =====================================================================
    def on_bar(self, bar: Bar) -> List[Event]:
        self._bar_events: List[Event] = []
        s = self.s

        # A session can end without ever printing a bar past the square-off
        # time -- Angel's 5-minute feed stops at 15:10 for many symbols. Catch
        # that here: if the day has rolled over with a position still open,
        # close it at the last price the previous session actually printed.
        self._close_carried_position(bar)

        # ---- bar bookkeeping --------------------------------------------
        self.bar_index += 1
        # Session membership is decided in the strategy's timezone, but events
        # are stamped in the bar's own (the exchange's) -- an NSE trader should
        # read "12:50", not the London "08:20" that means the same instant.
        session_moment = bar.time.astimezone(self.tz)
        moment = bar.time
        self.times.append(moment)
        self.open.push(bar.open)
        self.high.push(bar.high)
        self.low.push(bar.low)
        self.close.push(bar.close)
        self.volume.push(bar.volume)

        high, low, close = bar.high, bar.low, bar.close

        # ---- atr = ta.atr(atrLen), pip ----------------------------------
        self.atr = self._atr.update(high, low, close)
        self.atr_series.push(self.atr)
        atr = self.atr
        self.pip = self._pip_for(close)

        # ---- session handling -------------------------------------------
        inOR = self.or_window.contains(session_moment)
        inSig = self.sig_window.contains(session_moment)
        self.inOR, self.inSig = inOR, inSig

        orNew = inOR and not self._in_or_prev
        sessEnd = (not inSig) and self._in_sig_prev

        # ---- opening range high / low -----------------------------------
        if orNew:
            self.orH = NA
            self.orL = NA
            self.orLocked = False
            self.orStart = self.bar_index
            self.tookL = False
            self.tookS = False

        if inOR:
            self.orH = high if na(self.orH) else max(self.orH, high)
            self.orL = low if na(self.orL) else min(self.orL, low)

        # the range locks the moment the opening window finishes
        if (not inOR) and (not na(self.orH)) and (not self.orLocked):
            self.orLocked = True
            self._emit(
                Event(
                    EventType.RANGE_LOCKED,
                    moment,
                    self.bar_index,
                    "Range locked  H %s / L %s  (%s pips)"
                    % (
                        self._fmt(self.orH),
                        self._fmt(self.orL),
                        format_pips(self.orH - self.orL, self.pip),
                    ),
                )
            )

        orRange = self.orH - self.orL
        self.orRange = orRange

        # ---- 5-min FVG engine -------------------------------------------
        # Bullish FVG : low[0]  > high[2]  -> zone high[2] .. low[0]
        # Bearish FVG : high[0] < low[2]   -> zone high[0] .. low[2]
        minGap = atr * s.fvgAtrX
        dispOk = (not s.useDisp) or abs(self.close[1] - self.open[1]) >= self.atr_series[1] * s.dispX

        newBull = self.bar_index > 2 and low > self.high[2] and (low - self.high[2]) >= minGap and dispOk
        newBear = self.bar_index > 2 and high < self.low[2] and (self.low[2] - high) >= minGap and dispOk

        if newBull:
            self.bull_fvgs.append(FVG(top=low, bot=self.high[2], bar=self.bar_index))
        if newBear:
            self.bear_fvgs.append(FVG(top=self.low[2], bot=high, bar=self.bar_index))

        # drop mitigated / expired FVGs (iterate backwards so removal is safe)
        for i in range(len(self.bull_fvgs) - 1, -1, -1):
            f = self.bull_fvgs[i]
            mitigated = s.fvgUnmit and self.bar_index > f.bar and low <= f.bot
            expired = self.bar_index - f.bar > s.fvgLB
            if mitigated or expired:
                del self.bull_fvgs[i]

        for i in range(len(self.bear_fvgs) - 1, -1, -1):
            f = self.bear_fvgs[i]
            mitigated = s.fvgUnmit and self.bar_index > f.bar and high >= f.top
            expired = self.bar_index - f.bar > s.fvgLB
            if mitigated or expired:
                del self.bear_fvgs[i]

        hasBull = len(self.bull_fvgs) > 0
        hasBear = len(self.bear_fvgs) > 0

        # ---- breakout signals -------------------------------------------
        buf = orRange * s.bufPct / 100.0
        lvlUp = self.orH + buf
        lvlDn = self.orL - buf

        close_mode = s.brkMode == "Close beyond level"
        srcH_now, srcH_prev = (close, self.close[1]) if close_mode else (high, self.high[1])
        srcL_now, srcL_prev = (close, self.close[1]) if close_mode else (low, self.low[1])

        # NB: previous bar's source against the CURRENT bar's level, as in Pine.
        if s.confirmBars <= 1:
            upTrig = srcH_now > lvlUp and srcH_prev <= lvlUp
            dnTrig = srcL_now < lvlDn and srcL_prev >= lvlDn
        else:
            # The level must have been cleared confirmBars-1 bars ago and held
            # at every close since, so a single-bar poke never triggers.
            # N bars closed beyond the level, and the bar before those inside
            # it. N = 1 reduces to the plain Pine cross.
            n = s.confirmBars
            src_h = self.close if close_mode else self.high
            src_l = self.close if close_mode else self.low
            upTrig = all(src_h[k] > lvlUp for k in range(n)) and src_h[n] <= lvlUp
            dnTrig = all(src_l[k] < lvlDn for k in range(n)) and src_l[n] >= lvlDn

        # -- fakeout filters (all inert at their default values) -----------
        bar_range = high - low
        if s.strongClose > 0 and bar_range > 0:
            up_close = (close - low) / bar_range
            upTrig = upTrig and up_close >= s.strongClose
            dnTrig = dnTrig and (high - close) / bar_range >= s.strongClose

        if s.volMult > 0:
            recent = [self.volume[k] for k in range(1, s.volLen + 1)]
            recent = [v for v in recent if not na(v)]
            avg = sum(recent) / len(recent) if recent else NA
            vol_ok = (not na(avg)) and avg > 0 and bar.volume >= avg * s.volMult
            upTrig = upTrig and vol_ok
            dnTrig = dnTrig and vol_ok

        rangeOk = orRange > 0 and (s.minPips <= 0 or orRange >= s.minPips * self.pip)
        if s.minRangePct > 0:
            mid = (self.orH + self.orL) / 2.0
            rangeOk = rangeOk and (not na(mid)) and mid > 0 and \
                (orRange / mid * 100.0) >= s.minRangePct
        self.rangeOk = bool(rangeOk)
        ready = self.orLocked and (not na(self.orH)) and (not na(self.orL)) and rangeOk and inSig
        okBar = bar.confirmed if s.confirm else True

        buySig = ready and okBar and s.allowL and upTrig and ((not s.useFvg) or hasBull) and ((not s.oncePer) or (not self.tookL))
        sellSig = ready and okBar and s.allowS and dnTrig and ((not s.useFvg) or hasBear) and ((not s.oncePer) or (not self.tookS))

        # Past the square-off there is no point opening a trade that must be
        # closed minutes later, so the gate blocks entries as well as holding.
        past_sqoff = self._past_square_off(bar.time)

        fireLong = buySig and self.pos == 0 and not past_sqoff
        fireShort = sellSig and self.pos == 0 and not past_sqoff

        # ---- entries -----------------------------------------------------
        if fireLong:
            self._enter_long(close, high, moment)
        if fireShort:
            self._enter_short(close, low, moment)

        # ---- target / stop-loss hit markers ------------------------------
        if self.pos == 1 and not fireLong:
            if (not self.h1) and high >= self.p1:
                self.h1 = True
                self._target_event(moment, 1, self.p1, self.p1 - self.ePx, closes=False)
            if (not self.h2) and high >= self.p2:
                self.h2 = True
                self._target_event(moment, 2, self.p2, self.p2 - self.ePx, closes=False)
            if (not self.h3) and high >= self.p3:
                self.h3 = True
                self._target_event(moment, 3, self.p3, self.p3 - self.ePx, closes=True)
                self.pos = 0
            if self.pos == 1 and low <= self.sPx:
                self._stop_event(moment, self.sPx, self.ePx - self.sPx)
                self.pos = 0
            # Trail last, so this bar's high cannot tighten the stop that this
            # bar's low was just tested against.
            self._update_trail(moment, high, low)

        if self.pos == -1 and not fireShort:
            if (not self.h1) and low <= self.p1:
                self.h1 = True
                self._target_event(moment, 1, self.p1, self.ePx - self.p1, closes=False)
            if (not self.h2) and low <= self.p2:
                self.h2 = True
                self._target_event(moment, 2, self.p2, self.ePx - self.p2, closes=False)
            if (not self.h3) and low <= self.p3:
                self.h3 = True
                self._target_event(moment, 3, self.p3, self.ePx - self.p3, closes=True)
                self.pos = 0
            if self.pos == -1 and high >= self.sPx:
                self._stop_event(moment, self.sPx, self.sPx - self.ePx)
                self.pos = 0
            self._update_trail(moment, high, low)

        # ---- same-bar reversal -------------------------------------------
        # Pine evaluates entries before exits, so an opposite-side signal that
        # lands on the same bar as the stop is dropped -- and because entry
        # needs a *cross*, it never comes back. Optionally take it now.
        if s.reentrySameBar and self.pos == 0 and not past_sqoff:
            if buySig and not fireLong:
                self._enter_long(close, high, moment)
            elif sellSig and not fireShort:
                self._enter_short(close, low, moment)

        # ---- hard square-off ---------------------------------------------
        if past_sqoff and self.pos != 0:
            self._emit(
                Event(
                    EventType.SESSION_EXIT, moment, self.bar_index,
                    "SQUARE OFF %s (%s %s)"
                    % (self._fmt(close), s.sqOffTime, s.sqOffTz),
                    side="BUY" if self.pos == 1 else "SELL",
                    price=close, closes_position=True,
                )
            )
            self.pos = 0

        # ---- end of signal window ----------------------------------------
        if sessEnd and s.exitEnd and self.pos != 0:
            self._emit(
                Event(
                    EventType.SESSION_EXIT, moment, self.bar_index,
                    "SESSION EXIT %s" % self._fmt(close),
                    side="BUY" if self.pos == 1 else "SELL",
                    price=close, closes_position=True,
                )
            )
            self.pos = 0

        self._in_or_prev = inOR
        self._in_sig_prev = inSig
        return self._bar_events

    # -- event helpers -----------------------------------------------------
    def _target_event(self, moment, n, price, gain, closes):
        self._emit(
            Event(
                EventType.TARGET_HIT, moment, self.bar_index,
                "T%d hit +%sp @ %s" % (n, self._to_pips(gain), self._fmt(price)),
                side="BUY" if self.pos == 1 else "SELL",
                price=price, target_no=n, closes_position=closes,
            )
        )

    def _stop_event(self, moment, price, loss):
        # After trailing, the stop can sit in profit -- reporting that as a
        # loss (and printing "--316.8p") would be plainly wrong.
        label = ("-%sp" % self._to_pips(loss)) if loss >= 0 else \
                ("+%sp" % self._to_pips(-loss))
        self._emit(
            Event(
                EventType.STOP_HIT, moment, self.bar_index,
                "SL hit %s @ %s" % (label, self._fmt(price)),
                side="BUY" if self.pos == 1 else "SELL",
                price=price, closes_position=True,
            )
        )

    # -- dashboard ---------------------------------------------------------
    def snapshot(self) -> dict:
        """The Pine dashboard table, as data."""
        status = "LONG" if self.pos == 1 else "SHORT" if self.pos == -1 else "FLAT"
        return {
            "session": "%s  %s" % (self.s.orSess, self.s.tzIn),
            "range_high": self.orH,
            "range_low": self.orL,
            "range_width_pips": format_pips(self.orRange, self.pip),
            "range_ok": self.rangeOk,
            "fvg_bull": len(self.bull_fvgs),
            "fvg_bear": len(self.bear_fvgs),
            "status": status,
            "entry": self.ePx,
            "stop": self.sPx,
            "initial_stop": self.iSPx,
            "trailed": (not na(self.iSPx)) and self.sPx != self.iSPx,
            "targets": (self.p1, self.p2, self.p3),
            "hits": (self.h1, self.h2, self.h3),
            "took_long": self.tookL,
            "took_short": self.tookS,
            "atr": self.atr,
            "in_signal_window": self.inSig,
        }
