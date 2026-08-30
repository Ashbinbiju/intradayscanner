"""
Angel One scrip master: resolve a tradingsymbol to its symboltoken.

Every SmartAPI call that touches an instrument needs the numeric `symboltoken`,
not just the symbol.  The full master is a single large JSON file that Angel
regenerates daily, so it is cached on disk and refreshed once a day.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

import config

_ONE_DAY = 24 * 60 * 60


@dataclass
class Instrument:
    token: str
    symbol: str
    name: str
    exch_seg: str
    lotsize: int
    tick_size: float
    expiry: str = ""
    instrumenttype: str = ""

    @classmethod
    def from_raw(cls, raw: dict) -> "Instrument":
        def as_int(value, default=1):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        def as_float(value, default=5.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            token=str(raw.get("token", "")),
            symbol=str(raw.get("symbol", "")),
            name=str(raw.get("name", "")),
            exch_seg=str(raw.get("exch_seg", "")),
            lotsize=as_int(raw.get("lotsize"), 1),
            # The master reports tick size in paise (500 -> 5.00), so scale it.
            tick_size=as_float(raw.get("tick_size"), 5.0) / 100.0,
            expiry=str(raw.get("expiry", "")),
            instrumenttype=str(raw.get("instrumenttype", "")),
        )


def _cache_is_fresh(path: str, max_age: int = _ONE_DAY) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age


def load_master(force_refresh: bool = False) -> List[dict]:
    """Return the scrip master, downloading it if the cache is stale."""
    path = config.SCRIP_MASTER_CACHE
    if not force_refresh and _cache_is_fresh(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass  # fall through and re-download

    response = requests.get(config.SCRIP_MASTER_URL, timeout=60)
    response.raise_for_status()
    data = response.json()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data


_INDEX: Optional[Dict[str, Instrument]] = None


def _index() -> Dict[str, Instrument]:
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for raw in load_master():
            symbol = str(raw.get("symbol", "")).upper()
            segment = str(raw.get("exch_seg", "")).upper()
            if symbol and segment:
                _INDEX.setdefault("%s:%s" % (segment, symbol), Instrument.from_raw(raw))
    return _INDEX


def resolve(symbol: str, exchange: str) -> Instrument:
    """Look up one instrument, or raise with near-miss suggestions."""
    key = "%s:%s" % (exchange.upper(), symbol.upper())
    found = _index().get(key)
    if found:
        return found

    # NSE cash symbols carry an "-EQ" suffix people routinely forget.
    if exchange.upper() == "NSE" and not symbol.upper().endswith("-EQ"):
        retry = _index().get("NSE:%s-EQ" % symbol.upper())
        if retry:
            return retry

    needle = symbol.upper().split("-")[0]
    hints = sorted(
        k.split(":", 1)[1]
        for k in _index()
        if k.startswith(exchange.upper() + ":") and needle in k
    )[:10]
    raise LookupError(
        "No instrument %r on %s.%s"
        % (symbol, exchange, ("  Did you mean: %s" % ", ".join(hints)) if hints else "")
    )


def search(term: str, exchange: Optional[str] = None, limit: int = 25) -> List[Instrument]:
    """Substring search over the master, for the `symbols` CLI command."""
    needle = term.upper()
    out = []
    for key, inst in _index().items():
        if exchange and not key.startswith(exchange.upper() + ":"):
            continue
        if needle in inst.symbol.upper() or needle in inst.name.upper():
            out.append(inst)
            if len(out) >= limit:
                break
    return out
