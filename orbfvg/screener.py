"""
Intraday Screener client -- the symbol universe for the scanner.

    https://intradayscreener.com/api/trackStocks/cash

One GET returns ~21 buckets. Thirteen carry full objects (LTP, % change,
volume change, intraday range, pivots, and the list of scans the stock is
currently triggering); eight carry bare symbol lists.

The response is a **live snapshot with no timestamp of its own** -- it tells
you what is happening now, not what was happening at 12:30 last Thursday.
That matters: the strategy needs to know which stocks were on a list *at the
time*, so `save_snapshot` writes each fetch to disk. Run it on a schedule and
the history needed for honest backtests accumulates. Without that, a backtest
over past dates is picking today's winners out of last week's charts, which
flatters every result.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests

API_URL = "https://intradayscreener.com/api/trackStocks/cash"
IST = ZoneInfo("Asia/Kolkata")

# The API's own naming is inconsistent -- `stocksNearDaysHighAnd3DayLow`
# actually holds stocks near the day's LOW. Grouped by what they contain.
BULLISH = [
    "high52Week", "topGainers", "volumeGainers", "intradayGainers", "gapup",
    "longBuildUp", "shortCovering", "stocksNearDaysHigh",
    "stocksNearDaysHighAndYestHigh", "stocksNearDaysHighAnd3DayHigh",
    "stocksNearDaysHighAnd5DayHigh",
]
BEARISH = [
    "low52Week", "topLoosers", "intradayLosers", "gapdown", "shortBuildUp",
    "longUnWinding", "stocksNearDaysLow", "stocksNearDaysLowAndYestLow",
    "stocksNearDaysHighAnd3DayLow", "stocksNearDaysHighAnd5DayLow",
]

LABELS = {
    "high52Week": "52-week high",
    "low52Week": "52-week low",
    "topGainers": "Top gainers",
    "topLoosers": "Top losers",
    "volumeGainers": "Volume gainers",
    "intradayGainers": "Intraday gainers",
    "intradayLosers": "Intraday losers",
    "gapup": "Gap up",
    "gapdown": "Gap down",
    "longBuildUp": "Long build-up",
    "shortBuildUp": "Short build-up",
    "shortCovering": "Short covering",
    "longUnWinding": "Long unwinding",
    "stocksNearDaysHigh": "Near day's high",
    "stocksNearDaysHighAndYestHigh": "Near day's + yesterday's high",
    "stocksNearDaysHighAnd3DayHigh": "Near day's + 3-day high",
    "stocksNearDaysHighAnd5DayHigh": "Near day's + 5-day high",
    "stocksNearDaysLow": "Near day's low",
    "stocksNearDaysLowAndYestLow": "Near day's + yesterday's low",
    "stocksNearDaysHighAnd3DayLow": "Near day's + 3-day low",
    "stocksNearDaysHighAnd5DayLow": "Near day's + 5-day low",
}


class ScreenerError(RuntimeError):
    pass


def fetch(timeout: int = 45, url: str = API_URL) -> dict:
    """One snapshot of every bucket."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ScreenerError("Could not reach the screener: %s" % exc) from exc
    if response.status_code != 200:
        raise ScreenerError("Screener returned HTTP %d" % response.status_code)
    try:
        data = response.json()
    except ValueError as exc:
        raise ScreenerError("Screener did not return JSON: %s" % exc) from exc
    if not isinstance(data, dict) or not data:
        raise ScreenerError("Screener returned an unexpected payload")
    return data


def bucket_names(data: dict) -> List[str]:
    return [k for k, v in data.items() if isinstance(v, list)]


def symbols_in(data: dict, bucket: str) -> List[str]:
    """Symbols in one bucket, whether it holds objects or bare strings."""
    out = []
    for item in data.get(bucket) or []:
        if isinstance(item, dict):
            symbol = item.get("symbol")
        else:
            symbol = item
        if symbol:
            out.append(str(symbol).upper())
    return sorted(set(out))


def symbols(data: dict, buckets: Iterable[str]) -> List[str]:
    """Union of several buckets."""
    found = set()
    for bucket in buckets:
        found.update(symbols_in(data, bucket))
    return sorted(found)


def details(data: dict) -> Dict[str, dict]:
    """Per-symbol facts, merged across every bucket that carries objects.

    A symbol usually appears in several buckets; the buckets it appears in are
    collected under ``buckets`` so the UI can show why it was picked up.
    """
    merged: Dict[str, dict] = {}
    for bucket, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                symbol = str(item.get("symbol", "")).upper()
                if not symbol:
                    continue
                row = merged.setdefault(symbol, {"symbol": symbol, "buckets": []})
                row["buckets"].append(bucket)
                for field in ("ltp", "priceChangePct", "changeInVolPct",
                              "intradayPriceHigh", "intradayPriceLow", "fno"):
                    if item.get(field) is not None:
                        row[field] = item[field]
                scans = item.get("intradayScansList") or []
                tags = row.setdefault("scans", [])
                for scan in scans:
                    code = scan.get("scanShortcode") or scan.get("scanName")
                    if code and code not in tags:
                        tags.append(code)
                if item.get("pivots"):
                    row["pivots"] = item["pivots"]
            else:
                symbol = str(item).upper()
                row = merged.setdefault(symbol, {"symbol": symbol, "buckets": []})
                row["buckets"].append(bucket)
    for row in merged.values():
        row["buckets"] = sorted(set(row["buckets"]))
    return merged


# ---------------------------------------------------------------------------
#  Snapshot history
# ---------------------------------------------------------------------------
def snapshot_dir(base: str) -> str:
    path = os.path.join(base, "screener")
    os.makedirs(path, exist_ok=True)
    return path


def save_snapshot(data: dict, base: str, moment: Optional[datetime] = None) -> str:
    """Write one fetch to data/screener/YYYY-MM-DD/HHMM.json."""
    moment = moment or datetime.now(IST)
    day_dir = os.path.join(snapshot_dir(base), moment.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, moment.strftime("%H%M") + ".json")
    payload = {"captured_at": moment.isoformat(), "data": data}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return path


def list_snapshot_days(base: str) -> List[str]:
    root = snapshot_dir(base)
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)))


def load_snapshots(base: str, day: str) -> List[dict]:
    """Every snapshot recorded on one day, oldest first."""
    day_dir = os.path.join(snapshot_dir(base), day)
    if not os.path.isdir(day_dir):
        return []
    out = []
    for name in sorted(os.listdir(day_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(day_dir, name), "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def symbols_from_history(base: str, day: str, buckets: Iterable[str],
                         start_time: str = "00:00", end_time: str = "23:59") -> List[str]:
    """Symbols that appeared in these buckets within a time window on `day`.

    This is the honest replacement for querying a dated watchlist: it only
    knows what was actually recorded at the time.
    """
    found = set()
    for snap in load_snapshots(base, day):
        stamp = snap.get("captured_at", "")
        clock = stamp[11:16] if len(stamp) >= 16 else ""
        if not (start_time <= clock <= end_time):
            continue
        found.update(symbols(snap.get("data") or {}, buckets))
    return sorted(found)
