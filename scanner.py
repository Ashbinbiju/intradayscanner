#!/usr/bin/env python
"""
Intraday scanner -- a Streamlit view of which stocks actually triggered.

    streamlit run scanner.py

Pick a date and a symbol source, and it runs the same engine the backtester and
the live runner use, then shows every entry in the order it fired. The cap
controls answer the question that matters in practice: if you can only take two
or three trades, which ones would you actually have been in?

Symbols come from one of three places: the live intradayscreener.com API (any
of its 21 lists), a recorded snapshot of that API from a past day, or a list
you type. Candles come from the local cache when present, otherwise Angel One.

The live source reflects *now*, so pair it with today's date. For a past day
use a recorded snapshot -- running today's movers over last week's charts is
hindsight, and it flatters every result.
"""

from __future__ import annotations

import os
import pickle
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

# Secrets must reach the environment before config is imported, because config
# resolves credentials with os.getenv at import time. On Streamlit Cloud they
# come from .streamlit/secrets.toml; locally, from the shell.
for _key in ("CLIENT_ID", "PASSWORD", "TOTP_SECRET", "API_KEY",
             "UPSTOX_TOKEN", "DATA_SOURCE"):
    try:
        if _key in st.secrets and not os.getenv(_key):
            os.environ[_key] = str(st.secrets[_key])
    except Exception:
        pass  # no secrets file; fall back to the environment

import config
from orbfvg import backtest as bt
from orbfvg import livestate as LS
from orbfvg import screener
from orbfvg import universe as U
from orbfvg.strategy import Bar
from orbfvg.supertrend import SupertrendStrategy
from orbfvg.trendbreakout import TrendBreakoutStrategy

IST = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")

st.set_page_config(page_title="Intraday Scanner", page_icon="📈", layout="wide")

STRATEGIES = ["ORB + FVG", "Supertrend", "Trend Breakout"]

# Starting list selection per Direction. "Both" takes the mirror image of the
# bullish set, so short setups come from bearish lists, not only from stocks
# that happened to sit on a bullish one.
DEFAULT_LISTS = {
    "Bullish": ("stocksNearDaysHighAnd3DayHigh", "stocksNearDaysHighAndYestHigh",
                "high52Week", "volumeGainers"),
    "Bearish": ("stocksNearDaysHighAnd3DayLow", "stocksNearDaysLowAndYestLow",
                "low52Week", "topLoosers"),
}
DEFAULT_LISTS["Both"] = DEFAULT_LISTS["Bullish"] + DEFAULT_LISTS["Bearish"]


def is_supertrend(settings):
    return isinstance(settings, config.SupertrendSettings)


def is_trendbreakout(settings):
    return isinstance(settings, config.TrendBreakoutSettings)


def is_windowed(settings):
    """Strategies with a fixed entry window instead of an opening range."""
    return is_supertrend(settings) or is_trendbreakout(settings)


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_feed(prefer):
    """Candle source with a fallback behind it. Cached: logging in is slow."""
    from orbfvg.feed import MarketData

    return MarketData(prefer=prefer)


@st.cache_data(show_spinner=False)
def load_cache_file():
    """Whatever build_cache.py last wrote, if anything."""
    files = sorted(f for f in os.listdir(config.DATA_DIR) if f.startswith("candles_"))
    if not files:
        return {"candles": {}, "meta": {}, "selection": {}}
    with open(os.path.join(config.DATA_DIR, files[-1]), "rb") as fh:
        return pickle.load(fh)


@st.cache_data(show_spinner=False, ttl=900)
def own_shortlist(day, cutoff, top_n, min_rel_volume, min_move, pool_limit,
                  min_turnover):
    """Our own selection. Cached hard -- it scans hundreds of symbols."""
    stats, built_at = U.load_daily()
    if not stats:
        return [], {}, None
    pool = U.eligible(stats, U.Filters(min_turnover_cr=min_turnover))
    if pool_limit:
        pool = pool[:pool_limit]
    picks = U.shortlist(pool, day, cutoff=cutoff, top_n=top_n,
                        min_rel_volume=min_rel_volume, min_move_pct=min_move)
    facts = {c.symbol: {"priceChangePct": c.move_pct,
                        "changeInVolPct": (c.rel_volume - 1) * 100.0,
                        "scans": [c.direction, "relvol %.1fx" % c.rel_volume],
                        "buckets": ["own"]} for c in picks}
    return [c.symbol for c in picks], facts, built_at


@st.cache_data(show_spinner=False, ttl=120)
def screener_snapshot():
    """One live pull, cached briefly so slider fiddling does not re-fetch."""
    data = screener.fetch()
    try:
        screener.save_snapshot(data, config.DATA_DIR)
    except OSError:
        pass  # read-only filesystem on some hosts; not fatal
    return data, screener.details(data)


def bar_bucket(now=None):
    """Identifier for the current 5-minute slot.

    Passed into fetch_candles purely as part of its cache key, so a live
    refresh inside the same bar costs nothing and the first refresh after a
    bar closes pulls fresh candles.
    """
    now = now or datetime.now(IST)
    return "%s %02d:%02d" % (now.strftime("%Y-%m-%d"), now.hour, (now.minute // 5) * 5)


def market_state(now=None):
    """(state, human explanation) for the NSE cash session."""
    now = now or datetime.now(IST)
    clock = now.strftime("%H:%M")
    if now.weekday() >= 5:
        return "closed", "weekend"
    if clock < "09:15":
        return "pre", "opens 09:15"
    if clock > "15:30":
        return "closed", "closed at 15:30"
    return "open", "open until 15:30"


def session_clock(settings, now=None):
    """When this strategy's day actually happens, in exchange time.

    The NSE bell is not the thing to watch. With the shipped London preset the
    opening range forms 12:30-12:45 IST and entries run from 12:45 -- so a
    market-hours clock would report "open" for three hours during which the
    strategy cannot do anything.

    Derived from orSess/sigSess rather than hardcoded, by walking the day's
    5-minute slots through the same SessionWindow the engine uses. Switching
    --preset nse moves these automatically.
    """
    from orbfvg.pine import SessionWindow

    now = now or datetime.now(IST)
    if is_windowed(settings):
        def at(text):
            hh, mm = (int(x) for x in text.split(":"))
            return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return {"or_start": None, "or_end": None,
                "entry_from": at(settings.entryFrom), "entry_to": at(settings.entryTo),
                "sig_end": at(settings.entryTo),
                "square_off": at(settings.sqOffTime) if settings.sqOffTime else None}
    tz = ZoneInfo(settings.tzIn)
    or_win = SessionWindow.parse(settings.orSess)
    sig_win = SessionWindow.parse(settings.sigSess)

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    or_bars, sig_bars = [], []
    for i in range(24 * 12):                       # every 5-minute slot of the day
        slot = midnight + timedelta(minutes=5 * i)
        local = slot.astimezone(tz)
        if or_win.contains(local):
            or_bars.append(slot)
        if sig_win.contains(local):
            sig_bars.append(slot)

    out = {"or_start": or_bars[0] if or_bars else None,
           "or_end": (or_bars[-1] + timedelta(minutes=5)) if or_bars else None,
           "sig_end": (sig_bars[-1] + timedelta(minutes=5)) if sig_bars else None,
           "square_off": None}
    if getattr(settings, "sqOffTime", ""):
        try:
            hh, mm = (int(x) for x in settings.sqOffTime.split(":"))
            out["square_off"] = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            pass
    return out


def strategy_phase(settings, now=None):
    """(key, label, what happens next, seconds until it does)."""
    now = now or datetime.now(IST)
    state, why = market_state(now)
    if state == "closed":
        return "closed", "CLOSED", why, None

    clock = session_clock(settings, now)
    or_start, or_end = clock["or_start"], clock["or_end"]
    finish = clock["square_off"] or clock["sig_end"]

    def until(when):
        return max(0, int((when - now).total_seconds())) if when else None

    if is_windowed(settings):
        first, last = clock["entry_from"], clock["entry_to"]
        if now < first:
            return ("waiting", "WAITING", "entries from %s" % first.strftime("%H:%M"),
                    until(first))
        if now < last:
            return ("armed", "ENTRIES LIVE", "new entries until %s" % last.strftime("%H:%M"),
                    until(last))
        if finish and now < finish:
            return ("managing", "MANAGING", "squares off %s" % finish.strftime("%H:%M"),
                    until(finish))
        return ("done", "DONE",
                "squared off %s" % (finish.strftime("%H:%M") if finish else ""), None)

    if or_start and now < or_start:
        return ("waiting", "WAITING",
                "range forms %s" % or_start.strftime("%H:%M"), until(or_start))
    if or_start and or_end and now < or_end:
        return ("range", "RANGE FORMING",
                "locks %s" % or_end.strftime("%H:%M"), until(or_end))
    if finish and now < finish:
        return ("armed", "ENTRIES LIVE",
                "squares off %s" % finish.strftime("%H:%M"), until(finish))
    return ("done", "DONE",
            "squared off %s" % (finish.strftime("%H:%M") if finish else ""), None)


def seconds_to_next_bar(now=None):
    now = now or datetime.now(IST)
    nxt = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    nxt = nxt.replace(minute=(nxt.minute // 5) * 5)
    return max(0, int((nxt - now).total_seconds()))


def fetch_candles(symbol, exchange, day_from, day_to, bucket="", prefer="upstox"):
    """Cache first, then the feed. Returns (rows, meta) or (None, None).

    Live mode used to re-pull the whole 60-day warm-up for every symbol at
    every 5-minute close -- minutes of requests per refresh at 90 symbols,
    which is what made a sidebar change or a reconnect hang. History up to
    yesterday cannot change during the day, so it is cached for hours and
    only today's bars are re-pulled per `bucket`.
    """
    if not bucket or day_from >= day_to:
        return _fetch_range(symbol, exchange, day_from, day_to, bucket, prefer)
    yesterday = (datetime.strptime(day_to, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    hist_rows, hist_meta = _fetch_history(symbol, exchange, day_from, yesterday, prefer)
    today_rows, today_meta = _fetch_range(symbol, exchange, day_to, day_to, bucket, prefer)
    if not today_rows:
        # Nothing for today yet (or the split pull failed): fall back to one pull.
        return _fetch_range(symbol, exchange, day_from, day_to, bucket, prefer)
    rows = [r for r in (hist_rows or []) if r[0][:10] < day_to] + list(today_rows)
    return rows, (today_meta or hist_meta)


@st.cache_data(show_spinner=False, ttl=6 * 3600)
def _fetch_history(symbol, exchange, day_from, day_to, prefer="upstox"):
    """Completed sessions only -- safe to keep for hours."""
    return _fetch_range(symbol, exchange, day_from, day_to, "history", prefer)


@st.cache_data(show_spinner=False, ttl=900)
def _fetch_range(symbol, exchange, day_from, day_to, bucket="", prefer="upstox"):
    """`bucket` is unused in the body -- it exists so a new 5-minute slot is a
    cache miss and forces a re-fetch."""
    store = load_cache_file()
    if symbol in store["candles"]:
        rows = store["candles"][symbol]
        meta = store["meta"].get(symbol, {"tick": 0.05})
        keep = [r for r in rows if day_from <= r[0][:10] <= day_to]
        if keep:
            return keep, meta
    try:
        feed = get_feed(prefer)
        inst = feed.instrument(symbol, exchange)
        bars = feed.candles(
            exchange, symbol, "FIVE_MINUTE",
            datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=IST),
            datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1),
            tz=IST,
        )
    except Exception as exc:
        st.session_state.setdefault("errors", []).append("%s: %s" % (symbol, exc))
        return None, None
    if not bars:
        return None, None
    rows = [(b.time.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars]
    return rows, {"tick": inst.tick_size, "lot": inst.lotsize, "token": inst.token}


def to_bars(rows):
    return [
        Bar(time=datetime.fromisoformat(t), open=o, high=h, low=l, close=c, volume=v)
        for t, o, h, l, c, v in rows
    ]


def resolve_universe(spec):
    """Turn a universe *description* into symbols, right now.

    The sidebar records what to select rather than the selection itself, so
    the live fragment can re-resolve it on every tick. A Streamlit fragment
    closes over values from the last full script run -- had it captured the
    symbol list, the app would have kept scanning whatever the screener
    happened to return when the page loaded, even as the lists turned over
    completely at the open.
    """
    source = spec.get("source")
    if source == "typed":
        return list(spec.get("symbols") or []), {}
    if source == "own":
        symbols, facts, _ = own_shortlist(
            spec["day"], spec["cutoff"], spec["top_n"], spec["min_rel_volume"],
            spec["min_move"], spec["pool_limit"], spec["min_turnover"])
        return symbols, facts
    if source == "snapshot":
        found = screener.symbols_from_history(
            config.DATA_DIR, spec["day"], spec["buckets"],
            spec.get("start_t", "00:00"), spec.get("end_t", "23:59"))
        return found, {}
    try:
        data, details = screener_snapshot()
    except screener.ScreenerError:
        return [], {}
    found = screener.symbols(data, spec.get("buckets") or [])
    floor = spec.get("min_chg") or 0
    if floor > 0:
        found = [s for s in found
                 if abs((details.get(s) or {}).get("priceChangePct") or 0) >= floor]
    return found, details


# ---------------------------------------------------------------------------
#  Engine
# ---------------------------------------------------------------------------
def build_settings(ui):
    """UI overrides on top of the shipped config.

    Reads defensively: a `ui` dict restored from an older session may be
    missing keys this build expects, and falling back to the configured
    default is better than a crash.
    """
    ui = ui or {}
    if ui.get("strategy") == "Trend Breakout":
        tb_settings = config.TrendBreakoutSettings()
        for key in ("tfMinutes", "slAtr", "tpR", "useRe", "useLate",
                    "entryFrom", "entryTo", "sqOffTime"):
            if ui.get("tb_" + key) is not None:
                setattr(tb_settings, key, ui["tb_" + key])
        tb_settings.validate()
        return tb_settings
    if ui.get("strategy") == "Supertrend":
        st_settings = config.SupertrendSettings()
        for key in ("tfMinutes", "factor", "atrPeriod", "slAtrMult", "exitMode",
                    "exitOnFlip", "entryFrom", "entryTo", "sqOffTime"):
            if ui.get("st_" + key) is not None:
                setattr(st_settings, key, ui["st_" + key])
        st_settings.validate()
        return st_settings
    s, _ = config.load()
    for ui_key, attr in (("atrMult", "atrMult"), ("strongClose", "strongClose"),
                         ("useFvg", "useFvg"), ("fvgAtrX", "fvgAtrX"),
                         ("bufPct", "bufPct"), ("useTrail", "useTrail"),
                         ("trailMode", "trailMode"), ("sqOff", "sqOffTime"),
                         ("minRangePct", "minRangePct")):
        if ui.get(ui_key) is not None:
            setattr(s, attr, ui[ui_key])
    s.validate()
    return s


def scan(symbols, target_day, ui, exchange="NSE", bucket="", quiet=False,
         prefer="upstox"):
    """Run the engine over each symbol and collect that day's trades."""
    settings = build_settings(ui)
    # The trend breakout's 1H EMA 200 needs ~29 sessions of history to read a
    # trend at all; the other engines are warm well inside two weeks.
    warm_days = 60 if is_trendbreakout(settings) else 14
    warm_from = (datetime.strptime(target_day, "%Y-%m-%d") - timedelta(days=warm_days)).strftime("%Y-%m-%d")

    trades, context, missing = [], {}, []
    progress = None if quiet else st.progress(0.0, text="Scanning...")
    for n, symbol in enumerate(symbols, 1):
        if progress is not None:
            progress.progress(n / max(len(symbols), 1),
                              text="Scanning %s (%d/%d)" % (symbol, n, len(symbols)))
        rows, meta = fetch_candles(symbol, exchange, warm_from, target_day, bucket, prefer)
        if not rows:
            missing.append(symbol)
            continue
        bars = to_bars(rows)
        settings.mintick = meta.get("tick", 0.05)
        supertrend = is_supertrend(settings)
        trendbreakout = is_trendbreakout(settings)
        if supertrend:
            result = bt.run(bars, settings, engine=SupertrendStrategy)
        elif trendbreakout:
            result = bt.run(bars, settings, engine=TrendBreakoutStrategy)
        else:
            result = bt.run(bars, settings)

        day_bars = [b for b in bars if b.time.strftime("%Y-%m-%d") == target_day]
        if not day_bars:
            missing.append(symbol)
            continue

        orb = [] if (supertrend or trendbreakout) else [b for b in day_bars if in_or_window(b, settings)]
        ctx = {
            "bars": day_bars,
            "orb_high": max((b.high for b in orb), default=float("nan")),
            "orb_low": min((b.low for b in orb), default=float("nan")),
            "day_pct": (day_bars[-1].close / day_bars[0].open - 1) * 100,
            "events": [e for e in result.events if e.time.strftime("%Y-%m-%d") == target_day],
        }
        if supertrend:
            line = [(end, value, direction) for end, value, direction in result.engine.history
                    if end.strftime("%Y-%m-%d") == target_day]
            ctx["st_line"] = line
            ctx["trend"] = ("UP" if line[-1][2] < 0 else "DOWN") if line else "-"
            ctx["st_last"] = line[-1][1] if line else float("nan")
        if trendbreakout:
            snap = result.engine.snapshot()
            ctx["tb"] = True
            ctx["trend"] = snap["trend"]
            ctx["ema_fast"], ctx["ema_slow"] = snap["ema_fast"], snap["ema_slow"]
            ctx["next_buy"], ctx["next_sell"] = snap["next_buy"], snap["next_sell"]
            ctx["waiting"] = snap["waiting_pullback"]
        context[symbol] = ctx
        for t in result.trades:
            if t.entry_time.strftime("%Y-%m-%d") == target_day:
                t.symbol = symbol
                # Trend Breakout fills (and squares off) AT a bar's open.
                t.at_open = trendbreakout
                trades.append(t)
    if progress is not None:
        progress.empty()
    # The engine squares off on the first bar at/after the cut-off. If the
    # feed had not delivered that bar by the last refresh, the trade would
    # stay "open" for good (the page stops refreshing once the day is done).
    if is_trendbreakout(settings) and getattr(settings, "sqOffTime", ""):
        LS.settle_open_trades(trades, {s: c["bars"] for s, c in context.items()},
                              settings.sqOffTime, IST, datetime.now(IST))
    trades.sort(key=lambda t: (t.entry_time, t.symbol))
    return trades, context, missing


def in_or_window(bar, settings):
    from orbfvg.pine import SessionWindow

    return SessionWindow.parse(settings.orSess).contains(
        bar.time.astimezone(ZoneInfo(settings.tzIn))
    )


def apply_caps(trades, max_per_day, max_concurrent):
    """First come, first served, exactly as portfolio.py does it."""
    open_until, taken, skipped = [], [], []
    for t in trades:
        open_until = [x for x in open_until if x > t.entry_time]
        if len(taken) >= max_per_day:
            skipped.append((t, "daily cap"))
        elif len(open_until) >= max_concurrent:
            skipped.append((t, "slots busy"))
        else:
            taken.append(t)
            # A trade still open holds its slot until it closes, not just
            # for the instant it was entered.
            open_until.append(t.exit_time or t.entry_time + timedelta(days=1))
    return taken, skipped


def pct_of(t):
    return (t.exit - t.entry) * (1 if t.side == "BUY" else -1) / t.entry * 100


FEED_BAR = timedelta(minutes=5)


def filled_at(moment):
    """When an event actually happened: the close of the 5-minute bar it fired on.

    Events are stamped with the bar's OPEN, as Pine does, so an entry taken at
    the 09:30 close is stamped 09:25. Showing that to a trader reads as an
    entry before 09:30, so every table shows the bar's close instead.

    Not for Trend Breakout fills and square-offs -- those happen *at* a bar's
    open; use entry_at / exit_at, which know the difference.
    """
    return (moment + FEED_BAR).strftime("%H:%M")


def entry_at(t):
    return LS.entry_display(t, getattr(t, "at_open", False))


def exit_at(t):
    return LS.exit_display(t, getattr(t, "at_open", False))


def event_at(e, at_open):
    """Trend Breakout entries and square-offs are stamped at the moment they fill."""
    opens = at_open and (e.type.name == "ENTRY" or
                         (e.type.name == "SESSION_EXIT" and e.reason == "SESSION_END"))
    return LS.display_time(e.time, opens)


def _fact(details, symbol, field):
    """A screener field for this symbol, when the universe came from the API."""
    value = (details.get(symbol) or {}).get(field)
    return round(value, 2) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------------
#  Chart
# ---------------------------------------------------------------------------
def candle_chart(symbol, ctx, trade, settings):
    bars = ctx["bars"]
    df = pd.DataFrame([{
        "time": b.time.replace(tzinfo=None), "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])
    df["up"] = df["close"] >= df["open"]

    base = alt.Chart(df).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M")),
        color=alt.condition("datum.up", alt.value("#089981"), alt.value("#f23645")),
        tooltip=["time:T", "open:Q", "high:Q", "low:Q", "close:Q", "volume:Q"],
    )
    wick = base.mark_rule().encode(y=alt.Y("low:Q", title=None,
                                           scale=alt.Scale(zero=False)), y2="high:Q")
    body = base.mark_bar(size=5).encode(y="open:Q", y2="close:Q")
    layers = [wick, body]

    def hline(value, color, label, dash=None):
        if value != value:
            return None
        d = pd.DataFrame({"y": [value], "label": [label]})
        rule = alt.Chart(d).mark_rule(
            color=color, strokeDash=dash or [], size=1.5,
        ).encode(y="y:Q")
        text = alt.Chart(d).mark_text(
            align="left", dx=4, dy=-6, color=color, fontSize=10,
        ).encode(y="y:Q", text="label:N")
        return rule + text

    for value, color, label, dash in [
        (ctx["orb_high"], "#2962ff", "range high", None),
        (ctx["orb_low"], "#2962ff", "range low", None),
    ]:
        layer = hline(value, color, label, dash)
        if layer is not None:
            layers.append(layer)

    if ctx.get("st_line"):
        # Plotted at each signal candle's open, as TradingView draws it.
        tf = timedelta(minutes=getattr(settings, "tfMinutes", 15))
        line = pd.DataFrame([{"time": (end - tf).replace(tzinfo=None), "st": value,
                              "trend": "up" if direction < 0 else "down"}
                             for end, value, direction in ctx["st_line"]])
        layers.append(alt.Chart(line).mark_point(size=14, filled=True).encode(
            x="time:T", y="st:Q",
            color=alt.Color("trend:N", legend=None,
                            scale=alt.Scale(domain=["up", "down"],
                                            range=["#089981", "#f23645"])),
            tooltip=["time:T", "st:Q", "trend:N"]))

    if trade is not None:
        up = trade.side == "BUY"
        for value, color, label in [
            (trade.entry, "#787b86", "entry %.2f" % trade.entry),
            (trade.stop, "#f23645", "SL %.2f" % trade.stop),
            (trade.t1, "#089981" if up else "#f23645", "T1"),
            (trade.t2, "#089981" if up else "#f23645", "T2"),
            (trade.t3, "#089981" if up else "#f23645", "T3"),
        ]:
            layer = hline(value, color, label, [4, 4])
            if layer is not None:
                layers.append(layer)

        marks = pd.DataFrame([
            {"time": trade.entry_time.replace(tzinfo=None), "y": trade.entry,
             "m": "▲ " + trade.side if up else "▼ " + trade.side},
            {"time": (trade.exit_time or trade.entry_time).replace(tzinfo=None),
             "y": trade.exit, "m": "✕ " + (trade.reason or "")},
        ])
        layers.append(
            alt.Chart(marks).mark_text(fontSize=13, dy=-12, color="#111").encode(
                x="time:T", y="y:Q", text="m:N")
        )

    return alt.layer(*layers).properties(height=430).interactive()


# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------
st.title("Intraday Scanner")


# The main choices are mirrored into the page URL. A Streamlit reconnect starts
# a brand-new session with every widget back on its default -- mid-session that
# silently switched Live back to "Review a day" and the strategy back to
# ORB + FVG. The URL survives the reconnect (and can be bookmarked).
def remembered(key, options, default=0):
    value = st.query_params.get(key)
    return options.index(value) if value in options else default


def remember(key, value):
    if st.query_params.get(key) != value:
        st.query_params[key] = value


with st.sidebar:
    st.header("Mode")
    _modes = ["Review a day", "Live"]
    mode = st.radio(
        "Mode", _modes, index=remembered("mode", _modes), label_visibility="collapsed",
        help="Review replays a chosen day once, on demand. Live re-scans by "
             "itself at every 5-minute close during market hours.")
    remember("mode", mode)
    live_mode = mode == "Live"
    if live_mode:
        refresh_secs = st.select_slider(
            "Refresh every", [30, 60, 120, 300], value=60,
            format_func=lambda v: "%ds" % v if v < 60 else "%dm" % (v // 60))
        st.caption("Candles are only re-pulled when a 5-minute bar closes, so "
                   "a faster refresh costs no extra API calls.")
        lock_universe = st.checkbox(
            "Lock the watchlist once the range locks", value=True,
            help="The screener's lists turn over all day. Re-reading them "
                 "keeps the watchlist current, but letting it grow through the "
                 "afternoon inflates the scan and drifts from how the strategy "
                 "was tested. Locking freezes the set at the moment entries "
                 "become possible.")
    else:
        refresh_secs = 60
        lock_universe = True

    st.header("Universe")
    _sources = ["Our screener", "Screener (live)", "Recorded snapshot", "Type a list"]
    # With no universe built, "Our screener" can only show a warning; start
    # on the live screener instead.
    _default_source = 0 if U.load_daily()[0] else 1
    source = st.radio(
        "Symbols from", _sources, index=remembered("source", _sources, _default_source),
        help="Our screener needs no third-party API -- it ranks the NSE cash "
             "list from candles we already pull.")
    remember("source", source)

    symbols, details = [], {}
    spec = {"source": "typed", "symbols": []}

    if source == "Our screener":
        stats, built_at = U.load_daily()
        if not stats:
            st.warning("No universe built yet. Run `python build_universe.py "
                       "daily` — about two minutes for the whole NSE cash list.")
        else:
            st.caption("Universe built %s — %d symbols, %d eligible."
                       % ((built_at or "?")[:10], len(stats), len(U.eligible(stats))))
            own_day = st.text_input("Day", datetime.now(IST).strftime("%Y-%m-%d"),
                                    key="own_day")
            c1, c2 = st.columns(2)
            own_cutoff = c1.text_input("Rank as of", "12:25", key="own_cut")
            own_top = c2.number_input("Top N", 5, 100, 20, key="own_top")
            own_turnover = st.slider("Min turnover (cr/day)", 1.0, 100.0, 5.0, 1.0)
            own_relvol = st.slider("Min relative volume", 0.5, 5.0, 1.0, 0.1)
            own_move = st.slider("Min |move| % by cut-off", 0.0, 10.0, 1.0, 0.5)
            own_pool = st.number_input("Scan at most", 50, 2000, 400, step=50,
                                       help="Most liquid first. Each symbol is "
                                            "one request, so this sets the wait.")
            spec = {"source": "own", "day": own_day, "cutoff": own_cutoff,
                    "top_n": int(own_top), "min_rel_volume": own_relvol,
                    "min_move": own_move, "pool_limit": int(own_pool),
                    "min_turnover": own_turnover}
            if st.button("Build shortlist", use_container_width=True):
                own_shortlist.clear()
            with st.spinner("Ranking..."):
                symbols, details = resolve_universe(spec)
            st.caption("%d candidates, ranked using only bars before %s."
                       % (len(symbols), own_cutoff))

    elif source == "Screener (live)":
        try:
            data, details = screener_snapshot()
        except screener.ScreenerError as exc:
            st.error(str(exc))
            st.info("The screener API is unreachable. Switch the source to "
                    "**Type a list** to carry on without it.")
            data = {}
        if data:
            _sides = ["Bullish", "Bearish", "Both"]
            side = st.radio("Direction", _sides, index=remembered("side", _sides),
                            horizontal=True)
            remember("side", side)
            pool = (screener.BULLISH if side == "Bullish" else
                    screener.BEARISH if side == "Bearish" else
                    screener.BULLISH + screener.BEARISH)
            available_buckets = [b for b in pool if screener.symbols_in(data, b)]
            default = [b for b in DEFAULT_LISTS[side] if b in available_buckets] \
                or available_buckets[:2]
            # Keyed per direction: switching Bullish -> Both now picks up the
            # bearish defaults instead of keeping the bullish-only selection.
            chosen = st.multiselect(
                "Lists", available_buckets, default=default, key="lists_" + side,
                format_func=lambda b: "%s (%d)" % (
                    screener.LABELS.get(b, b), len(screener.symbols_in(data, b))))
            min_chg = st.slider("Min |price change| %", 0.0, 15.0, 0.0, 0.5)
            spec = {"source": "live", "buckets": chosen, "min_chg": min_chg}
            symbols, details = resolve_universe(spec)
            st.caption("%d symbols on the lists right now" % len(symbols))
            if live_mode:
                _lock = LS.UniverseLock(config.DATA_DIR,
                                        datetime.now(IST).strftime("%Y-%m-%d"), spec)
                _saved = _lock.load()
                if _saved:
                    st.caption("Today's watchlist is **locked at %d symbols** (since %s). "
                               "The lists keep changing, but the scan stays on the "
                               "locked set until tomorrow. Changing Direction or "
                               "Lists starts a new watchlist from the current lists."
                               % (len(_saved["symbols"]), _saved.get("locked_at", "")[11:16]))
            st.caption("Live snapshot — it reflects **now**, so use it with "
                       "today's date. Each pull is saved for later replay.")

    elif source == "Recorded snapshot":
        days = screener.list_snapshot_days(config.DATA_DIR)
        if not days:
            st.warning("No snapshots recorded yet. Run `python record_snapshot.py` "
                       "on a schedule during market hours to build the history.")
        else:
            snap_day = st.selectbox("Recorded day", days[::-1], index=0)
            c1, c2 = st.columns(2)
            start_t = c1.text_input("Listed from", "12:00")
            end_t = c2.text_input("to", "12:45")
            st.caption("The window the stock must have appeared in — the "
                       "run-up to the opening range, not the whole day.")
            side = st.radio("Direction", ["Bullish", "Bearish", "Both"],
                            index=0, horizontal=True)
            pool = (screener.BULLISH if side == "Bullish" else
                    screener.BEARISH if side == "Bearish" else
                    screener.BULLISH + screener.BEARISH)
            chosen = st.multiselect(
                "Lists", pool,
                default=[b for b in ("stocksNearDaysHighAnd3DayHigh", "high52Week")
                         if b in pool],
                format_func=lambda b: screener.LABELS.get(b, b))
            spec = {"source": "snapshot", "day": snap_day, "buckets": chosen,
                    "start_t": start_t, "end_t": end_t}
            symbols, details = resolve_universe(spec)
            st.caption("%d symbols recorded in that window" % len(symbols))

    else:
        typed = st.text_area(
            "One per line or comma separated",
            "NEWGEN\nATHERENERG\nTEJASNET\nCOFORGE\nSHYAMMETL\nCGCL")
        symbols = [s.strip().upper() for s in typed.replace(",", "\n").split("\n") if s.strip()]
        # Live mode re-resolves the universe from `spec` on every tick; without
        # this it resolved to the empty default and scanned nothing.
        spec = {"source": "typed", "symbols": symbols}

    st.header("Date")
    if live_mode:
        day = datetime.now(IST).strftime("%Y-%m-%d")
        st.caption("Live tracks today: %s" % day)
    elif source == "Recorded snapshot" and symbols:
        day = snap_day
        st.caption("Locked to the snapshot day: %s" % day)
    else:
        default_day = datetime.now(IST).date()
        day = st.date_input("Trading day", default_day).strftime("%Y-%m-%d")

    st.header("Strategy")
    ui = {}
    ui["strategy"] = st.selectbox(
        "Strategy", STRATEGIES, index=remembered("strategy", STRATEGIES),
        help="ORB + FVG: opening-range breakout confirmed by a fair-value gap. "
             "Supertrend: the 'Supertrend with Entry, TP1, TP2 & TP3' "
             "indicator's flip signal, traded intraday. "
             "Trend Breakout: 1H EMA 50/200 trend + close beyond the prior "
             "40 candles (the NSE Trend Breakout v1.1 indicator).")
    remember("strategy", ui["strategy"])

    if ui["strategy"] == "Trend Breakout":
        ui["tb_tfMinutes"] = st.select_slider(
            "Signal timeframe", [5, 15], value=15,
            format_func=lambda v: "%dm" % v,
            help="Candles built from the 5-minute feed, aligned to 09:15. "
                 "The trend is always the previous completed 1H candle")
        ui["tb_slAtr"] = st.slider("Stop  × ATR", 0.5, 4.0, 1.5, 0.1,
                                   help="Indicator default 1.5, from the fill")
        ui["tb_tpR"] = st.slider("Target (R)", 1.0, 5.0, 3.0, 0.5)
        ui["tb_useLate"] = st.checkbox(
            "Late-entry guard", True,
            help="A breakout more than 4 ATR from the 21-candle swing waits for a "
                 "0.5 ATR pullback, then enters on the retest")
        ui["tb_useRe"] = st.checkbox("Re-enter once after a stop-out", True)
        c1, c2 = st.columns(2)
        ui["tb_entryFrom"] = c1.text_input("Fills from", "09:30",
                                           help="Fill time = next candle's open, IST")
        ui["tb_entryTo"] = c2.text_input("until", "13:00",
                                         help="v1.1 default. v1.0 used 14:30")
        ui["tb_sqOffTime"] = st.text_input(
            "Square off at (IST)", "15:00",
            help="Exit at the open of the first candle starting at or after this")
        st.caption("Needs ~60 days of history per symbol for the 1H EMA 200, so "
                   "the first scan is slower. Measured on 37 NSE stocks it is "
                   "about break-even before costs and negative after them.")
    elif ui["strategy"] == "Supertrend":
        ui["st_tfMinutes"] = st.select_slider(
            "Signal timeframe", [5, 15, 30, 60], value=15,
            format_func=lambda v: "%dm" % v,
            help="Candles built from the 5-minute feed, aligned to 09:15")
        c1, c2 = st.columns(2)
        ui["st_factor"] = c1.number_input("Factor", 0.5, 10.0, 2.5, 0.1)
        ui["st_atrPeriod"] = int(c2.number_input("ATR period", 1, 100, 14))
        ui["st_slAtrMult"] = st.slider("Stop  × ATR", 0.5, 4.0, 1.5, 0.1,
                                       help="Indicator default 1.5. Targets sit at "
                                            "1R / 2R / 3R")
        ui["st_exitMode"] = st.selectbox(
            "Exit", list(config.SupertrendSettings.EXIT_MODES),
            help="T3 / T1: fixed stop, close at that target. Step trail: stop to "
                 "entry at T1, to T1 at T2, to T2 at T3. ST trail: stop follows "
                 "the Supertrend line.")
        ui["st_exitOnFlip"] = st.checkbox("Opposite signal closes the trade", True)
        c1, c2 = st.columns(2)
        ui["st_entryFrom"] = c1.text_input("Entries from", "09:30",
                                           help="Signal candle close time, IST")
        ui["st_entryTo"] = c2.text_input("until", "12:50")
        ui["st_sqOffTime"] = st.text_input(
            "Square off at (IST)", "15:00",
            help="Exits at the close of the 5-minute bar opening at this time")
        st.caption("Measured on 24 stocks over 2022-2026 (15m, these defaults): "
                   "negative with every exit method, even before costs.")
    else:
        ui["atrMult"] = st.slider("Stop  × ATR", 1.0, 4.0, 3.0, 0.1,
                                  help="Pine ships 1.5; testing favoured 3.0")
        ui["strongClose"] = st.slider("Strong close", 0.0, 1.0, 0.5, 0.05,
                                      help="0 = off. Breakout bar must close in this "
                                           "fraction of its own range")
        ui["bufPct"] = st.slider("Breakout buffer  % of range", 0.0, 25.0, 5.0, 1.0)
        ui["useFvg"] = st.checkbox("Require FVG", True)
        ui["fvgAtrX"] = st.slider("Min FVG  × ATR", 0.0, 1.0, 0.15, 0.05,
                                  disabled=not ui["useFvg"])
        ui["minRangePct"] = st.slider("Min range  % of price", 0.0, 2.0, 0.0, 0.1,
                                      help="0 = off. Skips thin, choppy ranges")
        ui["useTrail"] = st.checkbox("Trailing stop", False)
        ui["trailMode"] = st.selectbox("Trail mode", ["ATR", "Targets", "Both"],
                                       disabled=not ui["useTrail"])
        ui["sqOff"] = st.text_input("Square off at (IST)", "15:15",
                                    help="Blank disables — matches TradingView")

    st.header("Data")
    prefer = st.radio(
        "Candles from", ["Upstox", "Angel One"], index=0, horizontal=True,
        help="Whichever is picked, the other stands in when it fails. They "
             "agree on price; Angel's feed stops at 15:10 for many symbols "
             "while Upstox carries the 15:15-15:25 bars.").lower()
    prefer = "upstox" if prefer.startswith("upstox") else "angel"

    st.header("Position limits")
    max_per_day = st.number_input("Max trades per day", 1, 20, 3)
    max_concurrent = st.number_input("Max open at once", 1, 20, 3)

    run = st.button("Scan now" if live_mode else "Scan",
                    type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
#  Live panels
# ---------------------------------------------------------------------------
def open_positions(trades, context):
    """Trades with no exit yet, marked to the latest bar close.

    Pass only the trades you want listed -- the live view passes the taken
    ones here and shows cap-skipped signals separately, so "Open positions"
    means positions you could actually be holding.
    """
    rows = []
    for t in trades:
        if t.exit == t.exit:          # already closed
            continue
        bars = (context.get(t.symbol) or {}).get("bars") or []
        if not bars:
            continue
        last = bars[-1].close
        direction = 1 if t.side == "BUY" else -1
        r_now = LS.open_r(t, last)
        reached = LS.reached_targets(t, bars)
        rows.append({
            "Symbol": t.symbol,
            "Side": t.side,
            "In at": entry_at(t),
            "Entry": round(t.entry, 2),
            "Now": round(last, 2),
            "Open R": round(r_now, 2) if r_now is not None else None,
            "%": round((last - t.entry) * direction / t.entry * 100, 2),
            "Stop": round(t.stop, 2),
            "To stop %": round(abs(last - t.stop) / last * 100, 2),
            "T1": round(t.t1, 2),
            "T2": round(t.t2, 2),
            "T3": round(t.t3, 2),
            "Next target": ("T1" if reached < 1 else "T2" if reached < 2 else
                            "T3" if reached < 3 else "—"),
        })
    return rows


def open_r_total(trades, context):
    """Sum of open R across still-open trades, marked to the last close."""
    total = 0.0
    for t in trades:
        if t.exit == t.exit:
            continue
        bars = (context.get(t.symbol) or {}).get("bars") or []
        if bars:
            r = LS.open_r(t, bars[-1].close)
            total += r or 0.0
    return total


def _hms(seconds):
    if seconds is None:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%dh %02dm" % (h, m) if h else "%dm %02ds" % (m, s)


def live_header(day, symbols, trades, context, settings, taken=None):
    now = datetime.now(IST)
    phase, label, nxt, secs = strategy_phase(settings, now)
    icon = {"waiting": "🟡", "range": "🟠", "armed": "🟢", "managing": "🔵",
            "done": "⚪", "closed": "🔴"}[phase]
    # "Open now" counts positions you could hold: taken trades, not every
    # signal the cap skipped.
    live_trades = [t for t in (trades if taken is None else taken) if t.exit != t.exit]

    c = st.columns(5)
    c[0].metric("Strategy", "%s %s" % (icon, label), nxt)
    ticking = phase in ("range", "armed", "managing")
    c[1].metric("Next bar" if ticking else "Countdown",
                _hms(seconds_to_next_bar(now)) if ticking else _hms(secs))
    c[2].metric("Watching", len(symbols))
    c[3].metric("Signals today", len(trades))
    c[4].metric("Open now", len(live_trades))

    clock = session_clock(settings, now)
    if is_trendbreakout(settings):
        plan = ("Trend Breakout %dm · 1H EMA %d/%d · fills %s–%s · stop %g ATR · target %gR · square-off %s"
                % (settings.tfMinutes, settings.fastLen, settings.slowLen,
                   settings.entryFrom, settings.entryTo, settings.slAtr, settings.tpR,
                   settings.sqOffTime))
        st.caption("%s  ·  refreshed %s IST" % (plan, now.strftime("%H:%M:%S")))
        return phase
    if is_supertrend(settings):
        plan = ("Supertrend %dm (%g, %d) · entries %s–%s · square-off %s · exit %s"
                % (settings.tfMinutes, settings.factor, settings.atrPeriod,
                   settings.entryFrom, settings.entryTo, settings.sqOffTime or "—",
                   settings.exitMode))
        st.caption("%s  ·  refreshed %s IST" % (plan, now.strftime("%H:%M:%S")))
        return phase
    plan = "Range %s–%s · entries from %s · square-off %s (%s in %s)" % (
        clock["or_start"].strftime("%H:%M") if clock["or_start"] else "—",
        clock["or_end"].strftime("%H:%M") if clock["or_end"] else "—",
        clock["or_end"].strftime("%H:%M") if clock["or_end"] else "—",
        (clock["square_off"] or clock["sig_end"]).strftime("%H:%M")
        if (clock["square_off"] or clock["sig_end"]) else "—",
        settings.orSess, settings.tzIn)
    st.caption("%s  ·  refreshed %s IST" % (plan, now.strftime("%H:%M:%S")))
    return phase


def lock_state(lock):
    """Today's locked watchlist: this session's copy, else the one on disk."""
    mine = st.session_state.get("locked_universe")
    if (isinstance(mine, dict) and mine.get("day") == lock.day
            and st.session_state.get("locked_key") == lock.key):
        return mine
    saved = lock.load()
    st.session_state["locked_universe"] = saved
    st.session_state["locked_key"] = lock.key
    return saved


def render_live(spec, day, ui, max_per_day, max_concurrent, lock_after_range=True,
                prefer="upstox"):
    """One live pass: re-resolve the universe, rescan, draw everything."""
    settings = build_settings(ui)
    phase_key = strategy_phase(settings)[0]

    # Re-resolve every tick so the watchlist tracks the screener as it turns
    # over through the session -- the whole point of live mode.
    symbols, details = resolve_universe(spec)

    # Once the range has locked, freeze the set. Symbols joining a momentum
    # list at 14:00 still have a valid 12:30 range, but letting the universe
    # grow all afternoon inflates the scan (one candle request per symbol per
    # bar, at 3/sec) and drifts away from how the strategy was tested.
    #
    # The lock is saved to disk per day and universe definition, not only in
    # the browser session: a reconnect used to rebuild it from whatever the
    # screener listed at that moment and replay the whole morning on a
    # different set of stocks.
    lock = LS.UniverseLock(config.DATA_DIR, day, spec)
    locked = lock_state(lock)
    if lock_after_range and phase_key in ("armed", "managing", "done", "closed"):
        if locked:
            symbols = locked["symbols"]
            # Screener facts as of the lock, so a symbol that has since left
            # every list still shows its Day % / Vol %.
            details = {**(locked.get("details") or {}), **(details or {})}
        elif phase_key != "closed" and symbols:
            locked = lock.save(symbols, details, datetime.now(IST))
            st.session_state["locked_universe"] = locked
    elif phase_key in ("waiting", "range"):
        st.session_state.pop("locked_universe", None)
        lock.clear()

    previous = st.session_state.get("last_universe")
    st.session_state["last_universe"] = list(symbols)

    # Show a progress bar when the symbol set is new to this session (first
    # load, a reconnect, a sidebar change): that pull is the slow one, and a
    # blank page for minutes reads as a hang. Routine ticks stay quiet.
    cold = previous is None or set(previous) != set(symbols)
    bucket = bar_bucket() if phase_key not in ("done", "closed") else day + " final"
    trades, context, missing = scan(symbols, day, ui, bucket=bucket, quiet=not cold,
                                    prefer=prefer)
    taken, _skipped = apply_caps(trades, max_per_day, max_concurrent)
    phase = live_header(day, symbols, trades, context, settings, taken=taken)

    if previous is not None and set(previous) != set(symbols):
        added = sorted(set(symbols) - set(previous))
        gone = sorted(set(previous) - set(symbols))
        bits = []
        if added:
            bits.append("+%d (%s)" % (len(added), ", ".join(added[:5])))
        if gone:
            bits.append("-%d (%s)" % (len(gone), ", ".join(gone[:5])))
        st.caption("Watchlist changed: %s" % "  ".join(bits))
    if lock_after_range and st.session_state.get("locked_universe"):
        st.caption("Universe locked at %d symbols for the rest of the session."
                   % len(symbols))

    # New signals since the previous refresh, so nothing is missed while away.
    seen = st.session_state.setdefault("seen_signals", set())
    keys = {"%s|%s" % (t.symbol, t.entry_time.isoformat()) for t in trades}
    fresh = keys - seen
    if fresh and seen:
        for t in trades:
            if "%s|%s" % (t.symbol, t.entry_time.isoformat()) in fresh:
                st.toast("%s %s @ %.2f" % (t.side, t.symbol, t.entry), icon="🔔")
    st.session_state["seen_signals"] = keys

    open_taken, open_skipped = LS.split_open(trades, taken)
    live_rows = open_positions(open_taken, context)
    pos_cols = {
        "Open R": st.column_config.NumberColumn(format="%+.2f"),
        "%": st.column_config.NumberColumn(format="%+.2f%%"),
        "To stop %": st.column_config.NumberColumn(format="%.2f%%"),
    }
    st.subheader("Open positions" if live_rows else "No open positions")
    if live_rows:
        st.dataframe(pd.DataFrame(live_rows), use_container_width=True,
                     hide_index=True, column_config=pos_cols)
    if open_skipped:
        skipped_rows = open_positions(open_skipped, context)
        with st.expander("%d skipped signal(s) still running — not counted as positions"
                         % len(skipped_rows)):
            st.dataframe(pd.DataFrame(skipped_rows), use_container_width=True,
                         hide_index=True, column_config=pos_cols)
    if live_rows or open_skipped:
        pass  # the tables above say it all
    elif is_trendbreakout(settings):
        if phase == "waiting":
            st.caption("Fills open at %s." % settings.entryFrom)
        elif phase == "armed":
            st.caption("Fills are live; no breakout in the trend direction yet.")
    elif is_supertrend(settings):
        if phase == "waiting":
            st.caption("Entries open at %s." % settings.entryFrom)
        elif phase == "armed":
            st.caption("Entries are live; no Supertrend flip has fired yet.")
    elif phase == "waiting":
        st.caption("Nothing can trigger yet — the opening range has not started "
                   "forming.")
    elif phase == "range":
        st.caption("The opening range is still forming. Entries begin once it "
                   "locks.")
    elif phase == "armed":
        st.caption("Range is locked and entries are live; nothing has broken out "
                   "yet.")

    render_results(trades, context, missing, day, ui, details,
                   max_per_day, max_concurrent)
    return symbols


# ---------------------------------------------------------------------------
#  Rendering
# ---------------------------------------------------------------------------
def render_results(trades, context, missing, day, ui, details,
                   max_per_day, max_concurrent):
    settings = build_settings(ui)
    taken, skipped = apply_caps(trades, max_per_day, max_concurrent)
    closed = [t for t in trades if t.exit == t.exit]

    # -- headline ---------------------------------------------------------------
    st.subheader("%s — %d signals across %d symbols" % (day, len(trades), len(context)))

    # R counts closed trades only; anything still open is shown underneath as
    # "+x.xx open" (marked to the last close) so the headline is never a
    # silent 0.00 while positions are running.
    def open_note(group):
        if not any(t.exit != t.exit for t in group):
            return None
        return "%+.2fR open" % open_r_total(group, context)

    taken_closed = [t for t in taken if t.exit == t.exit]
    c = st.columns(5)
    c[0].metric("Signals", len(trades))
    c[1].metric("Taken (cap %d)" % max_per_day, len(taken))
    c[2].metric("R, taken (closed)",
                "%+.2f" % sum(t.r_multiple for t in taken_closed if t.r_multiple == t.r_multiple),
                open_note(taken), delta_color="off")
    c[3].metric("R, all signals (closed)",
                "%+.2f" % sum(t.r_multiple for t in closed if t.r_multiple == t.r_multiple),
                open_note(trades), delta_color="off")
    wins = sum(1 for t in taken_closed if t.r_multiple > 0.01)
    c[4].metric("Win rate, taken",
                "%d/%d" % (wins, len(taken_closed)) if taken_closed else "—",
                ("%d still open" % (len(taken) - len(taken_closed)))
                if len(taken) > len(taken_closed) else None, delta_color="off")

    if len(trades) > len(taken):
        st.caption(
            "The two R figures differ because you cannot hold every signal. "
            "**R, taken** is the honest one — and which trades land inside the cap "
            "is partly luck of the draw when several fire on the same bar."
        )

    tab_signals, tab_chart, tab_none = st.tabs(["Signals", "Chart", "Passed over"])

    # -- signals ----------------------------------------------------------------
    with tab_signals:
        if not trades:
            st.info("No entries triggered on this date with these settings.")
        else:
            taken_ids = {id(t) for t in taken}
            rows = []
            for n, t in enumerate(trades, 1):
                rows.append({
                    "#": n,
                    "Take": "✅" if id(t) in taken_ids else "—",
                    "Time": entry_at(t),
                    "Symbol": t.symbol,
                    "Side": t.side,
                    "Entry": round(t.entry, 2),
                    "SL": round(t.stop, 2),
                    "T1": round(t.t1, 2),
                    "T2": round(t.t2, 2),
                    "T3": round(t.t3, 2),
                    "Exit": round(t.exit, 2) if t.exit == t.exit else None,
                    "Out": exit_at(t),
                    "Reason": {"SESSION_END": "SQUARE-OFF",
                               "SESSION_END_EST": "SQUARE-OFF (last price)"}.get(t.reason, t.reason),
                    "R": round(t.r_multiple, 2) if t.r_multiple == t.r_multiple else None,
                    "%": round(pct_of(t), 2) if t.exit == t.exit else None,
                    "Day %": _fact(details, t.symbol, "priceChangePct"),
                    "Vol %": _fact(details, t.symbol, "changeInVolPct"),
                    "Scans": ", ".join((details.get(t.symbol) or {}).get("scans", [])[:4]),
                })
            df = pd.DataFrame(rows)
            st.dataframe(
                df, use_container_width=True, hide_index=True,
                column_config={
                    "R": st.column_config.NumberColumn(format="%+.2f"),
                    "%": st.column_config.NumberColumn(format="%+.2f%%"),
                },
            )
            st.download_button(
                "Download CSV", df.to_csv(index=False).encode(),
                "signals_%s.csv" % day, "text/csv")

            if skipped:
                with st.expander("%d signal(s) skipped by the cap" % len(skipped)):
                    for t, why in skipped:
                        r = t.r_multiple
                        if r == r:
                            outcome = "would have been %+.2fR" % r
                        else:
                            bars = (context.get(t.symbol) or {}).get("bars") or []
                            r_now = LS.open_r(t, bars[-1].close) if bars else None
                            outcome = ("still open, %+.2fR so far" % r_now
                                       if r_now is not None else "still open")
                        st.write("%s **%s** %s — %s  (%s)"
                                 % (entry_at(t), t.symbol, t.side, why, outcome))

    # -- chart ------------------------------------------------------------------
    with tab_chart:
        if not context:
            st.info("Nothing to chart.")
        else:
            traded = sorted({t.symbol for t in trades})
            options = traded + [s for s in sorted(context) if s not in traded]
            pick = st.selectbox(
                "Symbol", options,
                format_func=lambda s: ("%s  ●" % s) if s in traded else s)
            ctx = context[pick]
            trade = next((t for t in trades if t.symbol == pick), None)

            m = st.columns(4)
            m[0].metric("Day move", "%+.2f%%" % ctx["day_pct"])
            if ctx.get("tb"):
                m[1].metric("1H trend at last bar", ctx["trend"])
                nxt = ctx["next_buy"] if ctx["trend"] == "UP" else ctx["next_sell"]
                m[2].metric("Next trigger", "close %s %.2f" % (">" if ctx["trend"] == "UP" else "<", nxt)
                            if nxt == nxt else "—")
                m[3].metric("Signals today", sum(1 for t in trades if t.symbol == pick))
            elif "st_line" in ctx:
                m[1].metric("Trend at last bar", ctx["trend"])
                m[2].metric("Supertrend", "%.2f" % ctx["st_last"])
                m[3].metric("Signals today", sum(1 for t in trades if t.symbol == pick))
            else:
                m[1].metric("Range high", "%.2f" % ctx["orb_high"])
                m[2].metric("Range low", "%.2f" % ctx["orb_low"])
                width = ctx["orb_high"] - ctx["orb_low"]
                mid = (ctx["orb_high"] + ctx["orb_low"]) / 2
                m[3].metric("Range width", "%.2f (%.2f%%)" % (width, width / mid * 100 if mid else 0))

            st.altair_chart(candle_chart(pick, ctx, trade, settings), use_container_width=True)

            if ctx["events"]:
                st.caption("Engine events")
                for e in ctx["events"]:
                    st.text("%s  %-13s %s" % (event_at(e, bool(ctx.get("tb"))),
                                              e.type.value, e.message))

    # -- no-trades --------------------------------------------------------------
    with tab_none:
        traded = {t.symbol for t in trades}
        quiet = [s for s in context if s not in traded]
        st.write("**%d symbol(s) scanned, no entry**" % len(quiet))
        if quiet:
            rows = []
            for s in quiet:
                ctx = context[s]
                if ctx.get("tb"):
                    rows.append({
                        "Symbol": s,
                        "Day %": round(ctx["day_pct"], 2),
                        "1H trend": ctx["trend"],
                        "EMA 50": round(ctx["ema_fast"], 2) if ctx["ema_fast"] == ctx["ema_fast"] else None,
                        "EMA 200": round(ctx["ema_slow"], 2) if ctx["ema_slow"] == ctx["ema_slow"] else None,
                        "Waiting for pullback": "yes" if ctx["waiting"] else "",
                    })
                    continue
                if "st_line" in ctx:
                    rows.append({
                        "Symbol": s,
                        "Day %": round(ctx["day_pct"], 2),
                        "Trend": ctx["trend"],
                        "Supertrend": round(ctx["st_last"], 2),
                        "Flips today": sum(1 for a, b in zip(ctx["st_line"], ctx["st_line"][1:])
                                           if a[2] != b[2]),
                    })
                    continue
                width = ctx["orb_high"] - ctx["orb_low"]
                mid = (ctx["orb_high"] + ctx["orb_low"]) / 2
                rows.append({
                    "Symbol": s,
                    "Day %": round(ctx["day_pct"], 2),
                    "Range high": round(ctx["orb_high"], 2),
                    "Range low": round(ctx["orb_low"], 2),
                    "Width %": round(width / mid * 100, 2) if mid else None,
                })
            st.dataframe(pd.DataFrame(rows).sort_values("Day %", ascending=False),
                         use_container_width=True, hide_index=True)
            if is_trendbreakout(settings):
                st.caption("No close beyond the 40-candle high/low in the 1H trend "
                           "direction inside the fill window -- or the trend was "
                           "unclear (EMA 50 and 200 crossed, or price between them), "
                           "or a filter or the late-entry guard held it back.")
            elif is_supertrend(settings):
                st.caption("No flip inside the entry window — the trend held all "
                           "morning, or it turned only after the cut-off.")
            else:
                st.caption("Usually a breakout with no live FVG on the other side of it, "
                           "or a range too thin to clear the buffer.")
        if missing:
            st.warning("No candles for: %s" % ", ".join(missing))
        for err in st.session_state.get("errors", []):
            st.error(err)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
RESULT_SCHEMA = 3

if live_mode:
    if not symbols:
        st.warning("No symbols selected — pick some lists in the sidebar.")
        st.stop()

    _settings = build_settings(ui)
    _phase, _, _next, _ = strategy_phase(_settings)
    if _phase in ("closed", "done"):
        # After square-off (or after the close on a trading day) keep showing
        # the day's final result instead of a blank "0 signals" screen. One
        # pass, no timer: candles are cached under a fixed "final" key.
        if _phase == "closed" and datetime.now(IST).weekday() >= 5:
            live_header(day, symbols, [], {}, _settings)
            st.info("Market closed (%s). Switch to **Review a day** to replay a "
                    "past session." % _next)
            st.stop()
        st.info("Nothing more will happen today (%s). Auto-refresh is paused — "
                "this is the day's final result." % _next)
        render_live(spec, day, ui, max_per_day, max_concurrent, lock_universe, prefer)
        st.stop()

    # Only this fragment reruns on the timer, so the sidebar stays responsive
    # and the page does not flash on every tick.
    @st.fragment(run_every=refresh_secs)
    def _live_tick():
        render_live(spec, day, ui, max_per_day, max_concurrent, lock_universe,
                    prefer)

    _live_tick()
    st.stop()

# -- review mode -------------------------------------------------------------
# The result is stored as a dict stamped with a schema version, not a tuple.
# Session state survives a redeploy, so a positional tuple from an older build
# would blow up on unpack the moment the shape changed. Anything stale is
# dropped and the user is asked to scan again.
stored = st.session_state.get("result")
if not (isinstance(stored, dict) and stored.get("schema") == RESULT_SCHEMA):
    if stored is not None:
        st.session_state.pop("result", None)
    stored = None

if run:
    if not symbols:
        st.warning("No symbols selected.")
        st.stop()
    st.session_state["errors"] = []
    _trades, _context, _missing = scan(symbols, day, ui, prefer=prefer)
    stored = {
        "schema": RESULT_SCHEMA, "trades": _trades, "context": _context,
        "missing": _missing, "day": day, "ui": ui, "details": details,
        "max_per_day": max_per_day, "max_concurrent": max_concurrent,
    }
    st.session_state["result"] = stored

if stored is None:
    st.info("Choose a universe and a date in the sidebar, then press **Scan**.")
    st.stop()

render_results(stored["trades"], stored["context"], stored["missing"],
               stored["day"], stored["ui"], stored.get("details") or {},
               stored["max_per_day"], stored["max_concurrent"])
