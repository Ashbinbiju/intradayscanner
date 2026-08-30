"""
Upstox market-data client -- the fallback when Angel One is unavailable.

Angel's historical endpoint is rate-limited to 3 requests/second and does go
down; a second source keeps the scanner usable when it does. This module reads
candles only. It never places an order, and the Analytics token it uses cannot:
the trading endpoints reject it unless the request comes from a static IP
registered on the account.

Two differences from Angel worth knowing, both handled here:

  * Instruments are keyed by ISIN (``NSE_EQ|INE002A01018``), not a numeric
    token, and Upstox writes symbols without Angel's ``-EQ`` suffix.
  * Candles come back newest-first; they are reversed to match everything
    else in this codebase, which assumes oldest-first.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import config
from .strategy import Bar

log = logging.getLogger("orbfvg.upstox")

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INSTRUMENT_CACHE = os.path.join(config.DATA_DIR, "upstox_nse.json")
_ONE_DAY = 24 * 60 * 60

# Angel interval name -> (unit, interval) for the Upstox v3 candle API.
INTERVALS = {
    "ONE_MINUTE": ("minutes", "1"),
    "THREE_MINUTE": ("minutes", "3"),
    "FIVE_MINUTE": ("minutes", "5"),
    "TEN_MINUTE": ("minutes", "10"),
    "FIFTEEN_MINUTE": ("minutes", "15"),
    "THIRTY_MINUTE": ("minutes", "30"),
    "ONE_HOUR": ("hours", "1"),
    "ONE_DAY": ("days", "1"),
}


class UpstoxError(RuntimeError):
    pass


@dataclass
class UpstoxInstrument:
    """Shaped like orbfvg.instruments.Instrument so callers need no branch."""

    token: str          # the instrument_key, e.g. NSE_EQ|INE002A01018
    symbol: str
    name: str
    exch_seg: str
    lotsize: int
    tick_size: float


def _load_instruments(force: bool = False) -> List[dict]:
    fresh = (os.path.exists(INSTRUMENT_CACHE)
             and time.time() - os.path.getmtime(INSTRUMENT_CACHE) < _ONE_DAY)
    if fresh and not force:
        try:
            with open(INSTRUMENT_CACHE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass

    response = requests.get(INSTRUMENTS_URL, timeout=120)
    response.raise_for_status()
    rows = json.loads(gzip.decompress(response.content))
    equities = [r for r in rows
                if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ"]
    tmp = INSTRUMENT_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(equities, fh)
    os.replace(tmp, INSTRUMENT_CACHE)
    return equities


_INDEX: Optional[Dict[str, UpstoxInstrument]] = None


def _index() -> Dict[str, UpstoxInstrument]:
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for row in _load_instruments():
            symbol = str(row.get("trading_symbol", "")).upper()
            if not symbol:
                continue
            _INDEX[symbol] = UpstoxInstrument(
                token=row["instrument_key"],
                symbol=symbol,
                name=str(row.get("name", symbol)),
                exch_seg="NSE",
                lotsize=int(row.get("lot_size") or 1),
                # Reported in paise, exactly as Angel does it.
                tick_size=float(row.get("tick_size") or 5.0) / 100.0,
            )
    return _INDEX


def resolve(symbol: str, exchange: str = "NSE") -> UpstoxInstrument:
    """Angel-style symbol in, Upstox instrument out. ``-EQ`` is optional."""
    if exchange.upper() != "NSE":
        raise LookupError("The Upstox fallback only covers NSE cash")
    key = symbol.upper()
    found = _index().get(key) or _index().get(key.replace("-EQ", ""))
    if not found:
        raise LookupError("No Upstox instrument for %r" % symbol)
    return found


class UpstoxClient:
    """Candles only. Same call shape as AngelClient so it can stand in."""

    def __init__(self, token: str = ""):
        self.token = token or os.getenv("UPSTOX_TOKEN", "")

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.token, "Accept": "application/json"}

    def login(self):
        """No-op. The token is already a bearer credential."""
        if not self.token:
            raise UpstoxError(
                "No UPSTOX_TOKEN set. Put it in .env or Streamlit secrets.")
        return {"source": "upstox"}

    def instrument(self, symbol: str, exchange: str = "NSE") -> UpstoxInstrument:
        return resolve(symbol, exchange)

    def _get(self, url: str) -> dict:
        response = requests.get(url, headers=self._headers(), timeout=45)
        if response.status_code == 401:
            raise UpstoxError("Upstox rejected the token (401). It may have "
                              "expired, or the endpoint needs a static IP.")
        if response.status_code != 200:
            raise UpstoxError("Upstox returned HTTP %d: %s"
                              % (response.status_code, response.text[:200]))
        payload = response.json()
        if payload.get("status") != "success":
            raise UpstoxError("Upstox error: %s" % str(payload)[:200])
        return payload.get("data") or {}

    def candles(self, exchange: str, instrument_key: str, interval: str,
                start: datetime, end: datetime, tz=IST) -> List[Bar]:
        """Historical plus intraday, merged, oldest first.

        The historical endpoint does not serve the current day, so today's
        bars are pulled from the intraday endpoint and stitched on.
        """
        if interval not in INTERVALS:
            raise UpstoxError("Unsupported interval %r" % interval)
        unit, step = INTERVALS[interval]
        key = quote(instrument_key, safe="")
        today = datetime.now(tz).date()

        rows: List[list] = []
        hist_end = min(end.date(), today - timedelta(days=1))
        if start.date() <= hist_end:
            url = "%s/v3/historical-candle/%s/%s/%s/%s/%s" % (
                API, key, unit, step, hist_end.isoformat(), start.date().isoformat())
            rows.extend(self._get(url).get("candles") or [])

        if end.date() >= today >= start.date():
            url = "%s/v3/historical-candle/intraday/%s/%s/%s" % (API, key, unit, step)
            try:
                rows.extend(self._get(url).get("candles") or [])
            except UpstoxError as exc:
                log.debug("Upstox intraday unavailable: %s", exc)

        bars, seen = [], set()
        for row in rows:
            stamp = datetime.fromisoformat(row[0])
            if tz is not None:
                stamp = stamp.astimezone(tz)
            if stamp in seen or not (start <= stamp <= end):
                continue
            seen.add(stamp)
            bars.append(Bar(time=stamp, open=float(row[1]), high=float(row[2]),
                            low=float(row[3]), close=float(row[4]),
                            volume=float(row[5]) if len(row) > 5 else 0.0))
        bars.sort(key=lambda b: b.time)      # Upstox returns newest-first
        return bars

    def logout(self) -> None:
        return None
