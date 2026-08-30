"""
Configuration for the ORB + FVG bot on Angel One SmartAPI.

`StrategySettings` mirrors the indicator's inputs one-for-one -- same names,
same defaults, same units. The trading logic reads only from here, so the
strategy code itself never needs editing to retune anything.

The only values that differ from the Pine defaults are the three session
inputs, and only because the original ships with a London-forex preset. Both
presets are below; `PRESET` selects which one loads. Everything else is
byte-for-byte the Pine default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Dict, Optional

# ===========================================================================
#  Angel One credentials
# ===========================================================================
#  Read from the environment only. A TOTP secret plus an MPIN is full access to
#  the trading account, and anything written here would end up in git history,
#  which is permanent. Put them in .env (git-ignored) or the shell instead.
_BASE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path=None):
    """Minimal .env reader, so there is no dependency just for this."""
    path = path or os.path.join(_BASE, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID", "")
PASSWORD = os.getenv("PASSWORD", "")          # Angel One MPIN
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
API_KEY = os.getenv("API_KEY", "")


def require_credentials() -> None:
    """Fail with a useful message rather than a confusing API error."""
    missing = [n for n, v in (("CLIENT_ID", CLIENT_ID), ("PASSWORD", PASSWORD),
                              ("TOTP_SECRET", TOTP_SECRET), ("API_KEY", API_KEY)) if not v]
    if missing:
        raise RuntimeError(
            "Missing Angel One credentials: %s.\nSet them in the environment, "
            "in %s, or in Streamlit secrets. See .env.example."
            % (", ".join(missing), os.path.join(_BASE, ".env"))
        )


# ===========================================================================
#  Session presets
# ===========================================================================
#  "forex" -- the indicator's shipped defaults, and the DEFAULT here. The
#             London open window lands on 12:30-12:45 IST (BST = UTC+1), so on
#             an NSE chart the opening range is built from the 12:30 / 12:35 /
#             12:40 bars and entries run from 12:45 IST onward. This is what the
#             indicator does on TradingView with untouched inputs.
#  "nse"     -- opening range from the 09:15 market open instead. A different
#             strategy in practice, not a port detail. Opt in with --preset nse.
PRESETS: Dict[str, Dict[str, str]] = {
    "forex": {
        "tzIn": "Europe/London",
        "orSess": "0800-0815",
        "sigSess": "0815-1630",
    },
    "nse": {
        "tzIn": "Asia/Kolkata",
        "orSess": "0915-0930",
        "sigSess": "0930-1510",
    },
}

PRESET = os.getenv("PRESET", "forex")


@dataclass
class StrategySettings:
    """Every input from the Pine indicator, with its original default."""

    # -- (1) Session ------------------------------------------------------
    tzIn: str = "Asia/Kolkata"
    orSess: str = "0915-0930"
    sigSess: str = "0930-1515"
    exitEnd: bool = True                # close open trade at end of signal window
    showLn: bool = True                 # draw range high/low lines (reporting only)
    showBx: bool = True                 # draw range box (reporting only)

    # -- (2) FVG ----------------------------------------------------------
    useFvg: bool = True                 # require FVG confirmation
    fvgLB: int = 12                     # FVG stays valid for N bars
    fvgUnmit: bool = True               # only count unmitigated FVGs
    fvgAtrX: float = 0.15               # minimum FVG size x ATR
    useDisp: bool = False               # require displacement candle
    dispX: float = 0.80                 # displacement x ATR
    showFvg: bool = True                # draw FVG boxes (reporting only)

    # -- (3) Entry / stop / targets ---------------------------------------
    allowL: bool = True
    allowS: bool = True
    brkMode: str = "Close beyond level"     # or "Wick touch"
    bufPct: float = 5.0                     # breakout buffer, % of range width
    confirm: bool = True                    # fire only on bar close (no repaint)
    oncePer: bool = True                    # max 1 signal per side per session
    minPips: float = 0.0                    # skip session if range < N pips
    slMode: str = "ATR"                     # ATR | Opposite range level | FVG far edge | Fixed pips
    atrLen: int = 14
    #  DEVIATION FROM THE INDICATOR: Pine ships atrMult = 1.5. Measured over
    #  132 trades on 12 sessions, that stop sits inside ordinary noise and is
    #  speared before the move develops -- 73 stop-outs, 35.7% win, -12.45R.
    #  At 3.0 it is 26 stop-outs, 44.9% win, +3.43R, keeping 92% of the trades.
    #  The curve peaks here and turns negative by 4.0, so it is a real optimum.
    #  Use --set atrMult=1.5 to reproduce TradingView.
    atrMult: float = 3.0
    slPips: float = 15.0
    r1: float = 1.0
    r2: float = 2.0
    r3: float = 3.0
    projN: int = 40                         # level line length (reporting only)
    cleanUp: bool = True                    # reporting only

    # -- (5) Trailing stop ------------------------------------------------
    #  NOT part of the original indicator, which holds one fixed stop from
    #  entry to exit.  Set `useTrail = False` to get that behaviour back
    #  exactly.  The stop only ever tightens, and trailing never moves a
    #  target or creates a signal.
    #  OFF by default: trailing never showed a return edge in testing (total R
    #  flat across 126 trades) and on WOCKPHARMA 28 Aug 2026 it cut a +1.72R
    #  short to +0.26R by jumping the stop to 1991.43 the moment T1 was hit.
    #  With this off the bot matches the indicator as validated on TradingView.
    #  Opt in with --set useTrail=true; it still buys ~35% less drawdown.
    useTrail: bool = False
    trailMode: str = "ATR"          # Targets | ATR | Both
    #  "Targets": step the stop up the ladder the strategy already draws.
    trailBE: bool = True            # T1 hit -> stop to entry (breakeven)
    trailStep: bool = True          # T2 hit -> stop to T1
    #  "ATR": classic volatility trail from the best price reached so far.
    trailAtrMult: float = 2.0
    trailStartR: float = 1.0        # only start trailing after this much open R

    # -- (7) Fakeout filters ----------------------------------------------
    #  All additions, all OFF by default -- the zero/false value in each case
    #  is exactly the indicator's behaviour. Each targets the same failure:
    #  a breakout bar that clears the level and immediately reverses.
    #
    #  confirmBars = 2 waits one extra bar: the level must still be held at the
    #  next close. Costs entry price, kills the one-bar poke.
    confirmBars: int = 1
    #  Breakout bar must close in the top (long) / bottom (short) fraction of
    #  its own range. 0.5 = close in the upper (or lower) half -- rejects the
    #  bar that pokes through the level and closes back near the far end.
    #  ON by default alongside atrMult=3.0: together they took the same 132
    #  trades from -12.45R to +8.60R while keeping 85% of them. Set to 0 to
    #  disable. This is an addition, not part of the indicator.
    strongClose: float = 0.5
    #  Breakout bar volume must be at least this multiple of the recent
    #  average. Real breakouts carry volume; pokes usually do not.
    volMult: float = 0.0
    volLen: int = 20
    #  Skip the session when the opening range is thinner than this % of price.
    #  A narrow range gets speared by ordinary noise.
    minRangePct: float = 0.0
    #  Let the opposite-side entry fire on the very bar a stop closed the
    #  position. Pine checks entries before exits, so a reversal that triggers
    #  on the same bar as the stop is lost permanently -- the cross never
    #  repeats. This recovers it.
    reentrySameBar: bool = False

    # -- (6) Hard square-off ----------------------------------------------
    #  Also not in the indicator.  With the London session on an NSE chart the
    #  signal window runs to 16:30 London = 21:00 IST, long after NSE closes,
    #  so `sessEnd` never fires on the day and a trade would ride to the next
    #  morning.  This closes it at a wall-clock time in the exchange's own
    #  timezone, and blocks new entries from that moment.
    #
    #  Set sqOffTime = "" to disable and reproduce TradingView exactly.
    sqOffTime: str = "15:15"
    sqOffTz: str = "Asia/Kolkata"

    # -- (4) Visuals ------------------------------------------------------
    pipMode: str = "Auto"                   # Auto | 0.0001 | 0.01 | 0.1 | 1
    showTbl: bool = True
    showTf: bool = True

    # -- Instrument precision --------------------------------------------
    #  Pine's syminfo.mintick. NSE cash equities and index derivatives tick
    #  in paise. Only the "FVG far edge" stop mode consumes it.
    mintick: float = 0.05

    def resolved(self) -> "StrategySettings":
        """Apply the selected session preset over the dataclass defaults."""
        preset = PRESETS.get(PRESET)
        if preset is None:
            raise ValueError(
                "Unknown PRESET %r; expected one of %s" % (PRESET, sorted(PRESETS))
            )
        for key, value in preset.items():
            setattr(self, key, value)
        return self

    def validate(self) -> None:
        if self.brkMode not in ("Close beyond level", "Wick touch"):
            raise ValueError("brkMode must be 'Close beyond level' or 'Wick touch'")
        if self.slMode not in (
            "ATR",
            "Opposite range level",
            "FVG far edge",
            "Fixed pips",
        ):
            raise ValueError("slMode %r is not one of the four Pine options" % self.slMode)
        if self.pipMode not in ("Auto", "0.0001", "0.01", "0.1", "1"):
            raise ValueError("pipMode %r is not one of the five Pine options" % self.pipMode)
        if self.atrLen < 1:
            raise ValueError("atrLen must be >= 1")
        if not (1 <= self.fvgLB <= 200):
            raise ValueError("fvgLB must be between 1 and 200")
        if self.fvgAtrX < 0:
            raise ValueError("fvgAtrX must be >= 0")
        if min(self.r1, self.r2, self.r3) < 0.1:
            raise ValueError("target R multiples must be >= 0.1")
        if self.trailMode not in ("Targets", "ATR", "Both"):
            raise ValueError("trailMode must be 'Targets', 'ATR' or 'Both'")
        if self.trailAtrMult <= 0:
            raise ValueError("trailAtrMult must be > 0")
        if self.trailStartR < 0:
            raise ValueError("trailStartR must be >= 0")
        if self.confirmBars < 1:
            raise ValueError("confirmBars must be >= 1")
        if not (0.0 <= self.strongClose <= 1.0):
            raise ValueError("strongClose must be between 0 and 1")
        if self.volMult < 0 or self.volLen < 1:
            raise ValueError("volMult must be >= 0 and volLen >= 1")
        if self.minRangePct < 0:
            raise ValueError("minRangePct must be >= 0")
        if self.mintick <= 0:
            raise ValueError("mintick must be > 0")

    def as_dict(self) -> dict:
        return asdict(self)


# ===========================================================================
#  Instrument + execution
# ===========================================================================
@dataclass
class TradeSettings:
    """Everything the broker layer needs. None of it alters strategy logic."""

    # -- What to trade ----------------------------------------------------
    exchange: str = os.getenv("EXCHANGE", "NSE")
    symbol: str = os.getenv("SYMBOL", "RELIANCE-EQ")
    symboltoken: Optional[str] = os.getenv("SYMBOLTOKEN") or None  # auto-resolved if unset
    interval: str = "FIVE_MINUTE"       # the indicator is a 5-minute system

    # -- Order routing ----------------------------------------------------
    producttype: str = os.getenv("PRODUCTTYPE", "INTRADAY")
    variety: str = "NORMAL"
    duration: str = "DAY"
    ordertype: str = os.getenv("ORDERTYPE", "MARKET")   # MARKET | LIMIT

    # -- Position sizing --------------------------------------------------
    #  "fixed"    -- always `quantity` units
    #  "risk"     -- floor(capital * risk_per_trade_pct / 100 / per-unit risk),
    #                which is the distance from entry to the stop
    sizing_mode: str = os.getenv("SIZING_MODE", "fixed")
    quantity: int = int(os.getenv("QUANTITY", "1"))
    capital: float = float(os.getenv("CAPITAL", "100000"))
    risk_per_trade_pct: float = float(os.getenv("RISK_PCT", "1.0"))
    max_quantity: int = int(os.getenv("MAX_QUANTITY", "10000"))
    lot_size: int = int(os.getenv("LOT_SIZE", "1"))     # >1 rounds down to whole lots

    # -- Safety -----------------------------------------------------------
    #  Dry run places no orders; it logs exactly what it would have sent.
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
    #  A broker-side SL-M order mirroring the engine stop, so an adverse gap is
    #  not left unprotected between 5-minute bar closes. The engine remains the
    #  decision-maker; this is a backstop it cancels when the trade ends.
    use_broker_sl: bool = os.getenv("USE_BROKER_SL", "true").lower() in ("1", "true", "yes")
    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "4"))
    #  Hard square-off, enforced by the runner rather than the strategy. The
    #  indicator assumes `exitEnd` always closes the trade, but that depends on
    #  a bar printing after the signal window -- and NSE data can simply stop.
    #  This guarantees an intraday position is flat before the broker's own
    #  auto-square-off. It never creates or suppresses a signal.
    square_off_time: str = os.getenv("SQUARE_OFF", "15:15")
    #  The exchange's own wall clock. The strategy session may be quoted in a
    #  different zone entirely (the London preset runs to 21:00 IST), so the
    #  live loop and the square-off must not read times in the session's zone.
    exchange_tz: str = os.getenv("EXCHANGE_TZ", "Asia/Kolkata")
    market_close: str = os.getenv("MARKET_CLOSE", "15:30")

    # -- Live loop --------------------------------------------------------
    #  Angel publishes a completed candle a moment after the boundary; poll
    #  from `poll_delay_sec` past the close and retry until it lands.
    poll_delay_sec: int = 12
    poll_retry: int = 6
    poll_retry_sleep: float = 5.0
    warmup_days: int = int(os.getenv("WARMUP_DAYS", "6"))

    def validate(self) -> None:
        if self.ordertype not in ("MARKET", "LIMIT"):
            raise ValueError("ordertype must be MARKET or LIMIT")
        if self.sizing_mode not in ("fixed", "risk"):
            raise ValueError("sizing_mode must be 'fixed' or 'risk'")
        if self.quantity < 1:
            raise ValueError("quantity must be >= 1")
        if self.lot_size < 1:
            raise ValueError("lot_size must be >= 1")
        if self.producttype not in ("INTRADAY", "DELIVERY", "CARRYFORWARD", "MARGIN"):
            raise ValueError("unsupported producttype %r" % self.producttype)


# ===========================================================================
#  Paths
# ===========================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
SCRIP_MASTER_CACHE = os.path.join(DATA_DIR, "scrip_master.json")
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

for _d in (DATA_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


def load() -> tuple:
    """Build, apply the preset to, and validate both settings objects."""
    strategy = StrategySettings().resolved()
    strategy.validate()
    trade = TradeSettings()
    trade.validate()
    return strategy, trade
