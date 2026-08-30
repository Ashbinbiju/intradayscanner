#!/usr/bin/env python
"""
ORB + FVG scanner -- a Streamlit view of which stocks actually triggered.

    streamlit run scanner.py

Pick a date and a symbol source, and it runs the same engine the backtester and
the live runner use, then shows every entry in the order it fired. The cap
controls answer the question that matters in practice: if you can only take two
or three trades, which ones would you actually have been in?

Symbols come either from the Supabase watchlist (a category, filtered to the
stocks listed during the run-up to the opening range) or from a list you type.
Candles come from the local cache when present, otherwise from Angel One.
"""

from __future__ import annotations

import os
import pickle
from collections import defaultdict
from datetime import date as _date, datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

# Secrets must reach the environment before config is imported, because config
# resolves credentials with os.getenv at import time. On Streamlit Cloud they
# come from .streamlit/secrets.toml; locally, from the shell.
for _key in ("CLIENT_ID", "PASSWORD", "TOTP_SECRET", "API_KEY",
             "SUPABASE_URL", "SUPABASE_KEY"):
    try:
        if _key in st.secrets and not os.getenv(_key):
            os.environ[_key] = str(st.secrets[_key])
    except Exception:
        pass  # no secrets file; fall back to the environment

import config
from orbfvg import backtest as bt
from orbfvg.strategy import Bar, ORBFVGStrategy

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


class SupabaseError(RuntimeError):
    """Carries a human explanation, not just an HTTP status."""

    def __init__(self, message, hint="", sql=""):
        super().__init__(message)
        self.hint = hint
        self.sql = sql


@st.cache_data(show_spinner=False, ttl=300)
def watchlist_symbols(url, key, category, start_t, end_t):
    """symbol -> dates it was listed inside the time window."""
    import requests

    from scan_watchlist import qualifying

    base = url.rstrip("/")
    probe = requests.get(
        "%s/rest/v1/watchlist_snapshots?select=date&limit=1" % base,
        headers={"apikey": key, "Authorization": "Bearer " + key}, timeout=30,
    )

    if probe.status_code in (401, 403):
        raise SupabaseError(
            "Supabase rejected the key (HTTP %d)." % probe.status_code,
            hint=(
                "The URL resolved, so SUPABASE_URL is fine — it is SUPABASE_KEY "
                "that is wrong. In the Supabase dashboard go to **Project "
                "Settings → API Keys** and copy a key that can read this table, "
                "then paste it into the Streamlit **Secrets** box (App menu → "
                "Settings → Secrets) and reboot the app.\n\n"
                "Watch for: a stale key if you rotated it, stray quotes or "
                "line breaks in the TOML value, or a publishable/anon key on a "
                "table that has row-level security switched on. If RLS is on, "
                "an anon key also needs a read policy:"
            ),
            sql=(
                "alter table public.watchlist_snapshots enable row level security;\n\n"
                "create policy \"anon can read watchlist\"\n"
                "  on public.watchlist_snapshots\n"
                "  for select\n"
                "  to anon\n"
                "  using (true);"
            ),
        )
    if probe.status_code >= 400:
        raise SupabaseError("Supabase returned HTTP %d: %s"
                            % (probe.status_code, probe.text[:200]))
    if not probe.json():
        raise SupabaseError(
            "The key works, but watchlist_snapshots returned no rows.",
            hint=("Usually row-level security with no SELECT policy for this "
                  "key's role — the request succeeds and is filtered to nothing. "
                  "Add a read policy:"),
            sql=("create policy \"anon can read watchlist\"\n"
                 "  on public.watchlist_snapshots\n"
                 "  for select\n  to anon\n  using (true);"),
        )

    from scan_watchlist import fetch_watchlist

    rows = fetch_watchlist(url, key, category)
    if not rows:
        raise SupabaseError("No rows found for category %r." % category,
                            hint="Try a different category in the sidebar.")
    by_symbol = qualifying(rows, start_t, end_t)
    return {k: sorted(v) for k, v in by_symbol.items()}


def mask(value):
    if not value:
        return "(not set)"
    return "%s…%s  (%d chars)" % (value[:8], value[-4:], len(value)) \
        if len(value) > 14 else "(set, %d chars)" % len(value)


@st.cache_data(show_spinner=False, ttl=300)
def fetch_candles(symbol, exchange, day_from, day_to):
    """Cache first, Angel second. Returns (rows, meta) or (None, None)."""
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


# ---------------------------------------------------------------------------
#  Engine
# ---------------------------------------------------------------------------
def build_settings(ui):
    s, _ = config.load()
    s.atrMult = ui["atrMult"]
    s.strongClose = ui["strongClose"]
    s.useFvg = ui["useFvg"]
    s.fvgAtrX = ui["fvgAtrX"]
    s.bufPct = ui["bufPct"]
    s.useTrail = ui["useTrail"]
    s.trailMode = ui["trailMode"]
    s.sqOffTime = ui["sqOff"]
    s.minRangePct = ui["minRangePct"]
    s.validate()
    return s


def scan(symbols, target_day, ui, exchange="NSE"):
    """Run the engine over each symbol and collect that day's trades."""
    settings = build_settings(ui)
    warm_from = (datetime.strptime(target_day, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")

    trades, context, missing = [], {}, []
    progress = st.progress(0.0, text="Scanning...")
    for n, symbol in enumerate(symbols, 1):
        progress.progress(n / max(len(symbols), 1), text="Scanning %s (%d/%d)" % (symbol, n, len(symbols)))
        rows, meta = fetch_candles(symbol, exchange, warm_from, target_day)
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
    st.header("Universe")
    source = st.radio("Symbols from", ["Supabase watchlist", "Type a list"], index=0)

    symbols, sel_dates = [], {}
    if source == "Supabase watchlist":
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY", "")

        with st.expander("Connection", expanded=not (url and key)):
            st.caption("From Streamlit secrets or the environment. Override "
                       "here to test a key without redeploying.")
            st.text("URL  %s" % (url or "(not set)"))
            st.text("KEY  %s" % mask(key))
            url = st.text_input("Supabase URL", value=url) or url
            typed_key = st.text_input(
                "Supabase key", value="", type="password",
                placeholder="leave blank to use the secret")
            if typed_key.strip():
                key = typed_key.strip()

        category = st.selectbox(
            "Category", ["TOP_MOMENTUM", "SECTOR_MOMENTUM", "GAINER", "LOSER"])
        c1, c2 = st.columns(2)
        start_t = c1.text_input("Listed from", "12:00")
        end_t = c2.text_input("to", "12:45")
        st.caption("The window the stock must appear in — the run-up to the "
                   "opening range, not the whole day.")

        if url and key:
            try:
                sel_dates = watchlist_symbols(url, key, category, start_t, end_t)
            except SupabaseError as exc:
                st.error(str(exc))
                if exc.hint:
                    st.info(exc.hint)
                if exc.sql:
                    st.code(exc.sql, language="sql")
            except Exception as exc:
                st.error("Watchlist fetch failed: %s" % exc)
        else:
            st.warning("Set SUPABASE_URL and SUPABASE_KEY in the app's Secrets, "
                       "or paste them above. You can also switch the source to "
                       "**Type a list** and skip Supabase entirely.")
    else:
        typed = st.text_area(
            "One per line or comma separated",
            "NEWGEN\nATHERENERG\nTEJASNET\nCOFORGE\nSHYAMMETL\nCGCL")
        symbols = [s.strip().upper() for s in typed.replace(",", "\n").split("\n") if s.strip()]

    st.header("Date")
    available = sorted({d for ds in sel_dates.values() for d in ds}) if sel_dates else []
    if available:
        day = st.selectbox("Trading day", available[::-1], index=0)
        symbols = sorted(s for s, ds in sel_dates.items() if day in ds)
        st.caption("%d symbols qualified on %s" % (len(symbols), day))
    else:
        day = st.date_input("Trading day", _date(2026, 8, 28)).strftime("%Y-%m-%d")

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

    run = st.button("Scan", type="primary", use_container_width=True)

if not run and "result" not in st.session_state:
    st.info("Choose a universe and a date in the sidebar, then press **Scan**.")
    st.stop()

if run:
    if not symbols:
        st.warning("No symbols selected.")
        st.stop()
    st.session_state["errors"] = []
    trades, context, missing = scan(symbols, day, ui)
    st.session_state["result"] = (trades, context, missing, day, ui,
                                  max_per_day, max_concurrent)

trades, context, missing, day, ui, max_per_day, max_concurrent = st.session_state["result"]
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
