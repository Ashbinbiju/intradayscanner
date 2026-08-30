#!/usr/bin/env python
"""
ORB + FVG scanner -- a Streamlit view of which stocks actually triggered.

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
for _key in ("CLIENT_ID", "PASSWORD", "TOTP_SECRET", "API_KEY"):
    try:
        if _key in st.secrets and not os.getenv(_key):
            os.environ[_key] = str(st.secrets[_key])
    except Exception:
        pass  # no secrets file; fall back to the environment

import config
from orbfvg import backtest as bt
from orbfvg import screener
from orbfvg.strategy import Bar

IST = ZoneInfo("Asia/Kolkata")
LONDON = ZoneInfo("Europe/London")

st.set_page_config(page_title="ORB + FVG Scanner", page_icon="📈", layout="wide")


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_client():
    from orbfvg.angel import AngelClient

    client = AngelClient()
    client.login()
    return client


@st.cache_data(show_spinner=False)
def load_cache_file():
    """Whatever build_cache.py last wrote, if anything."""
    files = sorted(f for f in os.listdir(config.DATA_DIR) if f.startswith("candles_"))
    if not files:
        return {"candles": {}, "meta": {}, "selection": {}}
    with open(os.path.join(config.DATA_DIR, files[-1]), "rb") as fh:
        return pickle.load(fh)


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


@st.cache_data(show_spinner=False, ttl=900)
def fetch_candles(symbol, exchange, day_from, day_to, bucket=""):
    """Cache first, Angel second. Returns (rows, meta) or (None, None).

    `bucket` is unused in the body -- it exists so a new 5-minute slot is a
    cache miss and forces a re-fetch.
    """
    store = load_cache_file()
    if symbol in store["candles"]:
        rows = store["candles"][symbol]
        meta = store["meta"].get(symbol, {"tick": 0.05})
        keep = [r for r in rows if day_from <= r[0][:10] <= day_to]
        if keep:
            return keep, meta
    try:
        client = get_client()
        inst = client.instrument(symbol, exchange)
        bars = client.candles(
            exchange, inst.token, "FIVE_MINUTE",
            datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=IST),
            datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1),
            tz=IST,
        )
    except Exception as exc:
        st.session_state.setdefault("errors", []).append("%s: %s" % (symbol, exc))
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
    s, _ = config.load()
    ui = ui or {}
    for ui_key, attr in (("atrMult", "atrMult"), ("strongClose", "strongClose"),
                         ("useFvg", "useFvg"), ("fvgAtrX", "fvgAtrX"),
                         ("bufPct", "bufPct"), ("useTrail", "useTrail"),
                         ("trailMode", "trailMode"), ("sqOff", "sqOffTime"),
                         ("minRangePct", "minRangePct")):
        if ui.get(ui_key) is not None:
            setattr(s, attr, ui[ui_key])
    s.validate()
    return s


def scan(symbols, target_day, ui, exchange="NSE", bucket="", quiet=False):
    """Run the engine over each symbol and collect that day's trades."""
    settings = build_settings(ui)
    warm_from = (datetime.strptime(target_day, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")

    trades, context, missing = [], {}, []
    progress = None if quiet else st.progress(0.0, text="Scanning...")
    for n, symbol in enumerate(symbols, 1):
        if progress is not None:
            progress.progress(n / max(len(symbols), 1),
                              text="Scanning %s (%d/%d)" % (symbol, n, len(symbols)))
        rows, meta = fetch_candles(symbol, exchange, warm_from, target_day, bucket)
        if not rows:
            missing.append(symbol)
            continue
        bars = to_bars(rows)
        settings.mintick = meta.get("tick", 0.05)
        result = bt.run(bars, settings)

        day_bars = [b for b in bars if b.time.strftime("%Y-%m-%d") == target_day]
        if not day_bars:
            missing.append(symbol)
            continue

        orb = [b for b in day_bars if in_or_window(b, settings)]
        ctx = {
            "bars": day_bars,
            "orb_high": max((b.high for b in orb), default=float("nan")),
            "orb_low": min((b.low for b in orb), default=float("nan")),
            "day_pct": (day_bars[-1].close / day_bars[0].open - 1) * 100,
            "events": [e for e in result.events if e.time.strftime("%Y-%m-%d") == target_day],
        }
        context[symbol] = ctx
        for t in result.trades:
            if t.entry_time.strftime("%Y-%m-%d") == target_day:
                t.symbol = symbol
                trades.append(t)
    if progress is not None:
        progress.empty()
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
            open_until.append(t.exit_time or t.entry_time)
    return taken, skipped


def pct_of(t):
    return (t.exit - t.entry) * (1 if t.side == "BUY" else -1) / t.entry * 100


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
st.title("ORB + FVG Scanner")

with st.sidebar:
    st.header("Mode")
    mode = st.radio(
        "Mode", ["Review a day", "Live"], index=0, label_visibility="collapsed",
        help="Review replays a chosen day once, on demand. Live re-scans by "
             "itself at every 5-minute close during market hours.")
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
    source = st.radio(
        "Symbols from",
        ["Screener (live)", "Recorded snapshot", "Type a list"], index=0)

    symbols, details = [], {}
    spec = {"source": "typed", "symbols": []}

    if source == "Screener (live)":
        try:
            data, details = screener_snapshot()
        except screener.ScreenerError as exc:
            st.error(str(exc))
            st.info("The screener API is unreachable. Switch the source to "
                    "**Type a list** to carry on without it.")
            data = {}
        if data:
            side = st.radio("Direction", ["Bullish", "Bearish", "Both"],
                            index=0, horizontal=True)
            pool = (screener.BULLISH if side == "Bullish" else
                    screener.BEARISH if side == "Bearish" else
                    screener.BULLISH + screener.BEARISH)
            available_buckets = [b for b in pool if screener.symbols_in(data, b)]
            default = [b for b in ("stocksNearDaysHighAnd3DayHigh",
                                   "stocksNearDaysHighAndYestHigh",
                                   "high52Week", "volumeGainers")
                       if b in available_buckets] or available_buckets[:2]
            chosen = st.multiselect(
                "Lists", available_buckets, default=default,
                format_func=lambda b: "%s (%d)" % (
                    screener.LABELS.get(b, b), len(screener.symbols_in(data, b))))
            min_chg = st.slider("Min |price change| %", 0.0, 15.0, 0.0, 0.5)
            spec = {"source": "live", "buckets": chosen, "min_chg": min_chg}
            symbols, details = resolve_universe(spec)
            st.caption("%d symbols selected" % len(symbols))
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

    st.header("Position limits")
    max_per_day = st.number_input("Max trades per day", 1, 20, 3)
    max_concurrent = st.number_input("Max open at once", 1, 20, 3)

    run = st.button("Scan now" if live_mode else "Scan",
                    type="primary", use_container_width=True)

# ---------------------------------------------------------------------------
#  Live panels
# ---------------------------------------------------------------------------
def open_positions(trades, context):
    """Signals with no exit yet, marked to the latest bar close."""
    rows = []
    for t in trades:
        if t.exit == t.exit:          # already closed
            continue
        bars = (context.get(t.symbol) or {}).get("bars") or []
        if not bars:
            continue
        last = bars[-1].close
        direction = 1 if t.side == "BUY" else -1
        risk = abs(t.entry - t.stop)
        rows.append({
            "Symbol": t.symbol,
            "Side": t.side,
            "In at": t.entry_time.strftime("%H:%M"),
            "Entry": round(t.entry, 2),
            "Now": round(last, 2),
            "Open R": round((last - t.entry) * direction / risk, 2) if risk else None,
            "%": round((last - t.entry) * direction / t.entry * 100, 2),
            "Stop": round(t.stop, 2),
            "To stop %": round(abs(last - t.stop) / last * 100, 2),
            "T1": round(t.t1, 2),
            "T2": round(t.t2, 2),
            "T3": round(t.t3, 2),
            "Next target": ("T1" if not _reached(t, 1) else
                            "T2" if not _reached(t, 2) else
                            "T3" if not _reached(t, 3) else "—"),
        })
    return rows


def _reached(trade, n):
    """Has target n already been tagged on this trade?"""
    return getattr(trade, "targets_hit", 0) >= n


def _hms(seconds):
    if seconds is None:
        return "—"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%dh %02dm" % (h, m) if h else "%dm %02ds" % (m, s)


def live_header(day, symbols, trades, context, settings):
    now = datetime.now(IST)
    phase, label, nxt, secs = strategy_phase(settings, now)
    icon = {"waiting": "🟡", "range": "🟠", "armed": "🟢",
            "done": "⚪", "closed": "🔴"}[phase]
    live_trades = [t for t in trades if t.exit != t.exit]

    c = st.columns(5)
    c[0].metric("Strategy", "%s %s" % (icon, label), nxt)
    c[1].metric("Next bar" if phase in ("range", "armed") else "Countdown",
                _hms(seconds_to_next_bar(now)) if phase in ("range", "armed")
                else _hms(secs))
    c[2].metric("Watching", len(symbols))
    c[3].metric("Signals today", len(trades))
    c[4].metric("Open now", len(live_trades))

    clock = session_clock(settings, now)
    plan = "Range %s–%s · entries from %s · square-off %s (%s in %s)" % (
        clock["or_start"].strftime("%H:%M") if clock["or_start"] else "—",
        clock["or_end"].strftime("%H:%M") if clock["or_end"] else "—",
        clock["or_end"].strftime("%H:%M") if clock["or_end"] else "—",
        (clock["square_off"] or clock["sig_end"]).strftime("%H:%M")
        if (clock["square_off"] or clock["sig_end"]) else "—",
        settings.orSess, settings.tzIn)
    st.caption("%s  ·  refreshed %s IST" % (plan, now.strftime("%H:%M:%S")))
    return phase


def render_live(spec, day, ui, max_per_day, max_concurrent, lock_after_range=True):
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
    locked = st.session_state.get("locked_universe")
    if lock_after_range and phase_key in ("armed", "done"):
        if locked:
            symbols = locked
        else:
            st.session_state["locked_universe"] = symbols
    elif phase_key in ("waiting", "range"):
        st.session_state.pop("locked_universe", None)

    previous = st.session_state.get("last_universe")
    st.session_state["last_universe"] = list(symbols)

    bucket = bar_bucket()
    trades, context, missing = scan(symbols, day, ui, bucket=bucket, quiet=True)
    phase = live_header(day, symbols, trades, context, settings)

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

    live_rows = open_positions(trades, context)
    st.subheader("Open positions" if live_rows else "No open positions")
    if live_rows:
        st.dataframe(
            pd.DataFrame(live_rows), use_container_width=True, hide_index=True,
            column_config={
                "Open R": st.column_config.NumberColumn(format="%+.2f"),
                "%": st.column_config.NumberColumn(format="%+.2f%%"),
                "To stop %": st.column_config.NumberColumn(format="%.2f%%"),
            })
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

    c = st.columns(5)
    c[0].metric("Signals", len(trades))
    c[1].metric("Taken (cap %d)" % max_per_day, len(taken))
    c[2].metric("R, taken", "%+.2f" % sum(t.r_multiple for t in taken if t.r_multiple == t.r_multiple))
    c[3].metric("R, all signals", "%+.2f" % sum(t.r_multiple for t in closed))
    wins = sum(1 for t in taken if t.r_multiple > 0.01)
    c[4].metric("Win rate, taken", "%d/%d" % (wins, len(taken)) if taken else "—")

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
                    "Time": t.entry_time.strftime("%H:%M"),
                    "Symbol": t.symbol,
                    "Side": t.side,
                    "Entry": round(t.entry, 2),
                    "SL": round(t.stop, 2),
                    "T1": round(t.t1, 2),
                    "T2": round(t.t2, 2),
                    "T3": round(t.t3, 2),
                    "Exit": round(t.exit, 2) if t.exit == t.exit else None,
                    "Out": t.exit_time.strftime("%H:%M") if t.exit_time else "open",
                    "Reason": t.reason,
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
                        st.write("%s **%s** %s — %s  (would have been %+.2fR)"
                                 % (t.entry_time.strftime("%H:%M"), t.symbol, t.side,
                                    why, t.r_multiple))

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
            m[1].metric("Range high", "%.2f" % ctx["orb_high"])
            m[2].metric("Range low", "%.2f" % ctx["orb_low"])
            width = ctx["orb_high"] - ctx["orb_low"]
            mid = (ctx["orb_high"] + ctx["orb_low"]) / 2
            m[3].metric("Range width", "%.2f (%.2f%%)" % (width, width / mid * 100 if mid else 0))

            st.altair_chart(candle_chart(pick, ctx, trade, settings), use_container_width=True)

            if ctx["events"]:
                st.caption("Engine events")
                for e in ctx["events"]:
                    st.text("%s  %-13s %s" % (e.time.strftime("%H:%M"), e.type.value, e.message))

    # -- no-trades --------------------------------------------------------------
    with tab_none:
        traded = {t.symbol for t in trades}
        quiet = [s for s in context if s not in traded]
        st.write("**%d symbol(s) scanned, no entry**" % len(quiet))
        if quiet:
            rows = []
            for s in quiet:
                ctx = context[s]
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
            st.caption("Usually a breakout with no live FVG on the other side of it, "
                       "or a range too thin to clear the buffer.")
        if missing:
            st.warning("No candles for: %s" % ", ".join(missing))
        for err in st.session_state.get("errors", []):
            st.error(err)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
RESULT_SCHEMA = 2

if live_mode:
    if not symbols:
        st.warning("No symbols selected — pick some lists in the sidebar.")
        st.stop()

    _settings = build_settings(ui)
    _phase, _, _next, _ = strategy_phase(_settings)
    if _phase in ("closed", "done") and not run:
        live_header(day, symbols, [], {}, _settings)
        st.info("Nothing more will happen today (%s). Auto-refresh is paused — "
                "press **Scan now** to review today's bars anyway." % _next)
        st.stop()

    # Only this fragment reruns on the timer, so the sidebar stays responsive
    # and the page does not flash on every tick.
    @st.fragment(run_every=refresh_secs)
    def _live_tick():
        render_live(spec, day, ui, max_per_day, max_concurrent, lock_universe)

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
    _trades, _context, _missing = scan(symbols, day, ui)
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
