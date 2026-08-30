"""
Engine checks against hand-built bar sequences.

Each test pins one rule from the Pine source so a later refactor cannot
silently change behaviour.  Run with:  python tests/test_strategy.py
"""

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import StrategySettings
from orbfvg.pine import ATR, SessionWindow, na
from orbfvg.strategy import Bar, EventType, ORBFVGStrategy

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def settings(**overrides):
    """Defaults for the Pine-fidelity checks.

    Everything the port adds on top of the indicator is switched off here, and
    every Pine input whose shipped default we have since changed is pinned back
    to its original value. These tests exist to prove the translation is
    faithful, so they must not drift when the live defaults are retuned. The
    additions get their own sections lower down.
    """
    s = StrategySettings()
    s.tzIn, s.orSess, s.sigSess = "Asia/Kolkata", "0915-0930", "0930-1515"
    s.mintick = 0.05
    s.atrMult = 1.5         # Pine default (config now ships 3.0)
    s.strongClose = 0.0     # addition, off
    s.useTrail = False      # addition, off
    s.sqOffTime = ""        # addition, off
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def day_bars(date, specs, start=(9, 15)):
    """Build consecutive 5-minute bars from (o, h, l, c) tuples."""
    t = datetime(date.year, date.month, date.day, start[0], start[1], tzinfo=IST)
    bars = []
    for o, h, l, c in specs:
        bars.append(Bar(time=t, open=o, high=h, low=l, close=c))
        t += timedelta(minutes=5)
    return bars


def warmup(strategy, date, n=30, price=100.0):
    """Feed flat pre-session bars so ATR is seeded before the real test bars."""
    t = datetime(date.year, date.month, date.day, 9, 15, tzinfo=IST) - timedelta(minutes=5 * n)
    for _ in range(n):
        strategy.on_bar(Bar(time=t, open=price, high=price + 1, low=price - 1, close=price))
        t += timedelta(minutes=5)


# ---------------------------------------------------------------------------
print("\nSession windows")
w = SessionWindow.parse("0915-0930")
check("ORB spans exactly 3 five-minute bars",
      [w.contains(datetime(2026, 8, 28, 9, m, tzinfo=IST)) for m in (10, 15, 20, 25, 30)]
      == [False, True, True, True, False])
check("overnight window wraps midnight",
      SessionWindow.parse("2200-0500").contains(datetime(2026, 8, 28, 23, 0, tzinfo=IST))
      and SessionWindow.parse("2200-0500").contains(datetime(2026, 8, 28, 3, 0, tzinfo=IST)))
check("day filter honours Pine numbering (1=Sun)",
      not SessionWindow.parse("0915-0930:1").contains(datetime(2026, 8, 28, 9, 20, tzinfo=IST)))

# ---------------------------------------------------------------------------
print("\nATR warm-up (ta.rma seeds with SMA at bar length-1)")
a = ATR(14)
vals = [a.update(100 + i, 99 + i, 99.5 + i) for i in range(16)]
check("na for first 13 bars", all(na(v) for v in vals[:13]))
check("seeds on 14th bar", not na(vals[13]))

# ---------------------------------------------------------------------------
print("\nOpening range")
st = ORBFVGStrategy(settings())
d = datetime(2026, 8, 28)
warmup(st, d)
# 3 ORB bars: high 105, low 98
st.on_bar(day_bars(d, [(100, 103, 99, 102)])[0])
st.on_bar(day_bars(d, [(102, 105, 101, 104)], start=(9, 20))[0])
st.on_bar(day_bars(d, [(104, 104, 98, 100)], start=(9, 25))[0])
check("range accumulates while inside window", st.orH == 105 and st.orL == 98,
      "got %s/%s" % (st.orH, st.orL))
check("range not locked during window", not st.orLocked)
st.on_bar(day_bars(d, [(100, 101, 99, 100)], start=(9, 30))[0])
check("range locks on first bar after window", st.orLocked)
check("range values frozen after lock", st.orH == 105 and st.orL == 98)

# ---------------------------------------------------------------------------
print("\nBreakout buffer (bufPct = 5% of range)")
# range 98-105 => width 7, buffer 0.35, lvlUp = 105.35
st2 = ORBFVGStrategy(settings(useFvg=False))
warmup(st2, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st2.on_bar(b)
st2.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
check("close inside buffer does not trigger", st2.pos == 0, "pos=%s" % st2.pos)
ev = st2.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("close beyond buffer triggers", st2.pos == 1, "pos=%s" % st2.pos)
entry = [e for e in ev if e.type == EventType.ENTRY]
check("entry fires at bar close price", entry and entry[0].price == 105.5)
check("stop is entry - ATR*1.5",
      entry and abs(entry[0].stop - (105.5 - st2.atr * 1.5)) < 1e-9)
check("targets are 1R/2R/3R",
      entry and abs(entry[0].t3 - (105.5 + 3 * (105.5 - entry[0].stop))) < 1e-9)

# ---------------------------------------------------------------------------
print("\nFVG detection")
# Bullish FVG needs low[0] > high[2] by at least ATR*0.15.
st_f = ORBFVGStrategy(settings())
warmup(st_f, d)
check("flat warm-up bars produce no FVGs",
      not st_f.bull_fvgs and not st_f.bear_fvgs,
      "%d/%d" % (len(st_f.bull_fvgs), len(st_f.bear_fvgs)))
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st_f.on_bar(b)
st_f.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st_f.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("3-candle imbalance registers a bullish FVG", len(st_f.bull_fvgs) == 1)
check("FVG zone is high[2]..low[0]",
      st_f.bull_fvgs and st_f.bull_fvgs[0].bot == 104 and st_f.bull_fvgs[0].top == 105)

# Mitigation: a later bar trading back into the gap bottom removes it.
st_m = ORBFVGStrategy(settings())
warmup(st_m, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st_m.on_bar(b)
st_m.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st_m.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
st_m.on_bar(day_bars(d, [(105.5, 106, 103.5, 104)], start=(9, 40))[0])
check("bullish FVG is dropped once price fills it", not st_m.bull_fvgs)

# Expiry: fvgLB = 12 bars.
st_e = ORBFVGStrategy(settings(fvgUnmit=False))
warmup(st_e, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st_e.on_bar(b)
st_e.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st_e.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("FVG alive immediately after creation", len(st_e.bull_fvgs) == 1)
t = datetime(2026, 8, 28, 9, 40, tzinfo=IST)
for i in range(12):
    st_e.on_bar(Bar(time=t, open=105.5, high=106.5, low=105.2, close=106))
    t += timedelta(minutes=5)
# Pine expires on `bar_index - fvgBar > fvgLB`, so age 12 still counts.
check("FVG still live at age 12", len(st_e.bull_fvgs) == 1,
      "%d left" % len(st_e.bull_fvgs))
st_e.on_bar(Bar(time=t, open=105.5, high=106.5, low=105.2, close=106))
check("FVG expires at age 13 (fvgLB=12)", not st_e.bull_fvgs,
      "%d left" % len(st_e.bull_fvgs))

print("\nFVG confirmation gate")
# Same breakout, but the 09:35 low no longer clears high[2]=104, so no FVG.
st3 = ORBFVGStrategy(settings(useFvg=True))
warmup(st3, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st3.on_bar(b)
st3.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st3.on_bar(day_bars(d, [(105, 106, 103.9, 105.5)], start=(9, 35))[0])
check("breakout without a live bullish FVG is rejected",
      st3.pos == 0 and not st3.bull_fvgs, "pos=%s" % st3.pos)

# Positive control: identical breakout, FVG present -> entry taken.
st3b = ORBFVGStrategy(settings(useFvg=True))
warmup(st3b, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st3b.on_bar(b)
st3b.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st3b.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("same breakout with a live bullish FVG is accepted", st3b.pos == 1)

# With the gate off, the FVG-less breakout trades.
st3c = ORBFVGStrategy(settings(useFvg=False))
warmup(st3c, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st3c.on_bar(b)
st3c.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st3c.on_bar(day_bars(d, [(105, 106, 103.9, 105.5)], start=(9, 35))[0])
check("useFvg=False bypasses the gate", st3c.pos == 1)

# ---------------------------------------------------------------------------
print("\nOnce per side per session")
st4 = ORBFVGStrategy(settings(useFvg=False))
warmup(st4, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st4.on_bar(b)
st4.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st4.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("first long taken", st4.pos == 1 and st4.tookL)
# knock it out on the stop, then re-cross the level
st4.on_bar(day_bars(d, [(105, 105.5, 90, 91)], start=(9, 40))[0])
check("stop closes the position", st4.pos == 0)
st4.on_bar(day_bars(d, [(91, 92, 90, 91)], start=(9, 45))[0])
st4.on_bar(day_bars(d, [(91, 107, 91, 106)], start=(9, 50))[0])
check("second long blocked by oncePer", st4.pos == 0, "pos=%s" % st4.pos)

# ---------------------------------------------------------------------------
print("\nTarget beats stop on the same bar (Pine evaluation order)")
st5 = ORBFVGStrategy(settings(useFvg=False, slMode="Fixed pips", slPips=100.0))
warmup(st5, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st5.on_bar(b)
st5.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st5.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
e_px, s_px, t3 = st5.ePx, st5.sPx, st5.p3
check("fixed-pip stop uses pip=0.01 above price 20",
      abs(e_px - s_px - 1.0) < 1e-9, "risk=%s" % (e_px - s_px))
# one bar that spans both T3 and the stop
evs = st5.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
                     open=105.5, high=t3 + 0.5, low=s_px - 0.5, close=106))
kinds = [e.type for e in evs]
check("T3 resolves the bar, stop is not evaluated",
      EventType.TARGET_HIT in kinds and EventType.STOP_HIT not in kinds, str(kinds))
check("position flat after T3", st5.pos == 0)

# ---------------------------------------------------------------------------
print("\nSession-end exit")
st6 = ORBFVGStrategy(settings(useFvg=False))
warmup(st6, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st6.on_bar(b)
st6.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st6.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("in position before session end", st6.pos == 1)
evs = st6.on_bar(Bar(time=datetime(2026, 8, 28, 15, 15, tzinfo=IST),
                     open=106, high=106, low=105, close=105.2))
check("session exit fires on first bar outside signal window",
      any(e.type == EventType.SESSION_EXIT for e in evs) and st6.pos == 0)

# ---------------------------------------------------------------------------
print("\nShort side mirrors long")
st7 = ORBFVGStrategy(settings(useFvg=False))
warmup(st7, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st7.on_bar(b)
st7.on_bar(day_bars(d, [(100, 101, 98, 99)], start=(9, 30))[0])
st7.on_bar(day_bars(d, [(99, 99, 96, 96.5)], start=(9, 35))[0])
check("short entry below lvlDn (98 - 0.35)", st7.pos == -1, "pos=%s" % st7.pos)
check("short stop above entry", st7.sPx > st7.ePx)
check("short targets below entry", st7.p1 < st7.ePx and st7.p3 < st7.p1)

# ---------------------------------------------------------------------------
print("\nState resets on the next session")
st8 = ORBFVGStrategy(settings(useFvg=False))
warmup(st8, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st8.on_bar(b)
st8.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st8.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
took_before = st8.tookL
d2 = datetime(2026, 8, 31)
st8.on_bar(day_bars(d2, [(100, 101, 99, 100)])[0])
check("tookL cleared at the new session open", took_before and not st8.tookL)
check("range restarts from the new session's first bar", st8.orH == 101 and st8.orL == 99)
check("orLocked cleared", not st8.orLocked)

# ---------------------------------------------------------------------------
#  Trailing stop (an addition to the indicator, not part of the Pine source)
# ---------------------------------------------------------------------------
def trailing_setup(**overrides):
    """Long from 105.50 with a 1.00 risk -> T1 106.50, T2 107.50, T3 108.50."""
    # Explicit trailMode: these tests must not silently follow the config default.
    base = dict(useFvg=False, slMode="Fixed pips", slPips=100.0,
                useTrail=True, trailMode="Targets")
    base.update(overrides)
    st = ORBFVGStrategy(settings(**base))
    warmup(st, d)
    for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
        st.on_bar(b)
    st.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
    st.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
    return st


print("\nTrailing: breakeven at T1")
st = trailing_setup()
check("entry at 105.50 with stop 104.50 and T1 106.50",
      st.ePx == 105.5 and abs(st.sPx - 104.5) < 1e-9 and abs(st.p1 - 106.5) < 1e-9,
      "e=%s s=%s t1=%s" % (st.ePx, st.sPx, st.p1))
check("initial stop recorded separately", abs(st.iSPx - 104.5) < 1e-9)
# a bar that tags T1 and also dips below entry: must NOT stop out on this bar
evs = st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
                    open=105.5, high=106.6, low=105.0, close=106.2))
check("T1 recorded", st.h1)
check("stop moved to breakeven", abs(st.sPx - 105.5) < 1e-9, "sPx=%s" % st.sPx)
check("STOP_MOVED emitted", any(e.type == EventType.STOP_MOVED for e in evs))
check("no same-bar stop-out from the freshly raised stop",
      st.pos == 1 and not any(e.type == EventType.STOP_HIT for e in evs))
# next bar dips to the new stop
evs = st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
                    open=106.2, high=106.3, low=105.4, close=105.4))
check("breakeven stop triggers on the following bar",
      st.pos == 0 and any(e.type == EventType.STOP_HIT for e in evs))

print("\nTrailing: step to T1 at T2")
st = trailing_setup()
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
              open=105.5, high=106.6, low=105.4, close=106.5))
check("stop at breakeven after T1", abs(st.sPx - 105.5) < 1e-9)
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
              open=106.5, high=107.6, low=106.4, close=107.5))
check("T2 recorded", st.h2)
check("stop stepped up to T1", abs(st.sPx - 106.5) < 1e-9, "sPx=%s" % st.sPx)
check("targets themselves never move",
      abs(st.p1 - 106.5) < 1e-9 and abs(st.p3 - 108.5) < 1e-9)

print("\nTrailing: stop only ever tightens")
st = trailing_setup()
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
              open=105.5, high=106.6, low=105.4, close=106.5))
raised = st.sPx
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
              open=106.5, high=106.5, low=105.6, close=105.7))
check("a pullback does not loosen the stop", st.sPx == raised, "sPx=%s" % st.sPx)

print("\nTrailing: disabled restores the fixed indicator stop")
st = trailing_setup(useTrail=False)
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
              open=105.5, high=107.6, low=105.4, close=107.5))
check("T1 and T2 both hit", st.h1 and st.h2)
check("stop unchanged from entry", abs(st.sPx - 104.5) < 1e-9, "sPx=%s" % st.sPx)

print("\nTrailing: ATR mode")
# trailAtrMult is deliberately small here: this synthetic warm-up gives an ATR
# of roughly 5.7 against a 1.00 fixed-pip risk, so a wide multiple would sit
# below the entry stop and the forward-only rule would (correctly) ignore it.
st = trailing_setup(trailMode="ATR", trailStartR=1.0, trailAtrMult=0.5)
before = st.sPx
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
              open=105.5, high=106.2, low=105.4, close=106.0))
check("no ATR trail before trailStartR of profit", st.sPx == before,
      "moved to %s on +0.7R" % st.sPx)
# Stay below T3 (108.50), or the trade closes and there is nothing left to trail.
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
              open=106.0, high=108.0, low=105.9, close=107.8))
check("position still open below T3", st.pos == 1)
expected = st.trailHigh - st.atr * 0.5
check("ATR trail sits atr x mult below the best price",
      abs(st.sPx - expected) < 1e-9, "sPx=%s expected=%s" % (st.sPx, expected))
check("ATR trail tightened the stop", st.sPx > before)

# A candidate that would loosen the stop is ignored rather than applied.
st = trailing_setup(trailMode="ATR", trailStartR=1.0, trailAtrMult=8.0)
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
              open=105.5, high=108.0, low=105.4, close=107.8))
check("a far ATR trail leaves the entry stop alone",
      st.pos == 1 and abs(st.sPx - 104.5) < 1e-9, "sPx=%s pos=%s" % (st.sPx, st.pos))

print("\nTrailing: short side mirrors")
st = ORBFVGStrategy(settings(useFvg=False, slMode="Fixed pips", slPips=100.0,
                             useTrail=True, trailMode="Targets"))
warmup(st, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st.on_bar(b)
st.on_bar(day_bars(d, [(100, 101, 98, 99)], start=(9, 30))[0])
st.on_bar(day_bars(d, [(99, 99, 96, 96.5)], start=(9, 35))[0])
check("short entered at 96.50 with stop 97.50",
      st.pos == -1 and abs(st.sPx - 97.5) < 1e-9, "s=%s" % st.sPx)
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 40, tzinfo=IST),
              open=96.5, high=96.6, low=95.4, close=95.5))
check("short T1 hit", st.h1)
check("short stop pulled down to breakeven", abs(st.sPx - 96.5) < 1e-9, "s=%s" % st.sPx)
st.on_bar(Bar(time=datetime(2026, 8, 28, 9, 45, tzinfo=IST),
              open=95.5, high=95.6, low=94.4, close=94.5))
check("short stop steps to T1 after T2", abs(st.sPx - 95.5) < 1e-9, "s=%s" % st.sPx)

# ---------------------------------------------------------------------------
print("\nLondon preset on an NSE chart (the indicator's shipped defaults)")
# 0800-0815 Europe/London == 12:30-12:45 IST while BST is in effect.
lon = SessionWindow.parse("0800-0815")
LONDON = ZoneInfo("Europe/London")
ist_bars = [datetime(2026, 8, 28, 12, m, tzinfo=IST) for m in (25, 30, 35, 40, 45)]
check("London open window maps to 12:30/12:35/12:40 IST",
      [lon.contains(b.astimezone(LONDON)) for b in ist_bars]
      == [False, True, True, True, False])
check("signal window 0815-1630 London covers 12:45 IST onward",
      SessionWindow.parse("0815-1630").contains(
          datetime(2026, 8, 28, 12, 45, tzinfo=IST).astimezone(LONDON)))
check("and still covers 20:55 IST, long after NSE closes",
      SessionWindow.parse("0815-1630").contains(
          datetime(2026, 8, 28, 20, 55, tzinfo=IST).astimezone(LONDON)))

print("\nHard square-off")
sq = settings(useFvg=False, sigSess="0930-2100")
sq.sqOffTime, sq.sqOffTz = "15:15", "Asia/Kolkata"
st = ORBFVGStrategy(sq)
warmup(st, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st.on_bar(b)
st.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
check("in position before the cut-off", st.pos == 1)
evs = st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 10, tzinfo=IST),
                    open=106, high=106, low=105, close=105.5))
check("15:10 bar does not square off", st.pos == 1 and not evs)
evs = st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 15, tzinfo=IST),
                    open=105.5, high=106, low=105, close=105.8))
check("15:15 bar squares off at its close",
      st.pos == 0 and any(e.type == EventType.SESSION_EXIT and e.price == 105.8
                          for e in evs), str([e.message for e in evs]))

# no fresh entries after the cut-off
sq2 = settings(useFvg=False, sigSess="0930-2100")
sq2.sqOffTime, sq2.sqOffTz = "15:15", "Asia/Kolkata"
st = ORBFVGStrategy(sq2)
warmup(st, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st.on_bar(b)
st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 15, tzinfo=IST),
              open=100, high=105.2, low=99, close=105.2))
st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 20, tzinfo=IST),
              open=105, high=106, low=105, close=105.5))
check("no entry is opened past the cut-off", st.pos == 0, "pos=%s" % st.pos)

# a session that ends before the cut-off (Angel's feed stops at 15:10 for many
# symbols) must still square off, at the last price that session printed
sq4 = settings(useFvg=False, sigSess="0930-2100")
sq4.sqOffTime, sq4.sqOffTz = "15:15", "Asia/Kolkata"
st = ORBFVGStrategy(sq4)
warmup(st, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st.on_bar(b)
st.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 10, tzinfo=IST),
              open=106, high=106, low=105, close=105.9))
check("still open when the day's last bar is 15:10", st.pos == 1)
evs = st.on_bar(Bar(time=datetime(2026, 8, 31, 9, 15, tzinfo=IST),
                    open=104, high=105, low=103, close=104))
exit_ev = [e for e in evs if e.type == EventType.SESSION_EXIT]
check("next session's first bar closes the carried position", st.pos == 0 and exit_ev)
check("closed at the previous session's last print, not the new open",
      exit_ev and abs(exit_ev[0].price - 105.9) < 1e-9,
      "got %s" % (exit_ev[0].price if exit_ev else None))
check("exit is stamped on the previous session",
      exit_ev and exit_ev[0].time.date() == date(2026, 8, 28),
      str(exit_ev[0].time if exit_ev else None))

# disabling it restores pure indicator behaviour
sq3 = settings(useFvg=False, sigSess="0930-2100")
sq3.sqOffTime = ""
st = ORBFVGStrategy(sq3)
warmup(st, d)
for b in day_bars(d, [(100, 103, 99, 102), (102, 105, 101, 104), (104, 104, 98, 100)]):
    st.on_bar(b)
st.on_bar(day_bars(d, [(100, 105.2, 99, 105.2)], start=(9, 30))[0])
st.on_bar(day_bars(d, [(105, 106, 105, 105.5)], start=(9, 35))[0])
st.on_bar(Bar(time=datetime(2026, 8, 28, 15, 20, tzinfo=IST),
              open=105.5, high=106, low=105, close=105.8))
check("sqOffTime='' holds the position past 15:15", st.pos == 1)

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All engine checks passed.")
