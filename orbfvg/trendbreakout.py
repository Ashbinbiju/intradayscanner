"""
NSE Trend Breakout -- intraday (the Pine "NSE Trend Breakout Intraday v1.1").

A port of the TradingView indicator, rules unchanged:

  * Trend      1H EMA 50 vs EMA 200, plus the 1H close beyond the EMA 200,
               taken from the previous COMPLETED hour (no repaint)
  * BUY        uptrend and a signal candle CLOSES above the prior 40 candles'
               high; SELL is the mirror in a downtrend
  * Fill       at the NEXT candle's open; stop 1.5 x ATR(14) of the signal
               candle from the fill, target 3R; stop and target in one candle
               = stop
  * Filters    skip when ATR14 / ATR100 < 0.8 (too quiet) or the 40-candle
               range is wider than 10 ATR
  * Late guard a signal more than 4 ATR from the 21-candle swing waits up to
               10 candles for a 0.5 ATR pullback, then enters on a candle
               closing back beyond the signal price ("retest")
  * Re-entry   once, within 10 candles of a stop-out, when a candle closes
               back through the original signal price in the trend direction
  * Session    fills only 09:30-13:00 IST; everything flat at the open of the
               first candle starting at or after 15:00

Signals run on ``tfMinutes`` candles built from the 5-minute feed (5 = the
feed itself), aligned to 09:15 exactly as TradingView draws NSE candles. The
1-hour trend candles are built from the same feed, also aligned to 09:15.

Two things to know before reading results:

  * EMA 200 on 1-hour candles needs 200 completed hours -- about 29 trading
    sessions -- before the first trend reading. Feed at least that much
    history ahead of the window you care about (run.py does this for you).
  * Measured in TradingView on 37 NSE stocks, this engine is about break-even
    BEFORE costs (5m: -7R over 1,080 trades; 15m: 0R over 1,575), and the
    costs make it a loser. Treat it as a study tool until it shows an edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from .pine import NA, ATR, Series, na
from .strategy import Bar, Event, EventType


def _minutes(text: str) -> int:
    hh, _, mm = text.partition(":")
    return int(hh) * 60 + int(mm)


class EMA:
    """Pine ``ta.ema(src, length)``: na until ``length`` samples, seeded with
    their simple average, then ``alpha * src + (1 - alpha) * prev``."""

    __slots__ = ("length", "_seed", "value")

    def __init__(self, length: int):
        self.length = length
        self._seed: List[float] = []
        self.value = NA

    def update(self, src: float) -> float:
        if na(self.value):
            self._seed.append(src)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
        else:
            alpha = 2.0 / (self.length + 1)
            self.value = alpha * src + (1.0 - alpha) * self.value
        return self.value


@dataclass
class _Candle:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    last_time: datetime          # open time of the last feed bar folded in


class TrendBreakoutStrategy:
    """Bar-by-bar state machine. Feed it closed 5-minute bars in order."""

    def __init__(self, settings):
        settings.validate()
        self.s = s = settings
        self.tz = ZoneInfo(s.tzIn)
        self.tf = timedelta(minutes=s.tfMinutes)
        self.htf_minutes = max(s.htfMinutes, s.tfMinutes)
        self.htf = timedelta(minutes=self.htf_minutes)
        self.feed_bar = timedelta(minutes=s.barMinutes)
        self.anchor = _minutes(s.sessionStart)
        self.ent_min = _minutes(s.entryFrom)
        self.lst_min = _minutes(s.entryTo)
        self.ex_min = _minutes(s.sqOffTime)

        # -- candles ---------------------------------------------------------
        self.candle: Optional[_Candle] = None
        self.hcandle: Optional[_Candle] = None
        self.bar_index = -1                 # counts signal candles, like Pine
        self.prev_candle_day = None

        # -- 1H trend: values as of the last COMPLETED hour --------------------
        self.ema_fast = EMA(s.fastLen)
        self.ema_slow = EMA(s.slowLen)
        self.h_fast = self.h_slow = self.h_close = NA
        self.trend = 0

        # -- chart-timeframe series ----------------------------------------------
        self.atr14 = ATR(s.atrLen)
        self.atr100 = ATR(s.atrSlowLen)
        self.highs = Series()
        self.lows = Series()
        self.atr = NA
        self.next_buy = self.next_sell = NA     # dashboard: level that triggers next

        # -- position / pending state (names follow the Pine) ------------------------
        self.tDir = 0
        self.tEntry = self.tSL = self.tTP = self.tRisk = self.tSig = NA
        self.tBar = -1
        self.tIsRe = False
        self.tKind = 0
        self.reDir = 0
        self.reLvl = NA
        self.reBar = -1
        self.pdDir = 0
        self.pdC = self.pdX = self.pdA = NA
        self.pdBar = -1
        self.entry_time: Optional[datetime] = None
        self.touched = 0                    # 1R / 2R milestones reported so far

        self.events: List[Event] = []
        self._bar_events: List[Event] = []

    # -- helpers --------------------------------------------------------------
    def _fmt(self, price) -> str:
        return "-" if na(price) else "%.2f" % price

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        self._bar_events.append(event)

    def _side(self, d=None) -> str:
        return "BUY" if (d if d is not None else self.tDir) == 1 else "SELL"

    def _local_minute(self, moment: datetime) -> int:
        local = moment.astimezone(self.tz)
        return local.hour * 60 + local.minute

    def _bucket(self, moment: datetime, size_minutes: int) -> datetime:
        local = moment.astimezone(self.tz)
        minute = local.hour * 60 + local.minute
        offset = (minute - self.anchor) % size_minutes
        return (local - timedelta(minutes=offset)).replace(second=0, microsecond=0)

    def _tag(self) -> str:
        return " (re)" if self.tKind == 1 else " (retest)" if self.tKind == 2 else ""

    # -- 1H trend --------------------------------------------------------------
    def _close_hour(self, h: _Candle) -> None:
        self.h_fast = self.ema_fast.update(h.close)
        self.h_slow = self.ema_slow.update(h.close)
        self.h_close = h.close
        hf, hs, hc = self.h_fast, self.h_slow, self.h_close
        if na(hs):
            self.trend = 0
        elif hf > hs and hc > hs:
            self.trend = 1
        elif hf < hs and hc < hs:
            self.trend = -1
        else:
            self.trend = 0

    # -- session flags for a candle ---------------------------------------------
    def _flags(self, c: _Candle):
        bar_min = self._local_minute(c.start)
        day = c.start.astimezone(self.tz).date()
        new_day = self.prev_candle_day is not None and day != self.prev_candle_day
        sq_off = new_day or bar_min >= self.ex_min
        return bar_min, day, new_day, sq_off

    def _in_window(self, c: _Candle) -> bool:
        """Pine ``inWin``: the fill (this candle's close time) lands inside the
        entry window, on the same day."""
        fill_min = self._local_minute(c.end)
        same_day = c.end.astimezone(self.tz).date() == c.start.astimezone(self.tz).date()
        return same_day and self.ent_min <= fill_min <= self.lst_min and fill_min < self.ex_min

    # -- the open of a new signal candle: fills and the square-off -----------------
    def _on_candle_open(self, c: _Candle) -> None:
        s = self.s
        _, _, _, sq_off = self._flags(c)
        upcoming = self.bar_index + 1
        # A pending fill that would land at/after square-off (or on a new day) is cancelled.
        if self.tDir != 0 and na(self.tEntry) and upcoming >= self.tBar and sq_off:
            self.tDir = 0
        if self.tDir != 0 and na(self.tEntry) and upcoming >= self.tBar:
            d = self.tDir
            self.tEntry = c.open
            self.tSL = c.open - d * self.tRisk
            self.tTP = c.open + d * self.tRisk * s.tpR
            self.entry_time = c.start
            self.touched = 0
            r = self.tRisk
            t1 = c.open + d * r * min(1.0, s.tpR)
            t2 = c.open + d * r * min(2.0, s.tpR)
            self._emit(Event(
                EventType.ENTRY, c.start, upcoming,
                "%s%s %s | SL %s | TP %s (%gR) | signal close %s"
                % (self._side(), self._tag(), self._fmt(c.open), self._fmt(self.tSL),
                   self._fmt(self.tTP), s.tpR, self._fmt(self.tSig)),
                side=self._side(), price=c.open, stop=self.tSL,
                t1=t1, t2=t2, t3=self.tTP, risk=r))
        # Square-off: exit at the open of the first candle at/after the cut-off.
        if self.tDir != 0 and not na(self.tEntry) and upcoming >= self.tBar and sq_off:
            self._exit(c.start, c.open, "SQ", upcoming)

    # -- the close of a signal candle: everything else ---------------------------------
    def _on_candle_close(self, c: _Candle) -> None:
        s = self.s
        self.bar_index += 1
        bi = self.bar_index
        bar_min, day, new_day, sq_off = self._flags(c)

        # series on this candle
        atr = self.atr14.update(c.high, c.low, c.close)
        atr100 = self.atr100.update(c.high, c.low, c.close)
        self.atr = atr
        hh = max(self.highs.values[-s.brkLen:]) if len(self.highs) >= s.brkLen else NA
        ll = min(self.lows.values[-s.brkLen:]) if len(self.lows) >= s.brkLen else NA
        self.highs.push(c.high)
        self.lows.push(c.low)
        swing_n = s.swingLen + 1
        hi21 = max(self.highs.values[-swing_n:]) if len(self.highs) >= swing_n else NA
        lo21 = min(self.lows.values[-swing_n:]) if len(self.lows) >= swing_n else NA
        self.next_buy = max(self.highs.values[-s.brkLen:]) if len(self.highs) >= s.brkLen else NA
        self.next_sell = min(self.lows.values[-s.brkLen:]) if len(self.lows) >= s.brkLen else NA

        trend = self.trend
        in_win = self._in_window(c)
        from_sw = NA
        if not na(atr) and atr > 0:
            from_sw = (c.close - lo21) / atr if trend == 1 else (hi21 - c.close) / atr
        is_late = s.useLate and not na(from_sw) and from_sw >= s.lateAtr
        qual_ok = ((s.minVol <= 0 or (not na(atr100) and not na(atr) and atr / atr100 >= s.minVol))
                   and not na(atr) and atr > 0 and not na(hh) and (hh - ll) / atr < s.maxRng)
        buy = in_win and s.allowL and trend == 1 and not na(hh) and c.close > hh and qual_ok
        sell = in_win and s.allowS and trend == -1 and not na(ll) and c.close < ll and qual_ok

        # stop / target inside this candle (the fill candle included); stop first
        if self.tDir != 0 and not na(self.tEntry) and bi >= self.tBar:
            d = self.tDir
            if d == 1 and c.low <= self.tSL:
                self._exit(c.last_time, self.tSL, "SL", bi)
            elif d == 1 and c.high >= self.tTP:
                self._exit(c.last_time, self.tTP, "TP", bi)
            elif d == -1 and c.high >= self.tSL:
                self._exit(c.last_time, self.tSL, "SL", bi)
            elif d == -1 and c.low <= self.tTP:
                self._exit(c.last_time, self.tTP, "TP", bi)
        # 1R / 2R milestones (the Pine's dotted lines): reported, never closing
        if self.tDir != 0 and not na(self.tEntry) and bi >= self.tBar:
            d, best = self.tDir, (c.high if self.tDir == 1 else c.low)
            for n in (1, 2):
                level = self.tEntry + d * self.tRisk * n
                if n > self.touched and n < s.tpR and d * (best - level) >= 0:
                    self.touched = n
                    self._emit(Event(EventType.TARGET_HIT, c.last_time, bi,
                                     "%dR reached @ %s" % (n, self._fmt(level)),
                                     side=self._side(), price=level, target_no=n,
                                     closes_position=False))
        # an opposite signal closes the trade at this close
        if self.tDir != 0 and not na(self.tEntry) and ((self.tDir == 1 and sell) or (self.tDir == -1 and buy)):
            self._exit(c.last_time, c.close, "REV", bi)

        # nothing pending survives square-off or the overnight gap
        if sq_off:
            self.reDir = 0
            self.pdDir = 0
        if self.reDir != 0 and (trend != self.reDir or bi - self.reBar >= s.reBars):
            self.reDir = 0
        re_sig = (in_win and self.tDir == 0 and self.reDir != 0 and not na(atr)
                  and (c.close > self.reLvl and c.close > c.open if self.reDir == 1
                       else c.close < self.reLvl and c.close < c.open))

        if self.pdDir != 0 and bi > self.pdBar:
            self.pdX = max(self.pdX, c.high) if self.pdDir == -1 else min(self.pdX, c.low)
            if trend != self.pdDir or bi - self.pdBar > s.pbBars:
                self.pdDir = 0
        pb_sig = (in_win and self.tDir == 0 and not re_sig and self.pdDir != 0 and bi > self.pdBar
                  and abs(self.pdX - self.pdC) >= s.pbAtr * self.pdA
                  and (c.close < self.pdC and c.close < c.open if self.pdDir == -1
                       else c.close > self.pdC and c.close > c.open))
        go_sig = (buy or sell) and not is_late

        if (go_sig or re_sig or pb_sig) and self.tDir == 0:
            d = self.reDir if re_sig else self.pdDir if pb_sig else 1 if buy else -1
            self.tIsRe = re_sig
            self.tKind = 1 if re_sig else 2 if pb_sig else 0
            self.reDir = 0
            self.pdDir = 0
            self.tDir = d
            self.tEntry = self.tSL = self.tTP = NA
            self.tSig = c.close
            self.tRisk = atr * s.slAtr
            self.tBar = bi + 1

        if (buy or sell) and is_late and self.tDir == 0 and self.pdDir == 0:
            self.pdDir = 1 if buy else -1
            self.pdC = c.close
            self.pdX = c.low if buy else c.high
            self.pdA = atr
            self.pdBar = bi

        self.prev_candle_day = day

    def _exit(self, moment: datetime, price: float, why: str, bi: int) -> None:
        d = self.tDir
        if why == "TP":
            self._emit(Event(EventType.TARGET_HIT, moment, bi,
                             "TP hit @ %s" % self._fmt(price), side=self._side(),
                             price=price, target_no=3, closes_position=True))
        elif why == "SL":
            self._emit(Event(EventType.STOP_HIT, moment, bi,
                             "SL hit @ %s" % self._fmt(price), side=self._side(),
                             price=price, closes_position=True))
        else:
            label = "square off" if why == "SQ" else "opposite signal, out"
            self._emit(Event(EventType.SESSION_EXIT, moment, bi,
                             "%s @ %s" % (label, self._fmt(price)), side=self._side(),
                             price=price, closes_position=True,
                             reason="SESSION_END" if why == "SQ" else "REVERSED"))
        if why == "SL" and self.s.useRe and not self.tIsRe:
            self.reDir = d
            self.reLvl = self.tSig
            self.reBar = bi
        else:
            self.reDir = 0
        self.tDir = 0
        self.tEntry = NA

    # =====================================================================
    def on_bar(self, bar: Bar) -> List[Event]:
        self._bar_events = []
        moment = bar.time
        start = self._bucket(moment, self.s.tfMinutes)
        hstart = self._bucket(moment, self.htf_minutes)

        # A candle the feed never finished (a missing bar) closes as soon as a
        # bar from a later slot arrives -- late rather than lost.
        if self.candle is not None and start != self.candle.start:
            self._on_candle_close(self.candle)
            self.candle = None
        if self.hcandle is not None and hstart != self.hcandle.start:
            self._close_hour(self.hcandle)
            self.hcandle = None

        if self.hcandle is None:
            self.hcandle = _Candle(hstart, hstart + self.htf, bar.open, bar.high, bar.low,
                                   bar.close, moment)
        else:
            h = self.hcandle
            h.high, h.low, h.close, h.last_time = max(h.high, bar.high), min(h.low, bar.low), bar.close, moment

        if self.candle is None:
            self.candle = _Candle(start, start + self.tf, bar.open, bar.high, bar.low,
                                  bar.close, moment)
            self._on_candle_open(self.candle)
        else:
            c = self.candle
            c.high, c.low, c.close, c.last_time = max(c.high, bar.high), min(c.low, bar.low), bar.close, moment

        # Close the signal candle before the hour, so a candle ending on the
        # hour still reads the previous hour's trend (Pine lookahead + [1]).
        if moment + self.feed_bar >= self.candle.end:
            self._on_candle_close(self.candle)
            self.candle = None
        if moment + self.feed_bar >= self.hcandle.end:
            self._close_hour(self.hcandle)
            self.hcandle = None
        return self._bar_events

    # -- dashboard ---------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "status": "LONG" if self.tDir == 1 and not na(self.tEntry)
            else "SHORT" if self.tDir == -1 and not na(self.tEntry)
            else "PENDING" if self.tDir != 0 else "FLAT",
            "trend": "UP" if self.trend == 1 else "DOWN" if self.trend == -1 else "-",
            "ema_fast": self.h_fast,
            "ema_slow": self.h_slow,
            "entry": self.tEntry,
            "stop": self.tSL,
            "target": self.tTP,
            "signal": self.tSig,
            "next_buy": self.next_buy if self.trend == 1 else NA,
            "next_sell": self.next_sell if self.trend == -1 else NA,
            "waiting_pullback": self.pdDir != 0,
            "atr": self.atr,
        }
