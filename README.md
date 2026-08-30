# ORB + FVG on Angel One

A Python port of the Pine v6 indicator **"Forex ORB + FVG — Buy/Sell + Targets"**,
wired to Angel One SmartAPI so it can be backtested and traded on NSE.

The trading rules are unchanged. Only the drawing calls are gone, replaced by
events an execution layer acts on.

---

## Sessions — read this first

The indicator ships with a **London** session: `0800-0815 Europe/London`. Those
defaults are kept, because that is what runs on TradingView with untouched
inputs. On an NSE chart, while BST is in effect, they land on:

| Pine input | London | IST |
|---|---|---|
| `orSess` opening range | 08:00–08:15 | **12:30–12:45** |
| `sigSess` signal window | 08:15–16:30 | **12:45–21:00** |

So the opening range is built from the 12:30 / 12:35 / 12:40 bars and entries
run from 12:45 IST — *not* from the 09:15 market open.

`--preset nse` switches the range to 09:15–09:30 IST instead. That is a
different strategy, not a porting detail; it is opt-in for a reason.

---

## The strategy

1. The first 15 minutes after the session open build a range. Its high and low
   lock when that window closes.
2. On 5-minute bars the engine tracks Fair Value Gaps — the 3-candle imbalance
   where `low[0] > high[2]` (bullish) or `high[0] < low[2]` (bearish).
3. **BUY** when a 5-minute candle closes above the range high *and* a live
   bullish FVG exists. **SELL** on the mirror condition.
4. Stop from ATR, targets at 1R / 2R / 3R. T3 closes the trade; so does the
   stop, or the end of the signal window.

Two defaults deviate from the indicator, both on measured evidence (see
[Fakeout filters](#fakeout-filters--what-was-tested-and-what-actually-worked)):
`atrMult = 3.0` where Pine ships 1.5, and `strongClose = 0.5` which Pine does
not have. To reproduce TradingView exactly:

```bash
python run.py backtest --symbol X --set atrMult=1.5 --set strongClose=0 --set sqOffTime=
```

---

## Setup

```bash
pip install -r requirements.txt
```



Credentials are read from the environment, from a git-ignored `.env`, or from
Streamlit secrets — never from source. Copy `.env.example` to `.env` and fill in:

```bash
CLIENT_ID=<client id>
PASSWORD=<mpin>
TOTP_SECRET=<base32 secret from authenticator setup>
API_KEY=<api key>
```

A TOTP secret plus an MPIN is full access to the trading account, so nothing
is hardcoded and `.env` is git-ignored. `config.require_credentials()` fails
with a clear message when any are missing.

---

## Scanner

```bash
streamlit run scanner.py
```

A visual view of which stocks actually triggered on a given day. Symbols come
from three sources: the live **intradayscreener.com** API (pick any of its 21
lists), a **recorded snapshot** of that API from a past day, or a list you type.
Candles come from the local cache when present, otherwise Angel One.

Three tabs: **Signals** (every entry in the order it fired, with entry / SL /
T1-T3 / exit / R, and a ✅ on the ones that fit inside your daily cap),
**Chart** (candles with the opening range, levels and entry/exit marked), and
**Passed over** (symbols that were scanned but never triggered, with their
range width — usually a breakout with no live FVG behind it).

Every strategy input is a sidebar control, so you can re-run the same day at a
different `atrMult` or with the FVG gate off and see the difference
immediately. The position limits matter most: the app shows R for *all* signals
and R for *the ones you could actually have taken*, which are rarely the same
number.

### Why snapshots matter

The screener API reports **now**, with no history. Backtesting needs the
opposite — what was on the list *at the time*. Running today's movers over last
week's charts is hindsight, and it flatters every result.

```bash
python record_snapshot.py --loop 300 --market-hours-only
```

That writes `data/screener/YYYY-MM-DD/HHMM.json` every five minutes, and the
scanner's **Recorded snapshot** source reads it back with the same
"listed between 12:00 and 12:45" filter the analysis used. Until a few days
have accumulated, use the live source with today's date, or type symbols in.

### Deploying it

The app reads `st.secrets` first and falls back to environment variables, so
the same file runs locally and on Streamlit Cloud. Copy
`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` (git-ignored) for
local use, or paste the same keys into the Streamlit Cloud **Secrets** box.

On Streamlit Community Cloud, do both of these — secrets alone are not enough:

1. **Deploy from a private repo**, and
2. under *Settings → Sharing*, set the app to **specific viewers** and allowlist
   your own email.

Secrets keep the keys out of the repository. They do not stop someone who has
the URL from *using* the app, and the app carries credentials for a live
trading account. The viewer allowlist is what closes that.

One practical note: `data/` is git-ignored, so a cloud deploy has no candle
cache and fetches everything from Angel on demand — the first scan of a day is
slow because of the 3 requests/second limit and the ~35 MB scrip master
download. The screener API itself needs no credentials.

---

## Commands

```bash
python run.py check --symbol SBIN-EQ        # login, funds, instrument, settings
python run.py symbols RELIANCE              # find tradingsymbols and tokens
python run.py backtest --symbol SBIN-EQ --days 45
python run.py signals --symbol SBIN-EQ      # today's signals, no orders
python run.py live --symbol SBIN-EQ         # dry run — logs orders, sends none
python run.py live --symbol SBIN-EQ --real  # places real orders (asks to confirm)
```

Any strategy input can be changed without touching code:

```bash
python run.py backtest --symbol SBIN-EQ --days 60 \
    --set useFvg=false --set atrMult=2.0 --set slMode="Opposite range level"
```

`--preset forex` restores the indicator's original London-session defaults.

---

## Layout

| File | Role |
|---|---|
| [orbfvg/pine.py](orbfvg/pine.py) | Pine built-ins: `na`, `ta.atr`, `ta.rma`, `series[n]`, session windows |
| [orbfvg/strategy.py](orbfvg/strategy.py) | The port. Bar in, events out. No broker knowledge |
| [orbfvg/angel.py](orbfvg/angel.py) | SmartAPI: TOTP login, rate limiting, candles, orders |
| [orbfvg/instruments.py](orbfvg/instruments.py) | Scrip master lookup (symbol → token, tick, lot) |
| [orbfvg/broker.py](orbfvg/broker.py) | Events → orders. Sizing, protective stop, journal |
| [orbfvg/live.py](orbfvg/live.py) | Live loop: warm up, then one bar at a time |
| [orbfvg/backtest.py](orbfvg/backtest.py) | Historical replay and reporting |
| [orbfvg/screener.py](orbfvg/screener.py) | intradayscreener.com client, bucket parsing, snapshot history |
| [config.py](config.py) | Every input, in one place |

The backtester and the live runner drive the **same** `ORBFVGStrategy`, so what
you test is what trades.

---

## Fidelity notes

Details of the Pine source that are easy to "fix" by accident, and are
deliberately preserved:

- **ATR warm-up.** `ta.rma` returns `na` until `atrLen` bars have passed. While
  ATR is `na` no FVG can form and no ATR-stop entry can be taken. The port
  inherits this — NaN comparison in Python is false, which is how Pine treats
  `na`.
- **The breakout cross** compares the *previous* bar's source against the
  *current* bar's level (`srcH[1] <= lvlUp`, not `lvlUp[1]`).
- **Same-bar resolution.** When one bar spans both T3 and the stop, targets are
  evaluated first and the stop test is skipped once T3 has closed the position.
- **`oncePer` is per side.** A day can take one long *and* one short.
- **FVG expiry is `age > fvgLB`**, so a 12-bar lookback keeps a gap alive at age
  12 and drops it at 13.
- **`pip` is a series.** `"Auto"` re-evaluates `close > 20` every bar.

`tests/test_strategy.py` pins each of these.

---

## Square-off

The indicator closes an open trade on `sessEnd` — Pine's "first bar **outside**
the signal window that follows a bar inside it".

With the London session that window runs to **21:00 IST**, hours after NSE
stops printing bars. So `sessEnd` never fires on the day and the trade rides to
the next morning. That is genuinely what TradingView shows — but it is not
tradeable on an intraday product.

`sqOffTime` (default `15:15`, in `sqOffTz` = `Asia/Kolkata`) closes the position
at that wall-clock time and blocks new entries from then on. It applies in the
**backtest and live alike**, so the two agree. `square_off_time` in
`TradeSettings` is the live-only backstop for when no bar prints at all.

Set `--set sqOffTime=` (empty) to disable it and reproduce TradingView exactly.

---

## Trailing stop — off by default

Not part of the original indicator, which holds one fixed stop from entry to
exit, so it ships **off**: the bot matches what you validated on TradingView.

Turn it on with `--set useTrail=true`. Three modes, set with
`--set trailMode=...`:

| Mode | Behaviour |
|---|---|
| `ATR` *(default)* | Stop trails `trailAtrMult × ATR` below the best price reached, once the trade is `trailStartR` in profit |
| `Targets` | Stop to breakeven when T1 is hit, then up to T1 when T2 is hit |
| `Both` | Whichever of the two is tighter |

`useTrail=false` (the default) is the indicator's fixed stop, exactly.

Two rules hold in every mode: **the stop only ever tightens**, and **trailing
runs last in the bar** — a stop raised by this bar's high is never also tested
against this bar's low, because the intrabar order of those two extremes is
unknowable. A tightened stop takes effect from the next bar.

Live, the broker-side SL-M order is modified to follow the engine stop; if the
modify is rejected the order is cancelled and replaced rather than left stale.

### What trailing actually did — check this before leaving it on

Trailing **did not add return** in testing, and on a real trade it cost a lot.

WOCKPHARMA-EQ, short from 1994.60 on Fri 28 Aug 2026:

| Configuration | Exit | Result |
|---|---|---|
| No square-off, no trail (TradingView) | T3 next session | +3.00R |
| 15:15 square-off, no trail | 1973.60 at 15:15 | **+1.72R** |
| 15:15 square-off + ATR trail | 1991.43 at 15:00 | **+0.26R** |

The ATR trail jumped the stop from 2006.81 to 1991.43 the moment T1 was hit,
and an ordinary retrace took it out ten minutes later — turning a +1.72R day
into +0.26R.

Across a wider sample (126 trades, 5 NSE symbols, 45 days, the `nse` preset)
the picture was flat-to-negative on return, positive on risk:

| Mode | Total R | Summed max drawdown |
|---|---|---|
| Off | +6.31 | −43.11 |
| Targets | +6.90 | −31.30 |
| ATR ×2 after 1R | +6.12 | −27.83 |
| Both | +5.40 | −27.52 |

Trailing bought roughly a **35% cut in drawdown** for about the same money.
That is a real trade-off, not a free win — and it is why the default is off.
If you want the smoother equity curve rather than the bigger winners, turn it
on with `--set useTrail=true` and re-run the comparison on your own symbols.

---

## Safety

- `dry_run` is **on by default**. `--real` is required to send orders, and it
  asks for typed confirmation.
- `use_broker_sl` parks an SL-M order at the engine's stop, so an adverse move
  between 5-minute closes is not unprotected. It is cancelled when the trade ends.
- `max_trades_per_day` (default 4) caps activity.
- `max_quantity` caps size regardless of the sizing formula.
- Every closed trade is appended to `logs/trades_YYYYMMDD.csv`.

Position sizing is `fixed` by default. Switch to risk-based with:

```bash
SIZING_MODE=risk CAPITAL=200000 RISK_PCT=0.5 python run.py live --symbol SBIN-EQ
```

---

## Fakeout filters — what was tested, and what actually worked

Six filters were added for this. Five stay off (`confirmBars=1`, `volMult=0`,
`minRangePct=0`, `reentrySameBar=false`); **`strongClose=0.5` is on**, together
with **`atrMult=3.0`**, because the numbers below justified it.
[sweep.py](sweep.py) compares them offline against candles cached by
[build_cache.py](build_cache.py); "baseline" in every table means the indicator
untouched, not the shipped config.

Tested on 132 trades, 166 symbols, 12 trading days (13–28 Aug 2026), the
TOP_MOMENTUM noon universe:

| Config | Trades | Total R | Total % | Win% |
|---|---|---|---|---|
| baseline (`atrMult=1.5`) | 132 | −12.45 | −3.42 | 35.7% |
| `atrMult=2.5` | 125 | +2.45 | +7.62 | 45.1% |
| `atrMult=3.0` | 121 | +3.43 | +9.56 | 44.9% |
| **`atrMult=3.0 strongClose=0.5`** (shipped) | 112 | **+8.60** | **+17.64** | 47.3% |
| `atrMult=2.5 fvgAtrX=0.5` | 71 | +7.13 | +11.58 | **50.0%** |

**The problem was not fake breakouts — it was the stop.** At 1.5 × ATR the stop
sits inside ordinary noise and gets speared before the move develops. Widening
it to 2.5–3.0 lifts the win rate from 35.7% to ~45% while keeping 92% of the
trades. The ATR curve peaks at 3.0 and turns negative by 4.0, so it is a real
optimum rather than "no stop is best".

Both yardsticks are shown because they answer different questions: **R** is
right if you size down as the stop widens (`sizing_mode=risk`); **%** is right
for a fixed rupee position. They agree here, which is why the result is
believable.

What genuinely filtered, on top of the stop:

* `strongClose=0.5` — breakout bar must close in the upper/lower half of its
  own range. Cheap, keeps 87% of trades.
* `fvgAtrX=0.5` — demand a bigger imbalance. Highest quality (50% win) but
  halves the trade count.

What did **not** work, despite sounding right:

* **Bigger `bufPct`** (10/15/20) — all worse than baseline. Pushing the trigger
  further past the level costs more in entry price than it saves in fakeouts.
* **`confirmBars`** — waiting a bar for confirmation lost 6.8R at N=2.
* **`volMult`** — total R improves only because it trades less. Win rate stays
  at 35.8% vs the baseline's 35.7%, so it is not selecting better trades.
* **`reentrySameBar`** — real but rare (+0.9R, 2 extra trades).

Caveats worth taking seriously: 12 days and 132 trades is a small sample, and
roughly 30 configurations were tried against it, so some of the spread is noise.
The `atrMult` result is the most trustworthy — smooth curve, barely changes
trade selection, both yardsticks agree. The combinations are likelier to be
partly fitted. And at +0.157% per trade gross, intraday costs (brokerage, STT,
slippage — roughly 0.08% round trip) eat about half the edge.

---

## Tests

```bash
python tests/test_strategy.py    # engine rules vs. the Pine source
python tests/test_broker.py      # sizing, payloads, caps, journal — offline
```

---

## Before trading real money

1. Compare `python run.py backtest` entries against the indicator on a
   TradingView chart over the same window. They should match bar for bar.
2. Run `python run.py live` in dry run for a full session and read the log.
3. Only then use `--real`, starting with a small `--qty`.

The account this was built against currently shows **zero available funds** —
real orders will be rejected for margin until it is funded.
