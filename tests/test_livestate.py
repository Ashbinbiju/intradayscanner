"""
Live-mode bookkeeping (orbfvg/livestate.py): square-off safety net, display
times, targets read from bars, open/skipped split and the saved watchlist.

Run with:  python tests/test_livestate.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orbfvg import livestate as LS
from orbfvg.backtest import BacktestTrade
from orbfvg.strategy import Bar

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def at(hh, mm, day=2):
    return datetime(2026, 11, day, hh, mm, tzinfo=IST)


def bars_between(start, end, price=100.0, step=0.0):
    out, t, p = [], start, price
    while t <= end:
        out.append(Bar(time=t, open=p, high=p + 0.5, low=p - 0.5, close=p, volume=1000))
        t += timedelta(minutes=5)
        p += step
    return out


def trade(side="BUY", entry=100.0, stop=98.0, when=at(9, 30)):
    d = 1 if side == "BUY" else -1
    r = abs(entry - stop)
    t = BacktestTrade(side=side, entry_time=when, entry=entry, stop=stop,
                      t1=entry + d * r, t2=entry + d * 2 * r, t3=entry + d * 3 * r, risk=r)
    t.symbol = "TEST"
    return t


print("Square-off safety net")
t = trade()
bars = bars_between(at(9, 15), at(15, 10), price=101.0)
closed = LS.settle_open_trades([t], {"TEST": bars}, "15:00", IST, at(15, 12))
check("open trade closes at the 15:00 bar", closed == [t] and t.exit_time == at(15, 0))
check("exit at that bar's open", t.exit == 101.0)
check("reason is SESSION_END", t.reason == "SESSION_END")

t = trade()
bars = bars_between(at(9, 15), at(14, 55), price=101.0)   # feed has not delivered 15:00
closed = LS.settle_open_trades([t], {"TEST": bars}, "15:00", IST, at(15, 1))
check("15:00 bar missing, 1 minute after the cut: stays open", not closed and t.exit != t.exit)
closed = LS.settle_open_trades([t], {"TEST": bars}, "15:00", IST, at(15, 7))
check("15:00 bar missing, a full bar later: estimated from the last close",
      closed == [t] and t.reason == "SESSION_END_EST" and t.exit == 101.0)
check("estimated exit stamped at the last bar's close", t.exit_time == at(15, 0))

t = trade()
bars = bars_between(at(9, 15), at(13, 0), price=101.0)
closed = LS.settle_open_trades([t], {"TEST": bars}, "15:00", IST, at(13, 2))
check("before the cut-off nothing is touched", not closed and t.exit != t.exit)

t = trade()
t.exit, t.exit_time, t.reason = 99.0, at(11, 0), "SL"
closed = LS.settle_open_trades([t], {"TEST": bars_between(at(9, 15), at(15, 10))},
                               "15:00", IST, at(15, 12))
check("closed trades are left alone", not closed and t.reason == "SL" and t.exit == 99.0)

t = trade()
closed = LS.settle_open_trades([t], {"TEST": bars_between(at(9, 15), at(14, 0))},
                               "15:00", IST, at(10, 0, day=3))
check("a past day with no 15:00 bar is estimated", closed == [t] and t.reason == "SESSION_END_EST")

check("no square-off time configured -> nothing settled",
      LS.settle_open_trades([trade()], {"TEST": bars_between(at(9, 15), at(15, 10))},
                            "", IST, at(15, 12)) == [])

print("Display times")
check("event inside a bar shows the bar close", LS.display_time(at(9, 25), False) == "09:30")
check("fill at a bar open shows that open", LS.display_time(at(13, 0), True) == "13:00")
t = trade(when=at(13, 0))
check("Trend Breakout entry 13:00 shows 13:00, not 13:05", LS.entry_display(t, True) == "13:00")
check("ORB entry keeps the bar-close convention", LS.entry_display(t, False) == "13:05")
t.exit_time, t.reason = at(15, 0), "SESSION_END"
check("square-off at 15:00 shows 15:00, not 15:05", LS.exit_display(t, True) == "15:00")
t.exit_time, t.reason = at(14, 20), "SL"
check("stop inside a bar shows the bar close", LS.exit_display(t, True) == "14:25")
t.exit_time = None
check("no exit -> 'open'", LS.exit_display(t, True) == "open")

print("Targets from bars")
t = trade(entry=100.0, stop=98.0, when=at(9, 30))      # T1 102, T2 104, T3 106
bars = bars_between(at(9, 30), at(10, 0), price=100.0)
bars.append(Bar(time=at(10, 5), open=101, high=102.3, low=100.8, close=101.5, volume=1))
check("T1 touched intrabar counts before any 15m close", LS.reached_targets(t, bars) == 1)
check("nothing touched before the fill", LS.reached_targets(t, bars_between(at(9, 15), at(9, 25), 103)) == 0)
s = trade(side="SELL", entry=100.0, stop=102.0)        # T1 98, T2 96
bars = [Bar(time=at(10, 0), open=99, high=99.5, low=95.9, close=96.5, volume=1)]
check("SELL side reads the lows (T2)", LS.reached_targets(s, bars) == 2)

print("Open R and the open/skipped split")
t = trade(entry=100.0, stop=98.0)
check("open R at 103 = +1.5R", abs(LS.open_r(t, 103.0) - 1.5) < 1e-9)
check("open R with a NaN price is None", LS.open_r(t, float("nan")) is None)
a, b, c = trade(), trade(), trade()
c.exit, c.exit_time, c.reason = 99.0, at(11, 0), "SL"
mine, others = LS.split_open([a, b, c], [a, c])
check("taken & open -> positions", mine == [a])
check("skipped & open -> listed separately", others == [b])

print("Saved watchlist")
with tempfile.TemporaryDirectory() as tmp:
    spec = {"source": "live", "buckets": ["high52Week", "volumeGainers"], "min_chg": 0}
    lock = LS.UniverseLock(tmp, "2026-11-02", spec)
    check("nothing saved yet", lock.load() is None)
    lock.save(["COFORGE", "NIACL"], {"NIACL": {"priceChangePct": -3.9, "scans": ["x", 1, {"bad": 1}]},
                                     "OTHER": {"priceChangePct": 1}}, at(9, 30))
    again = LS.UniverseLock(tmp, "2026-11-02",
                            {"min_chg": 0, "buckets": ["volumeGainers", "high52Week"], "source": "live"})
    data = again.load()
    check("a new session finds the same watchlist", data and data["symbols"] == ["COFORGE", "NIACL"])
    check("bucket order does not change the key", again.key == lock.key)
    check("screener facts kept for locked symbols only",
          data["details"] == {"NIACL": {"priceChangePct": -3.9, "scans": ["x", 1]}})
    check("a different list selection is a different watchlist",
          LS.UniverseLock(tmp, "2026-11-02", {"source": "live", "buckets": ["low52Week"]}).load() is None)
    check("another day is a different watchlist",
          LS.UniverseLock(tmp, "2026-11-03", spec).load() is None)
    lock.clear()
    check("clear removes it", LS.UniverseLock(tmp, "2026-11-02", spec).load() is None)

print()
if FAILURES:
    print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all passed")
