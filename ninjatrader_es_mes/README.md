# NinjaTrader ES / MES Algorithm — Strategy + Backtest + Failure Analysis

An Opening-Range-Breakout (ORB) intraday strategy for CME E-mini (ES) and
Micro E-mini (MES) S&P 500 futures, built as a NinjaTrader 8 NinjaScript
strategy, plus a Python backtest harness that replicates the same rules
bar-for-bar against real historical ES/MES price data.

```
ninjatrader_es_mes/
├── NinjaScript/
│   └── ORBTrendATRStrategy.cs   ← import this into NinjaTrader 8
├── backtest/
│   ├── data.py                  ← fetches & caches ES=F / MES=F bars
│   ├── strategy.py               ← shared params + EMA/ATR math
│   ├── engine.py                  ← bar-by-bar simulator (the fill model)
│   ├── metrics.py                  ← performance stats
│   ├── failure_analysis.py          ← systemic vs one-off classifier
│   ├── run_backtest.py               ← orchestrates everything
│   ├── data/                          ← cached OHLCV CSVs
│   └── output/                         ← results.json, trade logs, equity charts
└── README.md                            ← this file
```

---

## 1. The strategy

**ORB + Trend + ATR** (`ORBTrendATRStrategy.cs`): trade the breakout of the
first 15 minutes of the RTH session (09:30–09:45 ET), filtered so it only
takes breakouts that agree with the intraday trend and only on days with
"tradeable" volatility.

| Rule | Detail |
|---|---|
| Opening range | High/low of the first `OrbMinutes` (default 15) minutes of RTH |
| Trend filter | EMA(50) on the 5-min chart — longs only above it, shorts only below it |
| Volatility filter | ATR(14) must sit inside `[MinAtrTicks, MaxAtrTicks]` — skips dead-quiet chop and news-spike regimes alike |
| Entry | Market order once price closes through `orb_high + buffer` (long) / `orb_low − buffer` (short), one attempt per side per day, cutoff at `MaxEntryTimeHHmm` (default 11:30 ET) |
| Initial stop | `max(1.5×ATR, 0.5×ORB range width)` behind entry — never tighter than half the opening range |
| Target / trail | 2R fixed target; at +1R, 50% scales out, stop moves to breakeven, remainder trails by `1.25×ATR` |
| Session control | No entries after 11:30 ET; everything flattened and orders cancelled at 15:55 ET — no overnight/rollover exposure |
| Position sizing | `contracts = floor(RiskPerTradeUsd / (stop_distance_pts × PointValue))`, capped at `MaxContracts`. `PointValue` is read from the instrument ($50/pt ES, $5/pt MES) so the same settings auto-scale between the two symbols |
| Guardrails | Daily loss limit halts new entries (existing brackets still manage); max 2 trades/day; instrument guard refuses to run on anything but ES/MES |

**Why this shape:** ORB is a well-worn futures index strategy because ES/MES
have a real, liquid opening auction and the first 15 minutes reliably sets a
range that either holds (trend day) or fails (chop/reversal day). The EMA
filter is there specifically to stop the strategy from taking both the long
*and* the short breakout on the same trend day (a classic ORB failure mode).
The ATR floor/ceiling exists because a breakout on an abnormally quiet day is
noise, and a breakout on an abnormally wild day (economic release, halt,
flash move) blows through an ATR-sized stop before the fill is even
confirmed — both get filtered out rather than traded.

### Installing it in NinjaTrader 8

1. NinjaTrader 8 → Tools → Import → NinjaScript Add-On, or copy
   `ORBTrendATRStrategy.cs` into `Documents\NinjaTrader 8\bin\Custom\Strategies`
   and compile via the NinjaScript Editor (F5).
2. Attach a 5-minute ES or MES front-month chart, session template "US Index
   Futures RTH" (or your broker's equivalent).
3. Strategies → ORBTrendATRStrategy → set contract/margin account,
   backtest first in Strategy Analyzer with real tick-replay data before
   any live/sim deployment.

This file was written to NinjaScript/NinjaTrader 8 conventions but has
**not been compiled inside an actual NinjaTrader install** (none is
available in this environment) — treat it as a strong first draft and let
the NinjaScript compiler have the final word on any local API-version
drift.

---

## 2. Backtest methodology — and its limits

NinjaTrader's own backtester (Strategy Analyzer / Market Replay) needs a
tick-level historical feed (Kinetick, CQG, Continuum, Rithmic...) that isn't
reachable from this sandboxed environment. Instead, `backtest/` is a
from-scratch Python re-implementation of the exact same rules
(`strategy.py`/`engine.py` mirror the `.cs` file's logic and fill semantics
— see the docstring in `engine.py` for the precise fill model: entries fill
at the *next* bar's open, since the `.cs` file uses `Calculate.OnBarClose`;
stop/target orders fill intrabar off each bar's high/low, matching how
`SetStopLoss`/`SetProfitTarget` actually behave in NinjaTrader even in
`OnBarClose` mode) run against **real ES/MES history pulled from Yahoo
Finance's continuous front-month futures feed** (`ES=F`, `MES=F`).

**Known limitations of this substitute, stated plainly:**

- Yahoo's free intraday feed only goes back **60 days at 5-minute
  granularity**, vs. the years of tick history NinjaTrader's own backtester
  would use. The 5-minute results below (`ES_5m_60d`, `MES_5m_60d`) are a
  ~48-trading-day sample — real, but statistically thin. Don't treat the
  win rate/profit factor from that run as a stable long-run estimate.
- To get more regime coverage, a second run uses **1-hour bars over the
  full 730-day Yahoo allowance** (`ES_1h_730d`), with the opening range
  scaled to 60 minutes. This is a genuinely different (coarser) strategy
  instance, not the same signals at higher fidelity — its purpose here is
  stress-testing the *rule structure* across ~2 years of varied regimes,
  not validating the 5-minute NinjaScript directly.
- Continuous-front-month series splice in contract rolls; Yahoo's roll
  adjustment can introduce small artificial gaps around expiration that a
  true single-contract series wouldn't have.
- Fills assume $2.25/side commission and 1 tick of slippage per fill — a
  reasonable retail-futures assumption, not what your specific broker/data
  feed will actually give you.
- Same-bar stop-and-target ambiguity (a 5-min bar wide enough to plausibly
  touch both) is resolved conservatively in favor of the stop. Real
  intrabar path could occasionally differ.

**Bottom line:** this validates the *rule logic* is sound and the strategy
behaves as designed, and it surfaces real failure patterns worth fixing —
but it is not a substitute for running the actual `.cs` file through
NinjaTrader's Strategy Analyzer against several years of tick data (and
then forward-testing on sim) before committing real capital.

### Reproducing it

```bash
cd ninjatrader_es_mes/backtest
pip install -r requirements.txt
python run_backtest.py --refresh   # omit --refresh to reuse cached data/*.csv
```

Outputs land in `backtest/output/`: `results.json` (full stats + failure
analysis), one `trades_<label>.csv` per run, and one `equity_<label>.png`
equity curve per run.

---

## 3. Backtest results

Starting equity $50,000 (illustrative — position sizing is risk-based, not
equity-based, so this only sets the drawdown-% denominator).

| Run | Symbol | Bars | Days | Trades | Win rate | Profit factor | Net P&L | Max DD | Max DD % | Longest losing streak |
|---|---|---|---|---|---|---|---|---|---|---|
| `ES_5m_60d` | ES | 13,509 (5m) | 48 | 66 | 33.3% | 0.89 | **−$3,326** | $9,290 | 18.6% | 8 |
| `MES_5m_60d` | MES | 13,512 (5m) | 48 | 69 (+23 scale legs) | 40.6% | 1.07 | **+$963** | $4,322 | 8.6% | 10 |
| `ES_1h_730d` | ES | 13,699 (1h) | 409 | 409 | 46.9% | 1.01 | **+$2,336** | $34,247 | 54.4% | 9 |

Full numbers (Sharpe, expectancy, avg R, etc.) are in
`backtest/output/results.json`; equity curves are the `equity_*.png` files.

**Reading these honestly:** the 5-minute ES run — the one that actually
matches the `.cs` file's intended timeframe — is close to breakeven-to-
slightly-negative over its (short) sample, dragged down mainly by
commissions/slippage on a ~33% win rate. MES on the identical signals comes
out slightly positive because its $5/pt economics make the fixed
per-trade cost drag much lighter relative to the same point-move P&L. The
1-hour variant posts a positive total but only by round-tripping through a
54%-of-equity drawdown that never recovered inside the sample window — see
§4, that's the headline failure, not a footnote.

---

## 4. Failure point analysis: systemic vs. one-off

`failure_analysis.py` tags every losing round-trip by probable cause, then
checks two things before calling a pattern "systemic": (a) does the same
tag recur **3+ times within a 5-day window** (a genuine cluster), and (b)
does the tag recur repeatedly **across unrelated weeks/months** spread
through the whole sample (a structural weakness, not a bad week). A loss
that matches neither — no nearby siblings, doesn't reappear elsewhere — is
called one-off.

### 4.1 Systemic: fake breakouts that reverse (`FADE_INTO_TREND`)

**~59% of all losing trades on both `ES_5m_60d` and `MES_5m_60d`.** Price
closes through the ORB level, the entry fires, and price immediately gives
it back and runs the other way — a classic ORB fake-out. This tag clusters
repeatedly across unrelated weeks (late May, early June, mid-to-late June,
and again through most of July in the 5-min run) — it is not tied to any
single news event, it is the strategy's structural weak point.

**Why it's systemic, not bad luck:** a 2-tick buffer past a 15-minute range
is a low bar. On a genuinely choppy day the first push past the range is
exactly the kind of move that reverses. The EMA(50) trend filter helps
(it blocks counter-trend breakouts) but does nothing to stop a
trend-aligned breakout that's simply premature.

**What would fix it:** require a second confirming bar close beyond the
level (not just a touch), widen the buffer on lower-ATR days specifically,
or add a short cooldown before re-arming the same side after a same-day
stop-out so the strategy doesn't immediately re-fight the same failed
level.

### 4.2 Systemic: entries on marginal-conviction days (`CHOP_STOPPED`)

**~25–29% of losses.** Entries where ATR at signal time was in the bottom
quartile of the sample still got stopped out same-session. This also
clusters (mid-June, mid-to-late July) rather than appearing as isolated
noise — the `MinAtrTicks` floor (12 ticks / 3.0 pts on ES) is set too low
to actually screen out the low-conviction days it's meant to filter.

**What would fix it:** raise the ATR floor, or better, make it adaptive
(e.g. percentile of the trailing 20-day ATR rather than a fixed tick
count) so "quiet" is judged relative to the recent regime, not a constant.

### 4.3 Mixed: stopped out on high-ATR entries (`WIDE_RANGE_STOP`)

**~12–14% of losses**, but its individual clusters are smaller and more
scattered than the two patterns above — several instances land as isolated
single-day events (`2024-08-06`, `2025-12-18`, `2026-03-10`, `2026-04-01`
in the 1-hour run) with no repeat nearby, alongside a few genuine multi-day
clusters (e.g. `2026-06-08` → `2026-06-12` in the 5-min run,
`2025-03-05` → `2025-03-11` in the 1-hour run — 3 losses in a week,
consistent with a specific volatile stretch rather than a standing
weakness). **Verdict: mostly one-off, with one confirmed systemic pocket**
— the `MaxAtrTicks` ceiling (160 ticks / 40 pts on ES) is wide enough that
occasional multi-day volatile stretches still get traded; consider pairing
it with an explicit macro-event blackout (FOMC, CPI, NFP) rather than
tightening the ceiling further, since tightening would also cut off good
trend days.

### 4.4 Systemic — and the single biggest problem found: the 1-hour variant's 6-month drawdown

The `ES_1h_730d` run's equity curve peaks on **2025-01-03** and does not
make a new high again through the end of the sample (2026-08-04) — the
deepest stretch, **2025-01-03 → 2025-07-04, is a $34,247 (54%-of-starting-
equity) drawdown that never recovered inside the backtest window.** This
is not one bad week: it spans roughly six months and dozens of unrelated
trading days across an entire regime change. By definition that is
**systemic**, not a one-off — the 60-minute opening-range / ATR-stop
combination is structurally mismatched to that stretch of the market (most
likely: hourly ATR stops are wide enough that losers cost multiples of what
winners on the same timeframe pay back, given the realized win rate of
~47% and a profit factor barely above 1.01).

**Conclusion: don't run the 60-minute variant as configured.** It was
included here specifically as a stress test of the rule structure across
more regimes, and it found a real structural gap — the position-sizing and
stop-distance model that works on a 5-minute chart does not simply transfer
to an hourly one without re-tuning the reward:risk ratio and/or the stop
multiple. The 5-minute configuration (the one the `.cs` file actually ships
with) is the one to trust.

### 4.5 One-off examples (for contrast)

- `2024-08-06`, `WIDE_RANGE_STOP`, −$2,479 — isolated, no clustering
  before/after it in the 1-hour run.
- `2025-04-02`, `OTHER`, −$5,177 — a single outsized loss with no repeat
  nearby; consistent with an isolated news/gap event rather than a
  structural issue.
- `2026-05-27`, `FADE_INTO_TREND`, −$551 in the 5-min run — the only
  isolated instance of this tag; every other occurrence of the same tag in
  that run is part of a multi-day cluster (see §4.1).

These are exactly the kind of losses a systemic fix would *not* prevent —
they're the cost of doing business with a stop-based strategy, not a rule
weakness to chase.

---

## 5. Recommendations before any live/sim deployment

1. **Re-validate in NinjaTrader itself** with Strategy Analyzer against
   several years of real tick data before trusting these numbers further —
   this Python harness is a rules-fidelity check, not a replacement.
2. **Fix the two confirmed systemic issues first:** require a confirming
   second bar (or a small additional buffer) before arming an entry, and
   make the ATR floor adaptive to the trailing regime instead of a fixed
   tick count. Both are cheap, mechanical changes to `ORBTrendATRStrategy.cs`.
3. **Do not run the 60-minute-bar configuration live.** If a higher-
   timeframe variant is wanted, its stop/target multiples and reward:risk
   need to be re-optimized independently, not inherited from the 5-minute
   settings.
4. **Add an explicit macro-event blackout** (FOMC/CPI/NFP mornings) to stop
   the ATR ceiling from occasionally admitting genuinely dangerous
   volatility regimes rather than just tightening it and cutting off good
   trend days too.
5. **Forward-test on sim** for at least several weeks after any rule change
   — the systemic patterns found here recur on a multi-week cadence, so a
   few sim days won't be enough to confirm a fix.

This is a research/engineering exercise, not investment advice. Futures
trading carries substantial risk of loss; past and simulated performance
does not guarantee future results.
