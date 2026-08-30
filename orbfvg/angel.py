"""
Angel One SmartAPI client.

Wraps `SmartConnect` with the things a live bot actually needs: TOTP login,
rate limiting, retries, chunked historical candles, and order helpers that
return a plain result object instead of raw dicts.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pyotp
from SmartApi import SmartConnect

import config
from .instruments import Instrument, resolve
from .strategy import Bar

log = logging.getLogger("orbfvg.angel")

# Published SmartAPI limits, kept a touch under the documented ceiling.
_RATE_LIMITS = {
    "candle": (3, 1.0),
    "ltp": (8, 1.0),
    "order": (15, 1.0),
    "generic": (8, 1.0),
}

# Angel caps a single historical request per interval; 5-minute data allows a
# wide window but chunking keeps responses small and retries cheap.
_CHUNK_DAYS = 30


class RateLimiter:
    """Sliding-window limiter shared across threads."""

    def __init__(self):
        self._hits: Dict[str, deque] = {k: deque() for k in _RATE_LIMITS}
        self._lock = threading.Lock()

    def acquire(self, bucket: str) -> None:
        limit, window = _RATE_LIMITS.get(bucket, _RATE_LIMITS["generic"])
        hits = self._hits.setdefault(bucket, deque())
        while True:
            with self._lock:
                now = time.monotonic()
                while hits and now - hits[0] > window:
                    hits.popleft()
                if len(hits) < limit:
                    hits.append(now)
                    return
                wait = window - (now - hits[0]) + 0.01
            time.sleep(max(wait, 0.01))


class AngelError(RuntimeError):
    pass


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str]
    raw: dict
    message: str = ""


class AngelClient:
    """Authenticated SmartAPI session with the helpers the runner needs."""

    def __init__(
        self,
        api_key: str = config.API_KEY,
        client_id: str = config.CLIENT_ID,
        password: str = config.PASSWORD,
        totp_secret: str = config.TOTP_SECRET,
    ):
        self.api_key = api_key
        self.client_id = client_id
        self.password = password
        self.totp_secret = totp_secret
        self.smart: Optional[SmartConnect] = None
        self.profile: dict = {}
        self.feed_token: Optional[str] = None
        self._logged_in_at: Optional[datetime] = None
        self._limiter = RateLimiter()

    # -- session ----------------------------------------------------------
    def login(self) -> dict:
        """Authenticate with client id + MPIN + a freshly generated TOTP."""
        missing = [n for n, v in (("CLIENT_ID", self.client_id),
                                  ("PASSWORD", self.password),
                                  ("TOTP_SECRET", self.totp_secret),
                                  ("API_KEY", self.api_key)) if not v]
        if missing:
            raise AngelError(
                "Missing Angel One credentials: %s. Set them in the environment, "
                "in .env, or in Streamlit secrets (see .env.example)."
                % ", ".join(missing)
            )
        self.smart = SmartConnect(api_key=self.api_key)
        try:
            otp = pyotp.TOTP(self.totp_secret).now()
        except Exception as exc:  # malformed base32 secret
            raise AngelError("Could not generate TOTP from TOTP_SECRET: %s" % exc) from exc

        self._limiter.acquire("generic")
        response = self.smart.generateSession(self.client_id, self.password, otp)
        if not isinstance(response, dict) or not response.get("status"):
            raise AngelError(
                "Login failed: %s"
                % (response.get("message") if isinstance(response, dict) else response)
            )

        self.profile = response.get("data", {}) or {}
        self.feed_token = self.profile.get("feedToken")
        self._logged_in_at = datetime.now()
        log.info(
            "Logged in as %s (%s)",
            self.profile.get("name") or self.client_id,
            self.profile.get("clientcode") or self.client_id,
        )
        return self.profile

    def ensure_session(self) -> None:
        """Re-login if the session is absent or older than the daily token life."""
        if self.smart is None or self._logged_in_at is None:
            self.login()
            return
        if datetime.now() - self._logged_in_at > timedelta(hours=8):
            log.info("Session is stale, re-authenticating")
            self.login()

    def _call(self, bucket: str, fn, *args, retries: int = 3, **kwargs):
        """Invoke a SmartAPI method with rate limiting and backoff."""
        last = None
        for attempt in range(retries):
            self._limiter.acquire(bucket)
            try:
                response = fn(*args, **kwargs)
            except Exception as exc:
                last = exc
                log.warning("SmartAPI call failed (%s/%s): %s", attempt + 1, retries, exc)
                time.sleep(1.5 * (attempt + 1))
                continue

            if isinstance(response, dict) and not response.get("status", True):
                message = str(response.get("message", ""))
                # Token problems are worth one re-login before giving up.
                if "token" in message.lower() or "session" in message.lower():
                    log.warning("Session rejected (%s), re-authenticating", message)
                    self.login()
                    fn = getattr(self.smart, fn.__name__)
                    last = AngelError(message)
                    continue
                if "rate" in message.lower() or "access denied" in message.lower():
                    time.sleep(2.0 * (attempt + 1))
                    last = AngelError(message)
                    continue
            return response
        raise AngelError("SmartAPI call failed after %d attempts: %s" % (retries, last))

    # -- reference data ---------------------------------------------------
    def instrument(self, symbol: str, exchange: str) -> Instrument:
        return resolve(symbol, exchange)

    # -- market data ------------------------------------------------------
    def candles(
        self,
        exchange: str,
        symboltoken: str,
        interval: str,
        start: datetime,
        end: datetime,
        tz=None,
    ) -> List[Bar]:
        """Historical candles as `Bar` objects, chunked and de-duplicated."""
        self.ensure_session()
        bars: List[Bar] = []
        seen = set()
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), end)
            params = {
                "exchange": exchange,
                "symboltoken": str(symboltoken),
                "interval": interval,
                "fromdate": cursor.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }
            response = self._call("candle", self.smart.getCandleData, params)
            rows = (response or {}).get("data") or []
            for row in rows:
                stamp = datetime.fromisoformat(row[0])
                if tz is not None:
                    stamp = stamp.astimezone(tz)
                if stamp in seen:
                    continue
                seen.add(stamp)
                bars.append(
                    Bar(
                        time=stamp,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]) if len(row) > 5 else 0.0,
                    )
                )
            cursor = chunk_end
        bars.sort(key=lambda b: b.time)
        return bars

    def ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        self.ensure_session()
        response = self._call(
            "ltp", self.smart.ltpData, exchange, tradingsymbol, str(symboltoken)
        )
        data = (response or {}).get("data") or {}
        price = data.get("ltp")
        if price is None:
            raise AngelError("No LTP returned for %s:%s" % (exchange, tradingsymbol))
        return float(price)

    # -- trading ----------------------------------------------------------
    def place_order(self, params: dict) -> OrderResult:
        self.ensure_session()
        response = self._call("order", self.smart.placeOrderFullResponse, params)
        if isinstance(response, dict) and response.get("status"):
            data = response.get("data") or {}
            return OrderResult(True, str(data.get("orderid") or ""), response,
                               str(response.get("message", "")))
        message = (
            str(response.get("message")) if isinstance(response, dict) else str(response)
        )
        return OrderResult(False, None, response if isinstance(response, dict) else {}, message)

    def modify_order(self, params: dict) -> OrderResult:
        self.ensure_session()
        response = self._call("order", self.smart.modifyOrder, params)
        ok = bool(isinstance(response, dict) and response.get("status"))
        data = (response or {}).get("data") or {}
        return OrderResult(
            ok, str(data.get("orderid") or params.get("orderid") or ""),
            response if isinstance(response, dict) else {},
            str((response or {}).get("message", "")),
        )

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> OrderResult:
        self.ensure_session()
        response = self._call("order", self.smart.cancelOrder, order_id, variety)
        ok = bool(isinstance(response, dict) and response.get("status"))
        return OrderResult(ok, order_id, response if isinstance(response, dict) else {},
                           str((response or {}).get("message", "")))

    def order_book(self) -> List[dict]:
        self.ensure_session()
        response = self._call("generic", self.smart.orderBook)
        return (response or {}).get("data") or []

    def positions(self) -> List[dict]:
        self.ensure_session()
        response = self._call("generic", self.smart.position)
        return (response or {}).get("data") or []

    def funds(self) -> dict:
        self.ensure_session()
        response = self._call("generic", self.smart.rmsLimit)
        return (response or {}).get("data") or {}

    def logout(self) -> None:
        """Terminate the session.

        Angel's logout endpoint intermittently answers AB1004 even on a valid
        session. The token expires on its own overnight, so a failure here is
        noise rather than a problem -- it is logged at debug and the SmartAPI
        library's own error print is muted for the duration of the call.
        """
        if self.smart is None:
            return
        import logzero

        previous = logzero.logger.level
        try:
            logzero.logger.setLevel(logging.CRITICAL)
            response = self.smart.terminateSession(self.client_id)
        except Exception as exc:
            log.debug("Logout call raised: %s", exc)
            return
        finally:
            logzero.logger.setLevel(previous)
            self.smart = None
            self._logged_in_at = None

        if isinstance(response, dict) and response.get("status"):
            log.info("Session terminated")
        else:
            message = (response or {}).get("message", "unknown")
            log.debug("Logout returned %s; the token will lapse on its own", message)
