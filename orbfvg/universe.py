"""
Our own stock selection, built from candles we already fetch.

The third-party screener returns generic momentum lists -- gainers, volume
gainers, stocks near the day's high. Useful, but not built for this strategy,
and an outage or a changed endpoint takes the whole scanner down with it.

Everything those lists provide can be computed from OHLCV, which we have two
sources for. More to the point, the one selection filter that actually
improved results in testing (`minRangePct`: skip sessions whose opening range
is too thin) was never a screener field at all -- it comes from the stock's own
12:30-12:45 candles.

Three stages, cheapest first:

  1. DAILY   Run once, pre-market. One request per symbol buys a year of daily
             bars, so the whole NSE cash list costs ~2 minutes. Filters on
             liquidity, price and daily volatility -- slow-moving facts that do
             not need re-checking intraday. ~2,650 names down to a few hundred.

  2. INTRADAY  Run shortly before the range forms. Pulls 5-minute bars for the
             survivors and ranks them on relative volume, distance travelled
             and where price sits in the day's range -- the same ideas the
             third-party lists encode, measured directly.

  3. RANGE   Applied by the engine itself at 12:45 via `minRangePct`, once the
             opening range exists.

Stage 2 reads only bars that closed before its cut-off, so a shortlist built
for a past day contains no information that was unavailable at the time.
Getting that wrong is the easiest way to build a screener that backtests
beautifully and loses money.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, List, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import config
from .pine import ATR

log = logging.getLogger("orbfvg.universe")

IST = ZoneInfo("Asia/Kolkata")
DAILY_CACHE = os.path.join(config.DATA_DIR, "universe_daily.json")
UPSTOX_API = "https://api.upstox.com"


# ---------------------------------------------------------------------------
#  Filters
# ---------------------------------------------------------------------------
@dataclass
class Filters:
    """Stage-1 thresholds. Defaults chosen from what hurt in the testing.

    `min_turnover_cr` is the important one. The measured edge was about
    +0.16% per trade gross, and a stock that trades a crore a day has a spread
    wide enough to eat that on its own. ALOKINDS came up earlier at 9.34 with
    a 6-paise stop -- arithmetically a fine R, untradeable in practice.
    """

    min_turnover_cr: float = 5.0     # median daily turnover, rupees crore
    min_price: float = 50.0          # below this the tick is a large % of price
    max_price: float = 20000.0
    min_atr_pct: float = 1.5         # needs room to reach 1R-3R inside a session
    max_atr_pct: float = 12.0        # above this the ATR stop is enormous
    min_sessions: int = 40           # traded on most of the last 60 days
    exclude_etf: bool = True


DEFAULT_FILTERS = Filters()

# ETFs and similar move on NAV, not on opening-range momentum.
_ETF_HINTS = ("ETF", "BEES", "IETF", "GOLD", "SILVER", "LIQUID", "GSEC",
              "SDL", "NIFTY", "SENSEX", "BHARATBOND")


@dataclass
class DailyStats:
    symbol: str
    instrument_key: str
    close: float
    turnover_cr: float          # median daily turnover, rupees crore
    atr_pct: float              # ATR(14) on daily bars, % of close
    range_pct: float            # median daily high-low, % of close
    sessions: int
    tick_size: float
    lot_size: int

    def passes(self, f: Filters) -> bool:
        if self.sessions < f.min_sessions:
            return False
        if not (f.min_price <= self.close <= f.max_price):
            return False
        if self.turnover_cr < f.min_turnover_cr:
            return False
        if not (f.min_atr_pct <= self.atr_pct <= f.max_atr_pct):
            return False
        if f.exclude_etf and any(h in self.symbol.upper() for h in _ETF_HINTS):
            return False
        return True


# ---------------------------------------------------------------------------
#  Rate-limited fetching
# ---------------------------------------------------------------------------
class _Limiter:
    """Token bucket shared across worker threads."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            delay = self._next - now
            self._next += self.interval
        if delay > 0:
            time.sleep(delay)


def _upstox_headers() -> dict:
    token = os.getenv("UPSTOX_TOKEN", "")
    if not token:
        raise RuntimeError("UPSTOX_TOKEN is required to build the universe")
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


# Upstox serves minute-granularity history about a month at a time, and a
# longer window comes back as an empty list rather than an error -- a silent
# failure that looks exactly like "this symbol did not trade".
_MINUTE_CHUNK_DAYS = 25


def _get_candles_chunked(instrument_key: str, unit: str, step: str,
                         first, last, limiter: _Limiter,
                         headers: dict) -> List[list]:
    """Walk a long span in windows the API will actually serve."""
    span = _MINUTE_CHUNK_DAYS if unit == "minutes" else 3650
    rows: List[list] = []
    cursor = first
    while cursor <= last:
        chunk_end = min(cursor + timedelta(days=span - 1), last)
        rows.extend(_get_candles(instrument_key, unit, step,
                                 chunk_end.isoformat(), cursor.isoformat(),
                                 limiter, headers))
        cursor = chunk_end + timedelta(days=1)
    return rows


def _get_candles(instrument_key: str, unit: str, step: str,
                 to_date: str, from_date: str, limiter: _Limiter,
                 headers: dict, retries: int = 3) -> List[list]:
    url = "%s/v3/historical-candle/%s/%s/%s/%s/%s" % (
        UPSTOX_API, quote(instrument_key, safe=""), unit, step, to_date, from_date)
    for attempt in range(retries):
        limiter.wait()
        try:
            response = requests.get(url, headers=headers, timeout=45)
        except requests.RequestException:
            time.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(2.0 * (attempt + 1))
            continue
        if response.status_code != 200:
            return []
        return (response.json().get("data") or {}).get("candles") or []
    return []


# ---------------------------------------------------------------------------
#  Stage 1 -- daily statistics
# ---------------------------------------------------------------------------
def _daily_stats_for(row: dict, limiter: _Limiter, headers: dict,
                     lookback_days: int, as_of=None) -> Optional[DailyStats]:
    # `as_of` exists so a backtest can rebuild the universe as it looked before
    # the test window. Liquidity and ATR drift slowly, but measuring them over
    # a period that includes the days being tested is still lookahead.
    end = as_of or datetime.now(IST).date()
    start = end - timedelta(days=lookback_days)
    candles = _get_candles(row["instrument_key"], "days", "1",
                           end.isoformat(), start.isoformat(), limiter, headers)
    if len(candles) < 20:
        return None

    # Upstox returns newest first.
    candles = sorted(candles, key=lambda c: c[0])
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]

    last_close = closes[-1]
    if last_close <= 0:
        return None

    recent = slice(-20, None)
    turnovers = [c * v for c, v in zip(closes[recent], volumes[recent])]
    turnover_cr = median(turnovers) / 1e7 if turnovers else 0.0

    atr = ATR(14)
    value = float("nan")
    for h, l, c in zip(highs, lows, closes):
        value = atr.update(h, l, c)
    atr_pct = (value / last_close * 100.0) if value == value else 0.0

    ranges = [(h - l) / c * 100.0 for h, l, c in
              zip(highs[recent], lows[recent], closes[recent]) if c > 0]

    return DailyStats(
        symbol=str(row["trading_symbol"]).upper(),
        instrument_key=row["instrument_key"],
        close=last_close,
        turnover_cr=round(turnover_cr, 2),
        atr_pct=round(atr_pct, 2),
        range_pct=round(median(ranges), 2) if ranges else 0.0,
        sessions=len(candles),
        tick_size=float(row.get("tick_size") or 5.0) / 100.0,
        lot_size=int(row.get("lot_size") or 1),
    )


def build_daily(workers: int = 6, per_second: float = 12.0,
                lookback_days: int = 120, limit: int = 0,
                progress=None, as_of=None) -> List[DailyStats]:
    """Fetch a few months of daily bars for every NSE equity and summarise."""
    from concurrent.futures import ThreadPoolExecutor

    from .upstox import _load_instruments

    rows = _load_instruments()
    if limit:
        rows = rows[:limit]
    headers = _upstox_headers()
    limiter = _Limiter(per_second)
    out: List[DailyStats] = []
    done = 0

    def work(row):
        try:
            return _daily_stats_for(row, limiter, headers, lookback_days, as_of)
        except Exception as exc:
            log.debug("daily stats failed for %s: %s", row.get("trading_symbol"), exc)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for stats in pool.map(work, rows):
            done += 1
            if stats is not None:
                out.append(stats)
            if progress and done % 100 == 0:
                progress(done, len(rows), len(out))
    return out


def save_daily(stats: Sequence[DailyStats], path: str = DAILY_CACHE,
               as_of=None) -> str:
    payload = {"built_at": (as_of.isoformat() if as_of
                            else datetime.now(IST).isoformat()),
               "rows": [asdict(s) for s in stats]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def load_daily(path: str = DAILY_CACHE) -> tuple:
    """(stats, built_at) or ([], None) when nothing has been built yet."""
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return [], None
    return [DailyStats(**r) for r in payload.get("rows", [])], payload.get("built_at")


def eligible(stats: Sequence[DailyStats], filters: Filters = None) -> List[DailyStats]:
    filters = filters or DEFAULT_FILTERS
    return sorted((s for s in stats if s.passes(filters)),
                  key=lambda s: s.turnover_cr, reverse=True)


# ---------------------------------------------------------------------------
#  Stage 2 -- intraday ranking
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    symbol: str
    ltp: float
    move_pct: float          # since the previous close
    rel_volume: float        # today's volume vs the same time on a normal day
    range_position: float    # 0 = on the day's low, 1 = on the day's high
    day_range_pct: float
    turnover_cr: float
    atr_pct: float
    score: float
    direction: str           # "long" or "short" lean

    def as_row(self) -> dict:
        return asdict(self)


def _intraday_series(stats: DailyStats, first_day: str, last_day: str,
                     cutoff: str, limiter: _Limiter, headers: dict,
                     vol_lookback: int = 10) -> Dict[str, list]:
    """One fetch per symbol for the whole span, grouped by day.

    Ranking a range of days used to re-fetch the same candles once per day --
    930 symbols over 12 sessions was 11,160 requests. The candles do not
    change, so pull them once and compute every day from the same series.

    Bars at or after `cutoff` are dropped here, at the point of ingestion, so
    no later step can accidentally see them.
    """
    start = (datetime.strptime(first_day, "%Y-%m-%d").date()
             - timedelta(days=vol_lookback * 2 + 10))
    end = datetime.strptime(last_day, "%Y-%m-%d").date()
    candles = _get_candles_chunked(stats.instrument_key, "minutes", "5",
                                   start, end, limiter, headers)
    by_day: Dict[str, list] = {}
    for row in candles:
        stamp = datetime.fromisoformat(row[0]).astimezone(IST)
        if stamp.strftime("%H:%M") >= cutoff:
            continue
        by_day.setdefault(stamp.strftime("%Y-%m-%d"), []).append(
            (stamp, float(row[1]), float(row[2]), float(row[3]),
             float(row[4]), float(row[5])))
    for day in by_day:
        by_day[day].sort()
    return by_day


def _rank_day(stats: DailyStats, by_day: Dict[str, list], day: str,
              vol_lookback: int = 10) -> Optional[Candidate]:
    """Score one symbol on one day from a pre-fetched series."""
    today_bars = by_day.get(day) or []
    if len(today_bars) < 6:
        return None
    prior_days = sorted(d for d in by_day if d < day)[-vol_lookback:]
    if not prior_days:
        return None

    today_volume = sum(b[5] for b in today_bars)
    prior_volumes = [sum(b[5] for b in by_day[d]) for d in prior_days]
    typical = median([v for v in prior_volumes if v > 0] or [0])
    rel_volume = (today_volume / typical) if typical > 0 else 0.0

    # Reference price: the last bar of the previous session, cut at the same
    # time of day. A same-time comparison, not the official close -- which is
    # the honest choice, since the official close is not what a 12:25 scan
    # would have been comparing against mid-session.
    prev_close = by_day[prior_days[-1]][-1][4]
    last = today_bars[-1]
    high = max(b[2] for b in today_bars)
    low = min(b[3] for b in today_bars)
    ltp = last[4]
    if not prev_close or not ltp:
        return None

    move_pct = (ltp - prev_close) / prev_close * 100.0
    span = high - low
    position = (ltp - low) / span if span > 0 else 0.5
    day_range_pct = span / ltp * 100.0

    # Wanted: real participation, a decisive move, and price at the edge of
    # its range rather than mid-chop.
    conviction = abs(position - 0.5) * 2.0
    score = (min(rel_volume, 5.0) / 5.0) * 0.4         + min(abs(move_pct) / 5.0, 1.0) * 0.35         + conviction * 0.25

    return Candidate(
        symbol=stats.symbol, ltp=round(ltp, 2), move_pct=round(move_pct, 2),
        rel_volume=round(rel_volume, 2), range_position=round(position, 2),
        day_range_pct=round(day_range_pct, 2), turnover_cr=stats.turnover_cr,
        atr_pct=stats.atr_pct, score=round(score, 4),
        direction="long" if position >= 0.5 else "short",
    )


def shortlist_many(stats: Sequence[DailyStats], days: Sequence[str],
                   cutoff: str = "12:25", top_n: int = 20,
                   min_rel_volume: float = 1.0, min_move_pct: float = 1.0,
                   workers: int = 6, per_second: float = 12.0,
                   progress=None) -> Dict[str, List[Candidate]]:
    """Rank many days from one pass over the data."""
    from concurrent.futures import ThreadPoolExecutor

    days = sorted(days)
    headers = _upstox_headers()
    limiter = _Limiter(per_second)
    per_day: Dict[str, List[Candidate]] = {d: [] for d in days}
    done = 0

    def work(s):
        try:
            series = _intraday_series(s, days[0], days[-1], cutoff, limiter, headers)
            return s, series
        except Exception as exc:
            log.debug("intraday series failed for %s: %s", s.symbol, exc)
            return s, {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for s, series in pool.map(work, stats):
            done += 1
            for day in days:
                candidate = _rank_day(s, series, day)
                if candidate is not None:
                    per_day[day].append(candidate)
            if progress and done % 50 == 0:
                progress(done, len(stats), 0)

    for day, found in per_day.items():
        keep = [c for c in found if c.rel_volume >= min_rel_volume
                and abs(c.move_pct) >= min_move_pct]
        keep.sort(key=lambda c: c.score, reverse=True)
        per_day[day] = keep[:top_n]
    return per_day


def shortlist(stats: Sequence[DailyStats], day: str, cutoff: str = "12:25",
              top_n: int = 40, min_rel_volume: float = 1.0,
              min_move_pct: float = 1.0, workers: int = 6,
              per_second: float = 12.0, progress=None) -> List[Candidate]:
    """Rank the eligible universe as it looked just before the range forms."""
    return shortlist_many(stats, [day], cutoff=cutoff, top_n=top_n,
                          min_rel_volume=min_rel_volume,
                          min_move_pct=min_move_pct, workers=workers,
                          per_second=per_second, progress=progress)[day]
