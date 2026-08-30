"""
Market data with a fallback: Angel One first, Upstox second (or the reverse).

Angel's historical endpoint is rate-limited to 3 requests/second and does fail
from time to time, which leaves the scanner with nothing. Upstox serves the
same NSE 5-minute candles from a different account and a different network
path, so one covering the other removes a single point of failure.

The two agree on price. Checked bar for bar on 28 Aug 2026 across four
symbols, every shared bar matched on OHLC to the paise, and the tick sizes
agreed. They do **not** agree on coverage:

    RELIANCE-EQ   angel last 15:10   upstox last 15:25
    COFORGE-EQ    angel last 15:10   upstox last 15:25
    KPITTECH      angel last 15:10   upstox last 15:25

Angel simply stops at 15:10 for many symbols. That missing tail is what let
positions ride overnight before the carried-position square-off was added, so
for this strategy Upstox is the better primary, not merely a spare.

Only market data lives here. Orders stay on Angel: the Upstox Analytics token
is rejected by the trading endpoints unless the request comes from a static IP
registered on that account.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from .strategy import Bar

log = logging.getLogger("orbfvg.feed")


class MarketData:
    """One candle source with another behind it.

    `prefer` picks which is tried first; the other is used when the first
    raises or returns nothing. Both clients are created lazily so a missing
    credential for one does not stop the other working.
    """

    def __init__(self, prefer: str = None, angel=None, upstox=None):
        self.prefer = (prefer or os.getenv("DATA_SOURCE", "upstox")).lower()
        self._angel = angel
        self._upstox = upstox
        self.last_source: Optional[str] = None

    # -- lazy clients -----------------------------------------------------
    def angel(self):
        if self._angel is None:
            from .angel import AngelClient

            client = AngelClient()
            client.login()
            self._angel = client
        return self._angel

    def upstox(self):
        if self._upstox is None:
            from .upstox import UpstoxClient

            client = UpstoxClient()
            if not client.available:
                raise RuntimeError("No UPSTOX_TOKEN configured")
            self._upstox = client
        return self._upstox

    def _order(self) -> List[str]:
        return (["upstox", "angel"] if self.prefer == "upstox"
                else ["angel", "upstox"])

    def _client(self, name):
        return self.upstox() if name == "upstox" else self.angel()

    # -- data -------------------------------------------------------------
    def instrument(self, symbol: str, exchange: str = "NSE"):
        errors = []
        for name in self._order():
            try:
                inst = self._client(name).instrument(symbol, exchange)
                self.last_source = name
                return inst
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
        raise LookupError("Could not resolve %r (%s)" % (symbol, "; ".join(errors)))

    def candles(self, exchange: str, symbol: str, interval: str,
                start, end, tz=None) -> List[Bar]:
        """Candles for a *symbol* -- each source resolves its own key.

        Takes the symbol rather than a token precisely because the two use
        different identifiers: Angel a numeric token, Upstox an ISIN-based
        instrument key.
        """
        errors = []
        for name in self._order():
            try:
                client = self._client(name)
                inst = client.instrument(symbol, exchange)
                bars = client.candles(exchange, inst.token, interval, start, end, tz=tz)
                if bars:
                    self.last_source = name
                    return bars
                errors.append("%s: no candles" % name)
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
                log.debug("%s failed for %s: %s", name, symbol, exc)
        log.warning("No candles for %s (%s)", symbol, "; ".join(errors))
        self.last_source = None
        return []

    def logout(self) -> None:
        for client in (self._angel, self._upstox):
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
