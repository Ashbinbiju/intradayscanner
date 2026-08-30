"""
Pine Script v6 runtime primitives, reproduced in Python.

The strategy port in ``strategy.py`` is a line-by-line translation of a
TradingView indicator. To keep that translation honest, every Pine built-in it
touches is rebuilt here with matching semantics:

  * ``na`` propagation and the "any comparison against na is false" rule
  * ``ta.tr(true)`` / ``ta.rma`` / ``ta.atr`` including their warm-up behaviour
  * ``series[n]`` historical offset access
  * ``time(timeframe.period, session, tz)`` session-window membership

Nothing in here knows anything about brokers, orders or Angel One.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

# ---------------------------------------------------------------------------
#  na
# ---------------------------------------------------------------------------
#  Pine's `na` is modelled as float NaN. Python's NaN comparison rules already
#  match Pine's: `nan > 0`, `nan >= x` and `x <= nan` are all False, which is
#  exactly how Pine treats a comparison involving na. That equivalence is what
#  lets the port keep the original guard expressions verbatim.

NA = float("nan")


def na(x) -> bool:
    """Pine ``na(x)`` -- true for None or NaN."""
    if x is None:
        return True
    if isinstance(x, float):
        return math.isnan(x)
    return False


def nz(x, replacement=0.0):
    """Pine ``nz(x, replacement)``."""
    return replacement if na(x) else x


def pine_max(a, b):
    """Pine ``math.max`` -- na if either operand is na."""
    if na(a) or na(b):
        return NA
    return a if a > b else b


def pine_min(a, b):
    """Pine ``math.min`` -- na if either operand is na."""
    if na(a) or na(b):
        return NA
    return a if a < b else b


def pine_abs(a):
    return NA if na(a) else abs(a)


# ---------------------------------------------------------------------------
#  series[n]
# ---------------------------------------------------------------------------
class Series:
    """A Pine series: append the current bar's value, index backwards in time.

    ``s[0]`` is the current bar, ``s[1]`` the previous bar. Reading before the
    start of history yields ``na``, exactly as Pine does.
    """

    __slots__ = ("_v",)

    def __init__(self, values: Optional[Iterable[float]] = None):
        self._v: List[float] = list(values) if values is not None else []

    def push(self, value) -> None:
        self._v.append(NA if value is None else value)

    def __getitem__(self, offset: int):
        idx = len(self._v) - 1 - offset
        if idx < 0 or idx >= len(self._v):
            return NA
        return self._v[idx]

    def __len__(self) -> int:
        return len(self._v)

    @property
    def values(self) -> List[float]:
        return self._v


# ---------------------------------------------------------------------------
#  ta.tr / ta.rma / ta.atr
# ---------------------------------------------------------------------------
def true_range(high: float, low: float, prev_close) -> float:
    """Pine ``ta.tr(true)``.

    ``handle_na = true`` means the very first bar (where ``close[1]`` is na)
    falls back to ``high - low`` instead of returning na.
    """
    if na(prev_close):
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


class RMA:
    """Pine ``ta.rma(src, length)`` -- Wilder's smoothing.

    Pine implements it as::

        alpha = 1 / length
        sum := na(sum[1]) ? ta.sma(src, length) : alpha * src + (1 - alpha) * sum[1]

    So the output is **na** until ``length`` samples have arrived, at which
    point it seeds with the simple average of those samples and then smooths
    recursively. That warm-up matters: while ATR is na the strategy can form no
    FVGs and can take no ATR-stop entry, and the port must inherit that.
    """

    __slots__ = ("length", "_seed", "value")

    def __init__(self, length: int):
        if length < 1:
            raise ValueError("RMA length must be >= 1")
        self.length = length
        self._seed: List[float] = []
        self.value = NA

    def update(self, src) -> float:
        if na(src):
            return self.value
        if na(self.value):
            self._seed.append(src)
            if len(self._seed) == self.length:
                self.value = sum(self._seed) / self.length
        else:
            alpha = 1.0 / self.length
            self.value = alpha * src + (1.0 - alpha) * self.value
        return self.value


class ATR:
    """Pine ``ta.atr(length)`` == ``ta.rma(ta.tr(true), length)``."""

    __slots__ = ("_rma", "_prev_close")

    def __init__(self, length: int):
        self._rma = RMA(length)
        self._prev_close = NA

    def update(self, high: float, low: float, close: float) -> float:
        tr = true_range(high, low, self._prev_close)
        value = self._rma.update(tr)
        self._prev_close = close
        return value


# ---------------------------------------------------------------------------
#  time(timeframe.period, session, tz)
# ---------------------------------------------------------------------------
_SESSION_RE = re.compile(r"^\s*(\d{4})\s*-\s*(\d{4})\s*(?::\s*([1-7]+)\s*)?$")

# Pine day numbering: 1 = Sunday ... 7 = Saturday.
_PY_WEEKDAY_TO_PINE = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 1}


@dataclass(frozen=True)
class SessionWindow:
    """A Pine ``input.session`` string such as ``"0915-0930"``.

    Membership is decided by the bar's **opening** timestamp, which is what
    ``time(timeframe.period, session, tz)`` tests. On 5-minute bars an
    ``0915-0930`` window therefore contains the bars opening at 09:15, 09:20
    and 09:25 -- three bars, fifteen minutes -- and excludes the 09:30 bar.
    """

    start_minute: int
    end_minute: int
    days: frozenset  # Pine day numbers, 1 = Sunday
    spec: str

    @classmethod
    def parse(cls, spec: str) -> "SessionWindow":
        m = _SESSION_RE.match(spec or "")
        if not m:
            raise ValueError(
                "Invalid session spec %r; expected 'HHMM-HHMM' with an optional "
                "':days' suffix, e.g. '0915-0930' or '0915-0930:23456'" % (spec,)
            )
        start_txt, end_txt, days_txt = m.groups()

        def to_minutes(text: str) -> int:
            hh, mm = int(text[:2]), int(text[2:])
            if hh > 24 or mm > 59:
                raise ValueError("Invalid time %r in session spec %r" % (text, spec))
            return hh * 60 + mm

        days = frozenset(int(c) for c in days_txt) if days_txt else frozenset(range(1, 8))
        return cls(to_minutes(start_txt), to_minutes(end_txt), days, spec.strip())

    def contains(self, moment: datetime) -> bool:
        """Is this bar-open timestamp (already in the session timezone) inside?"""
        if _PY_WEEKDAY_TO_PINE[moment.weekday()] not in self.days:
            return False
        minute = moment.hour * 60 + moment.minute
        if self.start_minute <= self.end_minute:
            return self.start_minute <= minute < self.end_minute
        # Overnight window (e.g. 2200-0500) wraps past midnight.
        return minute >= self.start_minute or minute < self.end_minute

    def __str__(self) -> str:
        return self.spec


def format_pips(price_distance: float, pip: float) -> str:
    """Pine ``toPips()`` -- ``str.tostring(px / pip, "#.#")``."""
    if na(price_distance) or na(pip) or pip == 0:
        return "-"
    return "%.1f" % (price_distance / pip)
