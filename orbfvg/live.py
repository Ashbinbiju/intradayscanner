"""
Live runner: drive the engine off Angel One's 5-minute candles.

Flow
----
1. Log in, resolve the instrument, adopt its tick size as Pine's `mintick`.
2. Warm up by replaying recent history through the engine.  This seeds ATR,
   the FVG store and the session state.  Signals produced during warm-up are
   printed but never traded -- they already happened.
3. Then, a few seconds after each 5-minute boundary, pull the freshly closed
   candle, feed it to the engine, and hand any events to the broker.

Only closed candles are fed.  Angel will happily return the candle that is
still forming, so every bar is checked against the clock before use --
feeding a partial bar would repaint the signal, which is exactly what the
indicator's "fire only on bar close" setting exists to prevent.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from .angel import AngelClient
from .broker import Broker
from .pine import SessionWindow, na
from .strategy import Bar, EventType, ORBFVGStrategy

log = logging.getLogger("orbfvg.live")

INTERVAL_SECONDS = {"FIVE_MINUTE": 300, "THREE_MINUTE": 180, "FIFTEEN_MINUTE": 900}


class LiveRunner:
    def __init__(self, strategy_settings, trade_settings, client: Optional[AngelClient] = None):
        self.s = strategy_settings
        self.t = trade_settings
        self.client = client or AngelClient()
        # Bars are handled in the exchange's timezone; the strategy converts to
        # its own session zone internally. Loop timing is an exchange concern.
        self.tz = ZoneInfo(trade_settings.exchange_tz)
        self.session_tz = ZoneInfo(strategy_settings.tzIn)
        self.bar_seconds = INTERVAL_SECONDS.get(trade_settings.interval, 300)
        self.sig_window = SessionWindow.parse(strategy_settings.sigSess)
        self.strategy: Optional[ORBFVGStrategy] = None
        self.broker: Optional[Broker] = None
        self.inst = None
        self.last_bar_time: Optional[datetime] = None
        self._stop = False

    # -- setup ------------------------------------------------------------
    def prepare(self) -> None:
        self.client.login()
        self.inst = self.client.instrument(self.t.symbol, self.t.exchange)
        if self.t.symboltoken and str(self.t.symboltoken) != self.inst.token:
            log.warning(
                "Configured SYMBOLTOKEN %s overrides resolved %s",
                self.t.symboltoken, self.inst.token,
            )
            self.inst.token = str(self.t.symboltoken)

        # Pine's syminfo.mintick comes from the instrument, not a guess.
        self.s.mintick = self.inst.tick_size
        if self.t.lot_size == 1 and self.inst.lotsize > 1:
            self.t.lot_size = self.inst.lotsize

        self.strategy = ORBFVGStrategy(self.s)
        self.broker = Broker(self.client, self.inst, self.t)

        log.info(
            "Instrument %s (%s) token=%s tick=%.2f lot=%d",
            self.inst.symbol, self.t.exchange, self.inst.token,
            self.inst.tick_size, self.t.lot_size,
        )
        mode = "DRY RUN - no orders will be sent" if self.t.dry_run else "LIVE - real orders"
        log.info("Mode: %s", mode)

    def warmup(self) -> None:
        """Replay recent history so the engine starts with correct state."""
        now = datetime.now(self.tz)
        start = now - timedelta(days=self.t.warmup_days)
        bars = self.client.candles(
            self.t.exchange, self.inst.token, self.t.interval, start, now, tz=self.tz
        )
        bars = [b for b in bars if self._is_closed(b, now)]
        if not bars:
            log.warning("No warm-up candles returned; the engine starts cold")
            return

        for bar in bars:
            for event in self.strategy.on_bar(bar):
                log.debug("warm-up %s", event)
            self.last_bar_time = bar.time

        log.info(
            "Warm-up: %d bars, %s -> %s",
            len(bars),
            bars[0].time.strftime("%Y-%m-%d %H:%M"),
            bars[-1].time.strftime("%Y-%m-%d %H:%M"),
        )
        snap = self.strategy.snapshot()
        log.info(
            "State after warm-up: range %s/%s, FVG %d bull / %d bear, %s",
            self._fmt(snap["range_high"]), self._fmt(snap["range_low"]),
            snap["fvg_bull"], snap["fvg_bear"], snap["status"],
        )
        if self.strategy.pos != 0:
            log.warning(
                "Engine carries a position from before start-up; no order was placed "
                "for it. New entries resume once that trade closes."
            )

    # -- helpers ----------------------------------------------------------
    def _fmt(self, value) -> str:
        return "-" if na(value) else "%.2f" % value

    def _is_closed(self, bar: Bar, now: datetime) -> bool:
        return bar.time + timedelta(seconds=self.bar_seconds) <= now

    def _next_boundary(self, now: datetime) -> datetime:
        epoch = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (now - epoch).total_seconds()
        return epoch + timedelta(
            seconds=(int(elapsed // self.bar_seconds) + 1) * self.bar_seconds
        )

    def _session_over(self, now: datetime) -> bool:
        """True once the exchange has closed, plus a bar of grace.

        Bounded by the market's own close rather than the strategy's signal
        window: under the London preset that window runs to 21:00 IST, hours
        after NSE stops printing bars.
        """
        try:
            hh, mm = (int(x) for x in self.t.market_close.split(":"))
        except (ValueError, AttributeError):
            return False
        close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return now > close + timedelta(seconds=self.bar_seconds * 2)

    def _square_off_due(self, now: datetime) -> bool:
        """Hard cut-off for an intraday position, independent of bar data.

        The strategy's own end-of-window exit needs a bar to print after the
        signal window. NSE's feed can stop before that happens, so this is the
        backstop that keeps a live position from running past the broker's
        auto-square-off. It only closes; it never opens.
        """
        if self.broker is None or self.broker.position is None:
            return False
        try:
            hh, mm = (int(x) for x in self.t.square_off_time.split(":"))
        except (ValueError, AttributeError):
            return False
        cutoff = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return now >= cutoff

    # -- the loop ---------------------------------------------------------
    def _fetch_new_bars(self) -> List[Bar]:
        now = datetime.now(self.tz)
        start = (self.last_bar_time or now - timedelta(hours=3)) - timedelta(minutes=5)
        bars = self.client.candles(
            self.t.exchange, self.inst.token, self.t.interval, start, now, tz=self.tz
        )
        fresh = [
            b for b in bars
            if self._is_closed(b, now)
            and (self.last_bar_time is None or b.time > self.last_bar_time)
        ]
        return fresh

    def _process(self, bars: List[Bar]) -> None:
        for bar in bars:
            events = self.strategy.on_bar(bar)
            self.last_bar_time = bar.time
            log.info(
                "%s  O%.2f H%.2f L%.2f C%.2f", bar.time.strftime("%H:%M"),
                bar.open, bar.high, bar.low, bar.close,
            )
            for event in events:
                log.info("  >> %s", event.message)
                self.broker.on_event(event)

    def run(self, forever: bool = False) -> None:
        self.prepare()
        self.warmup()

        def _handle_signal(signum, frame):
            log.info("Interrupt received, shutting down")
            self._stop = True

        signal.signal(signal.SIGINT, _handle_signal)
        try:
            signal.signal(signal.SIGTERM, _handle_signal)
        except (AttributeError, ValueError):
            pass

        log.info("Waiting for the next %d-minute close...", self.bar_seconds // 60)
        while not self._stop:
            now = datetime.now(self.tz)

            if self._square_off_due(now):
                log.warning(
                    "Square-off time %s reached with a position still open",
                    self.t.square_off_time,
                )
                try:
                    price = self.client.ltp(self.t.exchange, self.inst.symbol, self.inst.token)
                except Exception:
                    price = self.broker.position.entry
                self.broker.flatten_if_open(now, price, "SQUARE_OFF")

            if self._session_over(now) and not forever:
                log.info("Signal window is over for today")
                break

            target = self._next_boundary(now) + timedelta(seconds=self.t.poll_delay_sec)
            while datetime.now(self.tz) < target and not self._stop:
                time.sleep(min(2.0, (target - datetime.now(self.tz)).total_seconds() + 0.1))
            if self._stop:
                break

            bars: List[Bar] = []
            for attempt in range(self.t.poll_retry):
                try:
                    bars = self._fetch_new_bars()
                except Exception as exc:
                    log.error("Candle fetch failed: %s", exc)
                if bars or self._stop:
                    break
                time.sleep(self.t.poll_retry_sleep)

            if not bars:
                log.debug("No new candle yet (market may be closed)")
                continue

            try:
                self._process(bars)
            except Exception as exc:
                log.exception("Error while processing bars: %s", exc)

        self.shutdown()

    def shutdown(self) -> None:
        if self.broker is not None and self.broker.position is not None:
            try:
                price = self.client.ltp(self.t.exchange, self.inst.symbol, self.inst.token)
            except Exception:
                price = self.broker.position.entry
            self.broker.flatten_if_open(datetime.now(self.tz), price, "SHUTDOWN")

        if self.broker is not None and self.broker.trades:
            total = sum(t.pnl for t in self.broker.trades)
            log.info(
                "Session done: %d trade(s), P&L %.2f, journal %s",
                len(self.broker.trades), total, self.broker.journal_path,
            )
        else:
            log.info("Session done: no trades taken")
        self.client.logout()
