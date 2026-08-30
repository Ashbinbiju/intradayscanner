"""
Turns strategy events into Angel One orders.

The engine in `strategy.py` is the decision-maker: it decides when to enter,
when a target is reached and when the stop is breached, exactly as the
indicator does.  This module only executes those decisions and keeps the
broker's view of the position in sync.

Two safety features sit on top, neither of which can create or suppress a
signal:

  * `dry_run` logs the fully-formed order payload without sending it.
  * `use_broker_sl` parks an SL-M order at the engine's stop so an adverse
    move between 5-minute closes is not left unprotected.  When the engine
    closes the trade for any reason, that order is cancelled first.
"""

from __future__ import annotations

import csv
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

import config
from .angel import AngelClient, OrderResult
from .strategy import Event, EventType

log = logging.getLogger("orbfvg.broker")


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 2)


@dataclass
class Position:
    side: str            # "BUY" or "SELL"
    quantity: int
    entry: float
    stop: float
    t1: float
    t2: float
    t3: float
    opened_at: datetime
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1


@dataclass
class TradeRecord:
    symbol: str
    side: str
    quantity: int
    entry_time: datetime
    entry: float
    exit_time: Optional[datetime] = None
    exit: float = float("nan")
    reason: str = ""
    stop: float = float("nan")          # as first set; R is measured from this
    final_stop: float = float("nan")    # after trailing
    targets_hit: int = 0
    pnl: float = float("nan")
    r_multiple: float = float("nan")


class Broker:
    """Executes engine events against Angel One (or logs them in dry run)."""

    OPPOSITE = {"BUY": "SELL", "SELL": "BUY"}

    def __init__(self, client: AngelClient, instrument, trade_settings, journal_path=None):
        self.client = client
        self.inst = instrument
        self.t = trade_settings
        self.position: Optional[Position] = None
        self.trades: List[TradeRecord] = []
        self._open_trade: Optional[TradeRecord] = None
        self._trades_today = 0
        self._today: Optional[date] = None
        self._stop_moved = False
        self.journal_path = journal_path or os.path.join(
            config.LOG_DIR, "trades_%s.csv" % datetime.now().strftime("%Y%m%d")
        )

    # -- sizing -----------------------------------------------------------
    def size_for(self, entry: float, stop: float) -> int:
        """Quantity for this trade, honouring lot size and the hard cap."""
        if self.t.sizing_mode == "fixed":
            qty = self.t.quantity
        else:
            risk_per_unit = abs(entry - stop)
            if risk_per_unit <= 0:
                return 0
            budget = self.t.capital * self.t.risk_per_trade_pct / 100.0
            qty = int(math.floor(budget / risk_per_unit))

        if self.t.lot_size > 1:
            qty = (qty // self.t.lot_size) * self.t.lot_size
        return max(0, min(qty, self.t.max_quantity))

    # -- order payloads ---------------------------------------------------
    def _base_params(self, transaction: str, quantity: int) -> dict:
        return {
            "variety": self.t.variety,
            "tradingsymbol": self.inst.symbol,
            "symboltoken": str(self.inst.token),
            "transactiontype": transaction,
            "exchange": self.t.exchange,
            "ordertype": "MARKET",
            "producttype": self.t.producttype,
            "duration": self.t.duration,
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }

    def _send(self, params: dict, label: str) -> OrderResult:
        if self.t.dry_run:
            log.info("[DRY RUN] %s -> %s", label, params)
            return OrderResult(True, "DRY-%s" % label, {}, "dry run")
        result = self.client.place_order(params)
        if result.ok:
            log.info("%s placed, order id %s", label, result.order_id)
        else:
            log.error("%s REJECTED: %s", label, result.message)
        return result

    # -- lifecycle --------------------------------------------------------
    def _roll_day(self, moment: datetime) -> None:
        if self._today != moment.date():
            self._today = moment.date()
            self._trades_today = 0

    def on_event(self, event: Event) -> None:
        self._roll_day(event.time)
        if event.type is EventType.ENTRY:
            self._open(event)
        elif event.type is EventType.TARGET_HIT:
            if self._open_trade is not None:
                self._open_trade.targets_hit = max(
                    self._open_trade.targets_hit, event.target_no or 0
                )
            if event.closes_position:
                self._close(event, "T%d" % (event.target_no or 3))
        elif event.type is EventType.STOP_MOVED:
            self._move_stop(event)
        elif event.type is EventType.STOP_HIT:
            self._close(event, "TRAIL" if self._stop_moved else "SL")
        elif event.type is EventType.SESSION_EXIT:
            self._close(event, "SESSION_END")

    def _open(self, event: Event) -> None:
        if self.position is not None:
            log.warning("Entry signal while already in a position; ignoring")
            return
        if self._trades_today >= self.t.max_trades_per_day:
            log.warning(
                "Daily trade cap reached (%d), skipping entry", self.t.max_trades_per_day
            )
            return

        quantity = self.size_for(event.price, event.stop)
        if quantity < 1:
            log.warning(
                "Computed quantity is 0 (risk %.2f per unit); skipping entry",
                abs(event.price - event.stop),
            )
            return

        params = self._base_params(event.side, quantity)
        if self.t.ordertype == "LIMIT":
            params["ordertype"] = "LIMIT"
            params["price"] = "%.2f" % round_to_tick(event.price, self.inst.tick_size)

        result = self._send(params, "ENTRY %s x%d" % (event.side, quantity))
        if not result.ok:
            return

        self.position = Position(
            side=event.side,
            quantity=quantity,
            entry=event.price,
            stop=event.stop,
            t1=event.t1,
            t2=event.t2,
            t3=event.t3,
            opened_at=event.time,
            entry_order_id=result.order_id,
        )
        self._open_trade = TradeRecord(
            symbol=self.inst.symbol,
            side=event.side,
            quantity=quantity,
            entry_time=event.time,
            entry=event.price,
            stop=event.stop,
        )
        self._trades_today += 1
        self._stop_moved = False
        log.info(
            "OPEN  %s %s x%d @ %.2f | SL %.2f | T1 %.2f T2 %.2f T3 %.2f",
            self.inst.symbol, event.side, quantity, event.price,
            event.stop, event.t1, event.t2, event.t3,
        )

        if self.t.use_broker_sl:
            self._place_protective_stop()

    def _move_stop(self, event: Event) -> None:
        """Follow the engine's trailing stop with the broker-side SL-M order."""
        pos = self.position
        if pos is None:
            return
        pos.stop = event.stop
        self._stop_moved = True
        log.info("TRAIL stop -> %.2f", event.stop)

        if not self.t.use_broker_sl or not pos.sl_order_id:
            return

        trigger = round_to_tick(event.stop, self.inst.tick_size)
        params = {
            "variety": "STOPLOSS",
            "orderid": pos.sl_order_id,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": self.t.producttype,
            "duration": self.t.duration,
            "quantity": str(pos.quantity),
            "tradingsymbol": self.inst.symbol,
            "symboltoken": str(self.inst.token),
            "exchange": self.t.exchange,
            "triggerprice": "%.2f" % trigger,
            "price": "%.2f" % trigger,
        }
        if self.t.dry_run:
            log.info("[DRY RUN] MODIFY SL %s -> %.2f", pos.sl_order_id, trigger)
            return
        result = self.client.modify_order(params)
        if not result.ok:
            # A rejected modify would leave the backstop at the old level, so
            # replace it outright rather than trade on a stale stop.
            log.warning("SL modify rejected (%s); replacing the order", result.message)
            self._cancel_protective_stop()
            self._place_protective_stop()

    def _place_protective_stop(self) -> None:
        """SL-M backstop at the engine's stop price."""
        pos = self.position
        if pos is None:
            return
        trigger = round_to_tick(pos.stop, self.inst.tick_size)
        params = self._base_params(self.OPPOSITE[pos.side], pos.quantity)
        params.update(
            {
                "variety": "STOPLOSS",
                "ordertype": "STOPLOSS_MARKET",
                "triggerprice": "%.2f" % trigger,
                "price": "%.2f" % trigger,
            }
        )
        result = self._send(params, "PROTECTIVE SL @ %.2f" % trigger)
        if result.ok:
            pos.sl_order_id = result.order_id

    def _cancel_protective_stop(self) -> None:
        pos = self.position
        if pos is None or not pos.sl_order_id:
            return
        if self.t.dry_run:
            log.info("[DRY RUN] cancel protective SL %s", pos.sl_order_id)
        else:
            result = self.client.cancel_order(pos.sl_order_id, "STOPLOSS")
            if not result.ok:
                log.warning("Could not cancel protective SL: %s", result.message)
        pos.sl_order_id = None

    def _close(self, event: Event, reason: str) -> None:
        pos = self.position
        if pos is None:
            return

        # Cancel the backstop first so the exit cannot double-fill.
        self._cancel_protective_stop()

        params = self._base_params(self.OPPOSITE[pos.side], pos.quantity)
        self._send(params, "EXIT %s x%d (%s)" % (self.OPPOSITE[pos.side], pos.quantity, reason))

        exit_price = event.price
        pnl = (exit_price - pos.entry) * pos.direction * pos.quantity
        risk = abs(pos.entry - pos.stop)
        record = self._open_trade
        if record is not None:
            record.exit_time = event.time
            record.exit = exit_price
            record.reason = reason
            record.final_stop = pos.stop
            record.pnl = pnl
            record.r_multiple = (
                (exit_price - pos.entry) * pos.direction / risk if risk > 0 else float("nan")
            )
            self.trades.append(record)
            self._write_journal(record)

        log.info(
            "CLOSE %s %s x%d @ %.2f (%s)  P&L %.2f",
            self.inst.symbol, pos.side, pos.quantity, exit_price, reason, pnl,
        )
        self.position = None
        self._open_trade = None

    # -- journal ----------------------------------------------------------
    def _write_journal(self, record: TradeRecord) -> None:
        header = [
            "symbol", "side", "quantity", "entry_time", "entry", "stop",
            "final_stop", "exit_time", "exit", "reason", "targets_hit",
            "pnl", "r_multiple",
        ]
        exists = os.path.exists(self.journal_path)
        try:
            with open(self.journal_path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if not exists:
                    writer.writerow(header)
                writer.writerow([
                    record.symbol, record.side, record.quantity,
                    record.entry_time.strftime("%Y-%m-%d %H:%M"), "%.2f" % record.entry,
                    "%.2f" % record.stop, "%.2f" % record.final_stop,
                    record.exit_time.strftime("%Y-%m-%d %H:%M") if record.exit_time else "",
                    "%.2f" % record.exit, record.reason, record.targets_hit,
                    "%.2f" % record.pnl, "%.3f" % record.r_multiple,
                ])
        except OSError as exc:
            log.warning("Could not write trade journal: %s", exc)

    # -- reconciliation ---------------------------------------------------
    def flatten_if_open(self, moment: datetime, price: float, reason: str = "SHUTDOWN") -> None:
        """Emergency square-off, used when the runner stops with a live trade."""
        if self.position is None:
            return
        log.warning("Flattening open position (%s)", reason)
        self._close(
            Event(EventType.SESSION_EXIT, moment, -1, reason, price=price), reason
        )
