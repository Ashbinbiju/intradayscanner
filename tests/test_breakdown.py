"""
Breakdown-retest short: one test per rule in the setup.

Each check pins a single requirement, so a change that quietly loosens the
pattern shows up as a named failure rather than as different backtest numbers.
Run with:  python tests/test_breakdown.py
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import BreakdownSettings
from orbfvg.breakdown import BreakdownRetestStrategy
from orbfvg.strategy import Bar, EventType

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def settings(**overrides):
    s = BreakdownSettings()
    s.trendBars, s.trendPct = 10, 2.0
    s.consolMinBars, s.consolMaxBars, s.consolMaxPct = 4, 12, 1.5
    s.retestBars, s.retestTolPct = 8, 0.30
    s.sigSess = "0915-1500"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class Tape:
    """Builds a bar sequence and feeds it, collecting events."""

    def __init__(self, strategy, start=(9, 15), day=(2026, 9, 14)):
        self.st = strategy
        self.t = datetime(day[0], day[1], day[2], start[0], start[1], tzinfo=IST)
        self.events = []

    def bar(self, o, h, l, c, v=10000):
        evs = self.st.on_bar(Bar(time=self.t, open=o, high=h, low=l, close=c, volume=v))
        self.events.extend(evs)
        self.t += timedelta(minutes=5)
        return evs

    def rally(self, start, n, step):
        """A clean up-move: each bar closes above the last."""
        price = start
        for _ in range(n):
            self.bar(price, price + step, price - step * 0.2, price + step)
            price += step
        return price

    def chop(self, centre, n, width):
        """Sideways: alternating small bars inside a tight band."""
        for i in range(n):
            up = i % 2 == 0
            o = centre - width * 0.2 if up else centre + width * 0.2
            c = centre + width * 0.2 if up else centre - width * 0.2
            self.bar(o, centre + width / 2, centre - width / 2, c)
        return centre

    def kinds(self):
        return [e.type for e in self.events]


def built(strategy=None, **over):
    st = strategy or BreakdownRetestStrategy(settings(**over))
    tape = Tape(st)
    tape.rally(100.0, 14, 0.45)          # ~+6% run-up
    tape.chop(106.5, 8, 0.8)             # tight range, ~0.75% wide
    return st, tape


# ---------------------------------------------------------------------------
print("\nThe setup forms")
st, tape = built()
check("no signal during the rally or the range", st.setup is None and st.pos == 0)
tape.bar(106.2, 106.3, 105.4, 105.5)     # close below the range floor
check("breakdown is registered", st.setup is not None,
      "setup=%s" % st.setup)
check("the broken floor is remembered",
      st.setup and abs(st.setup.support - 106.1) < 0.25,
      "support=%s" % (st.setup.support if st.setup else None))
check("no entry on the breakdown bar itself", st.pos == 0)

print("\nEntry comes on the failed retest")
evs = tape.bar(105.5, 106.15, 105.4, 105.6)   # back to the level, closes under
entry = [e for e in evs if e.type == EventType.ENTRY]
check("short entered on the rejection", st.pos == -1 and entry,
      "pos=%s" % st.pos)
check("entry is a SELL at the bar close",
      entry and entry[0].side == "SELL" and entry[0].price == 105.6)
check("stop sits above the retest high",
      entry and entry[0].stop > 106.15, "stop=%s" % (entry[0].stop if entry else None))
check("target is 2R below entry",
      entry and abs((entry[0].price - entry[0].t2)
                    - 2 * (entry[0].stop - entry[0].price)) < 1e-9)

print("\nA retest that does not fail is not an entry")
st2, tape2 = built()
tape2.bar(106.2, 106.3, 105.4, 105.5)
check("broken first", st2.setup is not None)
tape2.bar(105.5, 106.4, 105.4, 106.35)        # reclaims the range, closes above
check("no entry when price closes back inside", st2.pos == 0, "pos=%s" % st2.pos)

print("\nThe rally is required")
st3 = BreakdownRetestStrategy(settings())
t3 = Tape(st3)
t3.chop(100.0, 20, 0.8)                        # flat all session, no run-up
t3.bar(99.7, 99.8, 98.9, 99.0)
check("a range with no rally into it is ignored", st3.setup is None)

print("\nThe range must be tight")
st4 = BreakdownRetestStrategy(settings(consolMaxPct=0.4))
t4 = Tape(st4)
t4.rally(100.0, 14, 0.45)
t4.chop(106.5, 8, 0.8)                         # ~0.75% wide, over the 0.4% budget
t4.bar(106.2, 106.3, 105.4, 105.5)
check("a range wider than consolMaxPct is not a setup", st4.setup is None)

print("\nThe retest has a deadline")
st5, tape5 = built(retestBars=3)
tape5.bar(106.2, 106.3, 105.4, 105.5)
check("broken", st5.setup is not None)
for _ in range(4):
    tape5.bar(105.0, 105.1, 104.5, 104.6)      # drifts away, never comes back
check("an un-retested break is abandoned", st5.setup is None)
check("and no trade was taken", st5.pos == 0)

print("\nRisk and exits")
st6, tape6 = built()
tape6.bar(106.2, 106.3, 105.4, 105.5)
evs = tape6.bar(105.5, 106.15, 105.4, 105.6)
e = [x for x in evs if x.type == EventType.ENTRY][0]
risk = e.stop - e.price
tape6.bar(105.6, 105.7, e.price - risk - 0.01, e.price - risk - 0.01)
check("1R is reported on the way",
      any(x.type == EventType.TARGET_HIT and x.target_no == 1 for x in tape6.events))
check("still open at 1R", st6.pos == -1)
evs = tape6.bar(104.0, 104.1, e.t2 - 0.05, e.t2 - 0.05)
close_ev = [x for x in evs if x.closes_position]
check("2R closes the trade", st6.pos == 0 and close_ev)
check("closing event is the target", close_ev and close_ev[0].target_no == 2)

print("\nStop out")
st7, tape7 = built()
tape7.bar(106.2, 106.3, 105.4, 105.5)
evs = tape7.bar(105.5, 106.15, 105.4, 105.6)
stop = [x for x in evs if x.type == EventType.ENTRY][0].stop
evs = tape7.bar(105.7, stop + 0.10, 105.6, stop + 0.05)
check("stop closes the trade",
      st7.pos == 0 and any(x.type == EventType.STOP_HIT for x in evs))

print("\nShort only, and capped per day")
st8, tape8 = built(maxTradesPerDay=1)
tape8.bar(106.2, 106.3, 105.4, 105.5)
tape8.bar(105.5, 106.15, 105.4, 105.6)
check("first short taken", st8.pos == -1)
sides = {e.side for e in tape8.events if e.type == EventType.ENTRY}
check("every entry is a SELL", sides == {"SELL"}, str(sides))
stop = st8.sPx
tape8.bar(105.7, stop + 0.1, 105.6, stop + 0.05)     # stopped out
check("flat again", st8.pos == 0)
tape8.rally(105.0, 14, 0.45)
tape8.chop(111.5, 8, 0.8)
tape8.bar(111.2, 111.3, 110.4, 110.5)
tape8.bar(110.5, 111.15, 110.4, 110.6)
check("daily cap blocks a second trade", st8.pos == 0, "pos=%s" % st8.pos)

print("\nSquare-off")
st9, tape9 = built()
tape9.bar(106.2, 106.3, 105.4, 105.5)
tape9.bar(105.5, 106.15, 105.4, 105.6)
check("in a short before the cut-off", st9.pos == -1)
evs = st9.on_bar(Bar(time=datetime(2026, 9, 14, 15, 15, tzinfo=IST),
                     open=105.0, high=105.2, low=104.8, close=105.0))
check("squared off at 15:15",
      st9.pos == 0 and any(x.type == EventType.SESSION_EXIT for x in evs))

print("\nSetups do not survive overnight")
st10, tape10 = built()
tape10.bar(106.2, 106.3, 105.4, 105.5)
check("armed at the end of the day", st10.setup is not None)
st10.on_bar(Bar(time=datetime(2026, 9, 15, 9, 15, tzinfo=IST),
                open=104.0, high=104.2, low=103.8, close=104.0))
check("cleared on the next session's first bar", st10.setup is None)

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All breakdown checks passed.")
