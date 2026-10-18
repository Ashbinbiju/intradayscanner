"""
Supertrend engine: the indicator maths and each intraday rule.

Run with:  python tests/test_supertrend.py
"""

import math
import os
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SupertrendSettings
from orbfvg import backtest as bt
from orbfvg.strategy import Bar, EventType
from orbfvg.supertrend import SupertrendCalc, SupertrendStrategy

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def walk(days=6, minutes=5, seed=7, start=1000.0):
    """A random 5-minute (or N-minute) session walk, 09:15-15:30 each weekday."""
    rng = random.Random(seed)
    bars, price = [], start
    day = datetime(2026, 9, 7, tzinfo=IST)
    while len({b.time.date() for b in bars}) < days:
        if day.weekday() < 5:
            t = day.replace(hour=9, minute=15)
            while t.hour * 60 + t.minute < 15 * 60 + 30:
                o = price
                c = o * (1 + rng.gauss(0, 0.0025))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
                l = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
                bars.append(Bar(time=t, open=o, high=h, low=l, close=c, volume=1000))
                price = c
                t += timedelta(minutes=minutes)
        day += timedelta(days=1)
    return bars


def aggregate(bars, minutes=15):
    out = []
    for b in bars:
        local = b.time.hour * 60 + b.time.minute - (9 * 60 + 15)
        start = b.time - timedelta(minutes=local % minutes)
        if out and out[-1].time == start:
            o = out[-1]
            out[-1] = Bar(time=start, open=o.open, high=max(o.high, b.high),
                          low=min(o.low, b.low), close=b.close, volume=o.volume + b.volume)
        else:
            out.append(Bar(time=start, open=b.open, high=b.high, low=b.low,
                           close=b.close, volume=b.volume))
    return out


def reference_supertrend(bars, factor, period):
    """Straight transcription of Pine's ta.supertrend, on lists."""
    trs, atr, out = [], [], []
    for i, b in enumerate(bars):
        pc = bars[i - 1].close if i else None
        tr = b.high - b.low if pc is None else max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
        trs.append(tr)
        if len(trs) < period:
            atr.append(math.nan)
        elif len(trs) == period:
            atr.append(sum(trs) / period)
        else:
            atr.append((atr[-1] * (period - 1) + tr) / period)
    lb_p = ub_p = st_p = math.nan
    for i, b in enumerate(bars):
        src = (b.high + b.low) / 2
        ub, lb = src + factor * atr[i], src - factor * atr[i]
        plb = 0.0 if math.isnan(lb_p) else lb_p
        pub = 0.0 if math.isnan(ub_p) else ub_p
        pc = bars[i - 1].close if i else math.nan
        lb = lb if (lb > plb or pc < plb) else plb
        ub = ub if (ub < pub or pc > pub) else pub
        if i == 0 or math.isnan(atr[i - 1]):
            d = 1
        elif st_p == ub_p:
            d = -1 if b.close > ub else 1
        else:
            d = 1 if b.close < lb else -1
        st = lb if d == -1 else ub
        out.append((st, d))
        lb_p, ub_p, st_p = lb, ub, st
    return out


def minute(t):
    return t.hour * 60 + t.minute


print("Supertrend engine")

# -- the maths ---------------------------------------------------------------
bars15 = aggregate(walk(days=10))
calc = SupertrendCalc(2.5, 14)
mine = [calc.update(b.high, b.low, b.close) for b in bars15]
ref = reference_supertrend(bars15, 2.5, 14)
same = all((math.isnan(a[0]) and math.isnan(b[0])) or abs(a[0] - b[0]) < 1e-9 for a, b in zip(mine, ref)) \
    and all(a[1] == b[1] for a, b in zip(mine, ref))
check("ta.supertrend matches a direct transcription", same)
check("the walk produces flips to test against",
      sum(1 for a, b in zip(ref, ref[1:]) if a[1] != b[1]) >= 5)

# -- 5-minute feed builds the same 15-minute candles -------------------------
bars5 = walk(days=10)
e5 = SupertrendStrategy(SupertrendSettings())
for b in bars5:
    e5.on_bar(b)
e15 = SupertrendStrategy(SupertrendSettings(barMinutes=15))
for b in aggregate(bars5):
    e15.on_bar(b)

def same_point(a, b):
    both_na = math.isnan(a[1]) and math.isnan(b[1])
    return a[0] == b[0] and a[2] == b[2] and (both_na or abs(a[1] - b[1]) < 1e-9)


check("5m feed and 15m feed give the same Supertrend",
      len(e5.history) == len(e15.history) > 200
      and all(same_point(a, b) for a, b in zip(e5.history, e15.history)))

# -- intraday rules, on several seeds ------------------------------------------
s = SupertrendSettings()
entries_ok = exits_same_day = flips_seen = flip_reason = True
n_trades = 0
for seed in range(1, 9):
    res = bt.run(walk(days=8, seed=seed), SupertrendSettings(), engine=SupertrendStrategy)
    for t in res.trades:
        n_trades += 1
        close_at = t.entry_time + timedelta(minutes=5)
        if not (minute(close_at) % 15 == 0 and 9 * 60 + 30 <= minute(close_at) <= 12 * 60 + 50):
            entries_ok = False
        if t.exit_time is None or t.exit_time.date() != t.entry_time.date() \
                or minute(t.exit_time) > 15 * 60:
            exits_same_day = False
        if t.reason == "FLIP" and t.exit_time is not None:
            # The flip exit happens at a signal candle's close.
            if minute(t.exit_time + timedelta(minutes=5)) % 15 != 0:
                flip_reason = False
check("trades were generated", n_trades > 20, "only %d" % n_trades)
check("entries only on 15m closes between 09:30 and 12:50", entries_ok)
check("every trade is flat by the 15:00 bar, same day", exits_same_day)
check("opposite-signal exits land on a signal close", flip_reason)


# -- same-bar resolution and the step trail ------------------------------------
def armed_engine(**overrides):
    e = SupertrendStrategy(SupertrendSettings(**overrides))
    e.pos, e.ePx, e.sPx, e.iSPx = 1, 100.0, 98.0, 98.0
    e.p1, e.p2, e.p3 = 102.0, 104.0, 106.0
    e.entry_index = -5
    return e


t0 = datetime(2026, 9, 8, 10, 0, tzinfo=IST)
e = armed_engine()
ev = e.on_bar(Bar(time=t0, open=100, high=106.5, low=97.5, close=101))
check("stop and T3 in one bar: the stop is taken",
      [x.type for x in ev if x.closes_position] == [EventType.STOP_HIT])

e = armed_engine(exitMode="T1")
ev = e.on_bar(Bar(time=t0, open=100, high=102.5, low=99.5, close=102))
check("exit T1 closes at T1", any(x.type is EventType.TARGET_HIT and x.closes_position
                                  and x.price == 102.0 for x in ev))

e = armed_engine(exitMode="Step trail")
e.on_bar(Bar(time=t0, open=100, high=102.5, low=99.5, close=102))
check("step trail: T1 moves the stop to entry", e.sPx == 100.0 and e.pos == 1, "stop %s" % e.sPx)
e.on_bar(Bar(time=t0 + timedelta(minutes=5), open=102, high=104.2, low=101.5, close=104))
check("step trail: T2 moves the stop to T1", e.sPx == 102.0, "stop %s" % e.sPx)

e = armed_engine()
ev = e.on_bar(Bar(time=t0, open=97.0, high=99, low=96.5, close=98.5))
check("a gap through the stop fills at the open",
      any(x.type is EventType.STOP_HIT and x.price == 97.0 for x in ev))

e = armed_engine()
ev = e.on_bar(Bar(time=t0.replace(hour=15, minute=0), open=100, high=100.5, low=99.5, close=100.2))
check("square-off at the 15:00 bar's close",
      any(x.type is EventType.SESSION_EXIT and x.price == 100.2 for x in ev))

print("")
if FAILURES:
    print("%d failure(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all passed")
