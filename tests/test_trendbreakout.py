"""
NSE Trend Breakout engine: checked against an independent transcription of
the Pine indicator, plus each session rule.

Run with:  python tests/test_trendbreakout.py
"""

import math
import os
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TrendBreakoutSettings
from orbfvg import backtest as bt
from orbfvg.strategy import Bar, EventType
from orbfvg.trendbreakout import EMA, TrendBreakoutStrategy

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def walk(days=60, minutes=5, seed=3, start=1000.0, drift=0.00012):
    """Random 09:15-15:30 sessions with a slow drift, so trends form."""
    rng = random.Random(seed)
    bars, price = [], start
    day = datetime(2026, 6, 1, tzinfo=IST)
    sign = 1
    while len({b.time.date() for b in bars}) < days:
        if day.weekday() < 5:
            if rng.random() < 0.08:
                sign = -sign
            t = day.replace(hour=9, minute=15)
            price *= 1 + rng.gauss(0, 0.004)          # overnight gap
            while t.hour * 60 + t.minute < 15 * 60 + 30:
                o = price
                c = o * (1 + sign * drift + rng.gauss(0, 0.0022))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.0009)))
                l = min(o, c) * (1 - abs(rng.gauss(0, 0.0009)))
                bars.append(Bar(time=t, open=o, high=h, low=l, close=c, volume=1000))
                price = c
                t += timedelta(minutes=minutes)
        day += timedelta(days=1)
    return bars


def aggregate(bars, minutes):
    out = []
    for b in bars:
        offset = (b.time.hour * 60 + b.time.minute - (9 * 60 + 15)) % minutes
        start = b.time - timedelta(minutes=offset)
        if out and out[-1].time == start:
            o = out[-1]
            out[-1] = Bar(time=start, open=o.open, high=max(o.high, b.high),
                          low=min(o.low, b.low), close=b.close, volume=o.volume + b.volume)
        else:
            out.append(Bar(time=start, open=b.open, high=b.high, low=b.low,
                           close=b.close, volume=b.volume))
    return out


def minute(t):
    t = t.astimezone(IST)
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------------------
#  Reference: the Pine script, transcribed bar by bar on finished candles.
#  Written separately from the engine, sharing no code with it.
# ---------------------------------------------------------------------------
def rma_series(values, n):
    out, seed, v = [], [], float("nan")
    for x in values:
        if v != v:
            seed.append(x)
            if len(seed) == n:
                v = sum(seed) / n
        else:
            v = (x + (n - 1) * v) / n
        out.append(v)
    return out


def atr_series(c, n):
    trs = []
    for i, b in enumerate(c):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            p = c[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - p), abs(b.low - p)))
    return rma_series(trs, n)


def ema_series(values, n):
    out, seed, v = [], [], float("nan")
    for x in values:
        if v != v:
            seed.append(x)
            if len(seed) == n:
                v = sum(seed) / n
        else:
            v = (2 * x + (n - 1) * v) / (n + 1)
        out.append(v)
    return out


def reference(feed, s):
    nan = float("nan")
    candles = aggregate(feed, s.tfMinutes)
    hours = aggregate(feed, s.htfMinutes)
    hf = ema_series([h.close for h in hours], s.fastLen)
    hs = ema_series([h.close for h in hours], s.slowLen)

    def trend_at(t):
        # value of the HTF bar containing t, offset by one: previous hour's numbers
        k = max(i for i, h in enumerate(hours) if h.time <= t)
        if k == 0 or hs[k - 1] != hs[k - 1]:
            return 0
        f, sl, cl = hf[k - 1], hs[k - 1], hours[k - 1].close
        return 1 if f > sl and cl > sl else -1 if f < sl and cl < sl else 0

    atr = atr_series(candles, s.atrLen)
    atr100 = atr_series(candles, s.atrSlowLen)
    tf = timedelta(minutes=s.tfMinutes)
    hm = lambda txt: int(txt[:2]) * 60 + int(txt[3:])
    ent, lst, ex = hm(s.entryFrom), hm(s.entryTo), hm(s.sqOffTime)

    tDir, tEntry, tSL, tTP, tRisk, tBar, tIsRe, tSig = 0, nan, nan, nan, nan, -1, False, nan
    reDir, reLvl, reBar = 0, nan, -1
    pdDir, pdC, pdX, pdA, pdBar = 0, nan, nan, nan, -1
    trades = []
    for i, c in enumerate(candles):
        hh = max(x.high for x in candles[i - s.brkLen:i]) if i >= s.brkLen else nan
        ll = min(x.low for x in candles[i - s.brkLen:i]) if i >= s.brkLen else nan
        w = s.swingLen + 1
        hi21 = max(x.high for x in candles[i - w + 1:i + 1]) if i >= w - 1 else nan
        lo21 = min(x.low for x in candles[i - w + 1:i + 1]) if i >= w - 1 else nan
        trend = trend_at(c.time)
        a, a100 = atr[i], atr100[i]
        end = c.time + tf
        new_day = i > 0 and c.time.date() != candles[i - 1].time.date()
        sq = new_day or minute(c.time) >= ex
        in_win = end.date() == c.time.date() and ent <= minute(end) <= lst and minute(end) < ex
        from_sw = nan
        if a == a and a > 0:
            from_sw = (c.close - lo21) / a if trend == 1 else (hi21 - c.close) / a
        late = s.useLate and from_sw == from_sw and from_sw >= s.lateAtr
        qual = (a == a and a > 0 and a100 == a100 and a / a100 >= s.minVol
                and hh == hh and (hh - ll) / a < s.maxRng)
        buy = in_win and trend == 1 and hh == hh and c.close > hh and qual
        sell = in_win and trend == -1 and ll == ll and c.close < ll and qual

        exPx, why = nan, ""
        if tDir != 0 and tEntry != tEntry and i >= tBar and sq:
            tDir = 0
        if tDir != 0 and tEntry != tEntry and i >= tBar:
            tEntry = c.open
            tSL = c.open - tDir * tRisk
            tTP = c.open + tDir * tRisk * s.tpR
            trades.append({"side": tDir, "fill": c.time, "entry": tEntry, "stop": tSL})
        if tDir != 0 and tEntry == tEntry and i >= tBar and sq:
            exPx, why = c.open, "SQ"
        if tDir != 0 and tEntry == tEntry and i >= tBar and exPx != exPx:
            if tDir == 1 and c.low <= tSL:
                exPx, why = tSL, "SL"
            elif tDir == 1 and c.high >= tTP:
                exPx, why = tTP, "TP"
            elif tDir == -1 and c.high >= tSL:
                exPx, why = tSL, "SL"
            elif tDir == -1 and c.low <= tTP:
                exPx, why = tTP, "TP"
        if tDir != 0 and tEntry == tEntry and exPx != exPx and ((tDir == 1 and sell) or (tDir == -1 and buy)):
            exPx, why = c.close, "REV"
        if exPx == exPx:
            trades[-1].update(exit=exPx, why=why, r=(exPx - tEntry) * tDir / tRisk)
            if why == "SL" and s.useRe and not tIsRe:
                reDir, reLvl, reBar = tDir, tSig, i
            else:
                reDir = 0
            tDir, tEntry = 0, nan
        if sq:
            reDir = pdDir = 0
        if reDir != 0 and (trend != reDir or i - reBar >= s.reBars):
            reDir = 0
        reSig = in_win and tDir == 0 and reDir != 0 and a == a and (
            c.close > reLvl and c.close > c.open if reDir == 1 else c.close < reLvl and c.close < c.open)
        if pdDir != 0 and i > pdBar:
            pdX = max(pdX, c.high) if pdDir == -1 else min(pdX, c.low)
            if trend != pdDir or i - pdBar > s.pbBars:
                pdDir = 0
        pbSig = in_win and tDir == 0 and not reSig and pdDir != 0 and i > pdBar and \
            abs(pdX - pdC) >= s.pbAtr * pdA and (
                c.close < pdC and c.close < c.open if pdDir == -1 else c.close > pdC and c.close > c.open)
        go = (buy or sell) and not late
        if (go or reSig or pbSig) and tDir == 0:
            d = reDir if reSig else pdDir if pbSig else 1 if buy else -1
            tIsRe = reSig
            reDir = pdDir = 0
            tDir, tEntry, tSig, tRisk, tBar = d, nan, c.close, a * s.slAtr, i + 1
        if (buy or sell) and late and tDir == 0 and pdDir == 0:
            pdDir, pdC, pdX, pdA, pdBar = (1 if buy else -1), c.close, (c.low if buy else c.high), a, i
    return trades


def engine_trades(feed, s):
    res = bt.run(feed, s, engine=TrendBreakoutStrategy)
    return res, [{"side": 1 if t.side == "BUY" else -1, "fill": t.entry_time,
                  "entry": t.entry, "stop": t.stop, "exit": t.exit, "r": t.r_multiple}
                 for t in res.trades]


def same_trades(a, b):
    if len(a) != len(b):
        return False, "count %d vs %d" % (len(a), len(b))
    for x, y in zip(a, b):
        if x["side"] != y["side"] or x["fill"] != y["fill"] or abs(x["entry"] - y["entry"]) > 1e-9:
            return False, "entry %s vs %s" % (x, y)
        if "exit" in y and abs(x["exit"] - y["exit"]) > 1e-9:
            return False, "exit %s vs %s" % (x, y)
    return True, ""


print("NSE Trend Breakout engine")

# -- EMA ----------------------------------------------------------------------
vals = [100 + math.sin(i / 7.0) * 5 + i * 0.1 for i in range(300)]
e = EMA(50)
mine = [e.update(v) for v in vals]
ref = ema_series(vals, 50)
check("ta.ema: na for 49 samples, SMA seed, then recursive",
      all(math.isnan(x) for x in mine[:49])
      and all(abs(x - y) < 1e-9 for x, y in zip(mine[49:], ref[49:])))

# -- the engine against the reference, several seeds and both timeframes ---------
total, ok_all, detail = 0, True, ""
for tf in (15, 5):
    for seed in range(1, 7):
        feed = walk(days=55, seed=seed)
        s = TrendBreakoutSettings(tfMinutes=tf)
        _, mine = engine_trades(feed, s)
        theirs = [t for t in reference(feed, s) if "exit" in t]
        mine = [t for t in mine if t["exit"] == t["exit"]]
        ok, why = same_trades(mine, theirs)
        total += len(mine)
        if not ok:
            ok_all, detail = False, "tf %d seed %d: %s" % (tf, seed, why)
check("trades were generated to compare", total > 60, "only %d" % total)
check("engine == straight Pine transcription (entries, exits) on 5m and 15m", ok_all, detail)

# -- a 15-minute feed gives the same trades as 5-minute bars built up ------------
feed5 = walk(days=50, seed=11)
s15 = TrendBreakoutSettings()
_, a = engine_trades(feed5, s15)
_, b = engine_trades(aggregate(feed5, 15), TrendBreakoutSettings(barMinutes=15))
ok, why = same_trades(a, b)
check("5m feed and 15m feed produce identical 15m trades", ok and len(a) > 5, why)

# -- session rules -------------------------------------------------------------
fills_ok = flat_ok = True
n = 0
for seed in range(1, 9):
    res, _ = engine_trades(walk(days=50, seed=seed), TrendBreakoutSettings())
    for t in res.trades:
        n += 1
        if not (9 * 60 + 30 <= minute(t.entry_time) <= 13 * 60):
            fills_ok = False
        if t.exit_time is None or t.exit_time.date() != t.entry_time.date() \
                or minute(t.exit_time) > 15 * 60 + 10:
            flat_ok = False
check("fills only between 09:30 and 13:00", fills_ok and n > 20, "n=%d" % n)
check("every trade closes the same day, by the 15:00 candle", flat_ok)

# -- the late-entry guard turns a far-from-swing breakout into a wait --------------
s_off = TrendBreakoutSettings(useLate=False)
s_on = TrendBreakoutSettings()
feed = walk(days=55, seed=4)
r_off, _ = engine_trades(feed, s_off)
r_on, _ = engine_trades(feed, s_on)
tags_on = [e.message for e in r_on.events if e.type is EventType.ENTRY]
check("late guard produces retest entries", any("(retest)" in m for m in tags_on))
check("late guard off produces none", not any("(retest)" in e.message for e in r_off.events
                                              if e.type is EventType.ENTRY))


# -- same-candle resolution, square-off, the 13:00 cut-off -----------------------
def armed(tf=15):
    e = TrendBreakoutStrategy(TrendBreakoutSettings(tfMinutes=tf, barMinutes=tf))
    e.trend = 1
    e.tDir, e.tEntry, e.tSL, e.tTP, e.tRisk, e.tSig = 1, 100.0, 98.0, 106.0, 2.0, 99.5
    e.tBar = 0
    e.prev_candle_day = datetime(2026, 9, 8, tzinfo=IST).date()
    return e


t0 = datetime(2026, 9, 8, 11, 0, tzinfo=IST)
e = armed()
ev = e.on_bar(Bar(time=t0, open=100, high=106.5, low=97.5, close=101))
check("stop and target in one candle: the stop is taken",
      [x.type for x in ev if x.closes_position] == [EventType.STOP_HIT])

e = armed()
ev = e.on_bar(Bar(time=t0, open=100, high=106.5, low=99.0, close=105))
check("target alone closes at 3R", any(x.type is EventType.TARGET_HIT and x.price == 106.0 for x in ev))

e = armed()
ev = e.on_bar(Bar(time=t0.replace(hour=15, minute=0), open=101.5, high=102, low=101, close=101.2))
check("square-off at the open of the 15:00 candle",
      any(x.type is EventType.SESSION_EXIT and x.price == 101.5 for x in ev))

e = armed()
e.tEntry = float("nan")                     # pending: signal fired, fill not yet done
e.tBar = 0
ev = e.on_bar(Bar(time=t0.replace(hour=15, minute=0), open=101.5, high=102, low=101, close=101.2))
check("a fill that would land at square-off is cancelled",
      not any(x.type is EventType.ENTRY for x in ev) and e.tDir == 0)

s = TrendBreakoutSettings()
e = TrendBreakoutStrategy(s)
c = type("C", (), {})()
for hhmm, expect in (("12:45", True), ("13:00", False), ("09:00", False), ("09:15", True)):
    hh, mm = (int(x) for x in hhmm.split(":"))
    start = t0.replace(hour=hh, minute=mm)
    c.start, c.end = start, start + timedelta(minutes=15)
    if e._in_window(c) != expect:
        check("entry window at %s" % hhmm, False)
        break
else:
    check("fills only 09:30-13:00: the 12:45 candle fills at 13:00, the 13:00 candle may not", True)

print("")
if FAILURES:
    print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all passed")
