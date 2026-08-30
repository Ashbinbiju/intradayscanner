"""
Broker checks -- sizing, order payloads, journal and the safety caps.

Runs entirely offline against a stub client, so it never touches the network
or the account.  Run with:  python tests/test_broker.py
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TradeSettings
from orbfvg.angel import OrderResult
from orbfvg.broker import Broker, round_to_tick
from orbfvg.instruments import Instrument
from orbfvg.strategy import Event, EventType

IST = ZoneInfo("Asia/Kolkata")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


class StubClient:
    """Records payloads instead of sending them."""

    def __init__(self, modify_ok=True):
        self.orders = []
        self.cancels = []
        self.modifies = []
        self.counter = 0
        self.modify_ok = modify_ok

    def place_order(self, params):
        self.counter += 1
        self.orders.append(params)
        return OrderResult(True, "ORD%03d" % self.counter, {}, "ok")

    def cancel_order(self, order_id, variety="NORMAL"):
        self.cancels.append((order_id, variety))
        return OrderResult(True, order_id, {}, "ok")

    def modify_order(self, params):
        self.modifies.append(params)
        return OrderResult(self.modify_ok, params.get("orderid"), {},
                           "ok" if self.modify_ok else "rejected")


INSTRUMENT = Instrument(
    token="3045", symbol="SBIN-EQ", name="SBIN", exch_seg="NSE",
    lotsize=1, tick_size=0.05,
)


def stop_moved(price, when=None):
    return Event(
        EventType.STOP_MOVED, when or datetime(2026, 8, 28, 9, 45, tzinfo=IST), 11,
        "trail", side="BUY", price=price, stop=price,
    )


def make_broker(client=None, **overrides):
    t = TradeSettings()
    t.symbol, t.exchange = "SBIN-EQ", "NSE"
    t.dry_run = False           # exercise the real payload path against the stub
    t.use_broker_sl = False
    t.sizing_mode = "fixed"
    t.quantity = 10
    for k, v in overrides.items():
        setattr(t, k, v)
    client = client or StubClient()
    broker = Broker(client, INSTRUMENT, t, journal_path=os.devnull)
    return broker, client


def entry_event(side="BUY", price=100.0, stop=98.0, when=None):
    when = when or datetime(2026, 8, 28, 9, 35, tzinfo=IST)
    direction = 1 if side == "BUY" else -1
    risk = abs(price - stop)
    return Event(
        EventType.ENTRY, when, 10, "%s test" % side, side=side, price=price, stop=stop,
        t1=price + direction * risk, t2=price + direction * 2 * risk,
        t3=price + direction * 3 * risk, risk=risk,
    )


# ---------------------------------------------------------------------------
print("\nTick rounding")
check("rounds to the instrument tick", round_to_tick(101.03, 0.05) == 101.05,
      str(round_to_tick(101.03, 0.05)))
check("leaves an exact tick alone", round_to_tick(101.10, 0.05) == 101.10)

# ---------------------------------------------------------------------------
print("\nPosition sizing")
b, _ = make_broker(sizing_mode="fixed", quantity=7)
check("fixed mode uses the configured quantity", b.size_for(100, 98) == 7)

b, _ = make_broker(sizing_mode="risk", capital=100000, risk_per_trade_pct=1.0)
# 1% of 100000 = 1000 budget, 2.00 risk per unit -> 500 units
check("risk mode divides budget by per-unit risk", b.size_for(100, 98) == 500,
      str(b.size_for(100, 98)))

b, _ = make_broker(sizing_mode="risk", capital=100000, risk_per_trade_pct=1.0, max_quantity=100)
check("max_quantity caps the size", b.size_for(100, 98) == 100)

b, _ = make_broker(sizing_mode="risk", capital=100000, risk_per_trade_pct=1.0, lot_size=75)
check("lot size rounds down to whole lots", b.size_for(100, 98) == 450,
      str(b.size_for(100, 98)))

b, _ = make_broker(sizing_mode="risk", capital=1000, risk_per_trade_pct=0.1)
check("unaffordable risk yields zero", b.size_for(100, 98) == 0)

# ---------------------------------------------------------------------------
print("\nEntry order payload")
b, c = make_broker(quantity=10)
b.on_event(entry_event("BUY", 100.0, 98.0))
check("one order sent", len(c.orders) == 1)
o = c.orders[0]
check("transaction side is BUY", o["transactiontype"] == "BUY")
check("market order by default", o["ordertype"] == "MARKET")
check("quantity is a string", o["quantity"] == "10")
check("token and symbol carried", o["symboltoken"] == "3045" and o["tradingsymbol"] == "SBIN-EQ")
check("product type is intraday", o["producttype"] == "INTRADAY")
check("position recorded", b.position is not None and b.position.quantity == 10)

b, c = make_broker(quantity=10, ordertype="LIMIT")
b.on_event(entry_event("BUY", 100.03, 98.0))
check("limit order carries a tick-rounded price",
      c.orders[0]["ordertype"] == "LIMIT" and c.orders[0]["price"] == "100.05",
      c.orders[0]["price"])

# ---------------------------------------------------------------------------
print("\nProtective stop")
b, c = make_broker(quantity=10, use_broker_sl=True)
b.on_event(entry_event("BUY", 100.0, 98.02))
check("entry plus protective SL sent", len(c.orders) == 2)
sl = c.orders[1]
check("protective SL is the opposite side", sl["transactiontype"] == "SELL")
check("protective SL is STOPLOSS_MARKET", sl["ordertype"] == "STOPLOSS_MARKET")
check("trigger rounded to tick", sl["triggerprice"] == "98.00", sl["triggerprice"])
check("variety is STOPLOSS", sl["variety"] == "STOPLOSS")
check("sl order id retained", b.position.sl_order_id == "ORD002")

# closing should cancel the protective order before sending the exit
b.on_event(Event(EventType.STOP_HIT, datetime(2026, 8, 28, 10, 0, tzinfo=IST), 12,
                 "SL", side="BUY", price=98.02, closes_position=True))
check("protective SL cancelled on exit", c.cancels == [("ORD002", "STOPLOSS")], str(c.cancels))
check("exit order sent", len(c.orders) == 3 and c.orders[2]["transactiontype"] == "SELL")
check("position cleared", b.position is None)

# ---------------------------------------------------------------------------
print("\nTrade accounting")
b, c = make_broker(quantity=10)
b.on_event(entry_event("BUY", 100.0, 98.0))
b.on_event(Event(EventType.TARGET_HIT, datetime(2026, 8, 28, 10, 0, tzinfo=IST), 12,
                 "T1", side="BUY", price=102.0, target_no=1, closes_position=False))
check("T1 does not close the position", b.position is not None)
b.on_event(Event(EventType.TARGET_HIT, datetime(2026, 8, 28, 10, 5, tzinfo=IST), 13,
                 "T3", side="BUY", price=106.0, target_no=3, closes_position=True))
check("T3 closes the position", b.position is None)
tr = b.trades[-1]
check("P&L = points x quantity", abs(tr.pnl - 60.0) < 1e-9, str(tr.pnl))
check("R multiple recorded", abs(tr.r_multiple - 3.0) < 1e-9, str(tr.r_multiple))
check("targets hit recorded", tr.targets_hit == 3)
check("exit reason recorded", tr.reason == "T3")

# short side
b, c = make_broker(quantity=5)
b.on_event(entry_event("SELL", 100.0, 102.0))
check("short entry sends SELL", c.orders[0]["transactiontype"] == "SELL")
b.on_event(Event(EventType.SESSION_EXIT, datetime(2026, 8, 28, 15, 15, tzinfo=IST), 70,
                 "session", side="SELL", price=97.0, closes_position=True))
check("short exit sends BUY", c.orders[1]["transactiontype"] == "BUY")
check("short P&L is positive when price falls",
      abs(b.trades[-1].pnl - 15.0) < 1e-9, str(b.trades[-1].pnl))

# ---------------------------------------------------------------------------
print("\nTrailing stop")
b, c = make_broker(quantity=10, use_broker_sl=True)
b.on_event(entry_event("BUY", 100.0, 98.0))
b.on_event(stop_moved(100.0))
check("engine stop tracked on the position", b.position.stop == 100.0)
check("protective order modified, not replaced",
      len(c.modifies) == 1 and c.cancels == [], "%s / %s" % (c.modifies, c.cancels))
m = c.modifies[0]
check("modify targets the existing sl order id", m["orderid"] == "ORD002")
check("modify carries the new trigger", m["triggerprice"] == "100.00", m["triggerprice"])
check("modify keeps order type and quantity",
      m["ordertype"] == "STOPLOSS_MARKET" and m["quantity"] == "10")

# stopped out after trailing -> journal reason distinguishes it from a plain SL
b.on_event(Event(EventType.STOP_HIT, datetime(2026, 8, 28, 10, 0, tzinfo=IST), 12,
                 "SL", side="BUY", price=100.0, closes_position=True))
check("trailed exit is labelled TRAIL", b.trades[-1].reason == "TRAIL")
check("final stop recorded", b.trades[-1].final_stop == 100.0)
check("initial stop preserved for R", b.trades[-1].stop == 98.0)
check("breakeven exit is flat P&L", abs(b.trades[-1].pnl) < 1e-9)

# a rejected modify must not leave a stale backstop
b, c = make_broker(client=StubClient(modify_ok=False), quantity=10, use_broker_sl=True)
b.on_event(entry_event("BUY", 100.0, 98.0))
b.on_event(stop_moved(100.0))
check("rejected modify falls back to cancel + replace",
      len(c.cancels) == 1 and len(c.orders) == 3, "%s / %d" % (c.cancels, len(c.orders)))
check("replacement sits at the new stop",
      c.orders[2]["triggerprice"] == "100.00", c.orders[2]["triggerprice"])

# untrailed stop still reports as SL
b, c = make_broker(quantity=10)
b.on_event(entry_event("BUY", 100.0, 98.0))
b.on_event(Event(EventType.STOP_HIT, datetime(2026, 8, 28, 10, 0, tzinfo=IST), 12,
                 "SL", side="BUY", price=98.0, closes_position=True))
check("untrailed exit is labelled SL", b.trades[-1].reason == "SL")

# with no broker-side stop there is nothing to modify, but state still tracks
b, c = make_broker(quantity=10, use_broker_sl=False)
b.on_event(entry_event("BUY", 100.0, 98.0))
b.on_event(stop_moved(100.0))
check("no modify sent when broker SL is off",
      c.modifies == [] and b.position.stop == 100.0)

# ---------------------------------------------------------------------------
print("\nSafety caps")
b, c = make_broker(quantity=10, max_trades_per_day=2)
day = datetime(2026, 8, 28, 9, 35, tzinfo=IST)
for i in range(3):
    b.on_event(entry_event("BUY", 100.0, 98.0, when=day + timedelta(minutes=5 * i)))
    if b.position:
        b.on_event(Event(EventType.STOP_HIT, day + timedelta(minutes=5 * i + 1), 10 + i,
                         "SL", side="BUY", price=98.0, closes_position=True))
check("daily trade cap respected", len(b.trades) == 2, "%d trades" % len(b.trades))

b, c = make_broker(quantity=10, max_trades_per_day=2)
b.on_event(entry_event("BUY", 100.0, 98.0, when=day))
b.on_event(entry_event("BUY", 101.0, 99.0, when=day + timedelta(minutes=5)))
check("second entry while in position is ignored", len(c.orders) == 1)

b, c = make_broker(quantity=10, max_trades_per_day=1)
b.on_event(entry_event("BUY", 100.0, 98.0, when=day))
b.on_event(Event(EventType.STOP_HIT, day + timedelta(minutes=5), 11, "SL",
                 side="BUY", price=98.0, closes_position=True))
b.on_event(entry_event("BUY", 100.0, 98.0, when=day + timedelta(days=1)))
check("cap resets on the next day", b.position is not None)

# ---------------------------------------------------------------------------
print("\nDry run")
b, c = make_broker(dry_run=True, use_broker_sl=True, quantity=10)
b.on_event(entry_event("BUY", 100.0, 98.0))
check("dry run sends nothing to the client", c.orders == [])
check("dry run still tracks the position", b.position is not None)
b.on_event(Event(EventType.STOP_HIT, datetime(2026, 8, 28, 10, 0, tzinfo=IST), 12,
                 "SL", side="BUY", price=98.0, closes_position=True))
check("dry run records the trade", len(b.trades) == 1 and b.trades[0].pnl == -20.0)
check("dry run cancels nothing for real", c.cancels == [])

# ---------------------------------------------------------------------------
print("")
if FAILURES:
    print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All broker checks passed.")
