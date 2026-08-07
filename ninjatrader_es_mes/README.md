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
│   ├── data.py                  ← fetches/caches ES=F / MES=F bars + RTH filter
│   ├── strategy.py               ← shared params, instrument/fee specs, EMA/ATR math
│   ├── engine.py                  ← bar-by-bar simulator (the fill model)
│   ├── metrics.py                  ← performance stats
│   ├── failure_analysis.py          ← systemic vs one-off classifier
│   ├── scalper.py                    ← high-frequency scalper (rejected, §0)
│   ├── ma_momentum.py                 ← MA momentum/direction system (rejected, §5)
│   ├── run_backtest.py                 ← orchestrates the ORB backtests
│   ├── run_momentum.py                  ← orchestrates the MA-momentum report
│   ├── data/                          ← cached OHLCV CSVs (git-ignored)
│   └── output/                         ← results.json, trade logs, equity charts
└── README.md                            ← this file
```

---

## 0. v2 reassessment — what was audited and changed

The v1 code was re-audited top to bottom. Findings and fixes:

**Defects found in v1 (all fixed):**

1. **Extended-hours leak.** Yahoo's futures feed includes the overnight
   Globex session — **71.8% of the 5-minute bars** fed to the v1 engine
   were extended-hours bars. Entries were time-gated to RTH, but the
   EMA(50)/ATR(14) filters were computed over the overnight stream
   (diverging from a NinjaTrader RTH chart), and on early-close holidays
   the 1-hour run held positions past the 13:00 close and exited them at
   the **18:00 ET Globex reopen** — 4 confirmed extended-hours exits
   (Black Friday 2024/2025, Memorial Day 2025, Juneteenth 2025). v2
   hard-filters all data to weekday 09:30–16:00 ET before anything touches
   it, adds a half-day guard that closes any surviving position on the
   short session's final bar, and the NinjaScript gained an explicit RTH
   time gate that refuses to act on any bar outside 09:30–16:00 ET even on
   a 24/7 chart template. Re-verified: **0 of 490 trade legs across all v2
   runs touch extended hours.**

2. **Fees undercounted by ~half, and wrong per instrument.** v1 charged
   commission only on exit legs — the entry side of every position was
   never charged — and used one flat $2.25/side for both symbols. v2
   charges the full per-instrument fee stack on every fill leg (entry,
   scale-out, exit); see §2.1.

**New behavior added in v2 (per requirements):**

3. **Dual-feed, MES-first execution.** The strategy now pulls both ES and
   MES data. Signals are always computed on the ES series (the deep,
   price-discovering contract; MES tracks it within a tick). Every entry
   executes on **MES while account buying power ≤ $20,000** and on ES only
   above that threshold (`BuyingPowerSwitchUsd`).

4. **Catastrophic sell-off at 5% of buying power.** The protective stop
   sits at **exactly the price where adverse movement equals 5% of current
   buying power** (`BpStopCapPct`) — the position sells off AT that loss
   (e.g. $12.50 on a $250 account), with fees landing on top rather than
   shrinking the stop. The ATR stop stands when it is already tighter.
   When the cap cannot give the chosen size enough stop room, **size is
   reduced contract by contract first**; only when even a 1-lot cannot get
   `min_stop_ticks` of room is the trade skipped and logged. (Earlier v2
   placed the stop one tick above the 5% point net of fees and used a hard
   10x-fee viability gate, which silently blocked sub-$400 accounts; the
   gate is now the tunable `min_r_fee_mult`, default 3x.)

**v3 (equity-growth pass) — audit finding and changes:**

5. **Metrics double-count bug (v1-v2), now fixed.** A closing trade's P&L
   already included its scale-out leg, and `metrics.py` ALSO summed the
   separate SCALE_OUT rows — inflating every scaling run's reported total
   (the 60-day DUAL run's "+$1,750" was actually **−$2,451** under the old
   touch-trigger entry). All statistics now come from closing legs only,
   verified against the engine's own equity accounting. Every number in
   this README is post-fix.

6. **Confirming-close entry (now the default).** The dominant systemic
   loss mode was always FADE_INTO_TREND — wick-touch breakouts that
   immediately reverse. Requiring the signal bar to CLOSE through the
   trigger (`confirm_close` / `ConfirmClose`) attacks it directly, and it
   shows on both samples: the 60-day 5-minute run flips from −$2,451 to
   **+$892** (win rate 39.7% → 49.1%) and the 2-year hourly run's
   drawdown falls by a third. Reward:risk default moved 2.0 → 1.5, the
   only setting positive on BOTH samples (picking the single best cell of
   the 60-day sweep would be curve-fitting).

7. **High-frequency scalping: investigated, rejected.** A trend-pullback
   scalper (`scalper.py`) was built and swept across 56 configurations
   (market and limit entries; targets 6-12 ticks, stops 12-32, time
   stops). Win rates up to **73%** are mechanically easy — and **every
   single configuration lost money** (best: −$5.2k over 60 days) once
   real fees and 1-tick slippage are charged, because the small-target/
   wide-stop geometry pays the full cost stack ~10 times a day while the
   rare loss erases many wins. Win rate is cosmetic; expectancy is not.
   A trend-day-gated variant (scalp pullbacks only after, and only in the
   direction of, a confirmed ORB breakout — modeling a live trader's
   long-only dip-buying on an up day) roughly halves the damage
   (−$1.5k to −$2.4k across 12 configs, win rates 51-64%) but never turns
   positive: at this geometry the breakeven win rate is ~70%, and the
   mechanical entries can't reach it. The module stays in the repo as
   documented negative research.

---

## 1. The strategy

**ORB + Trend + ATR** (`ORBTrendATRStrategy.cs`): trade the breakout of the
first 15 minutes of the RTH session (09:30–09:45 ET), filtered so it only
takes breakouts that agree with the intraday trend and only on days with
tradeable volatility.

| Rule | Detail |
|---|---|
| Session | **RTH only** (09:30–16:00 ET), enforced by an explicit time gate |
| Data | Both ES and MES load; signals from ES, execution routed by buying power |
| Execution routing | **MES while buying power ≤ $20,000**, ES above |
| Opening range | High/low of the first `OrbMinutes` (default 15) minutes, ES series |
| Trend filter | EMA(50) on ES 5-min — longs only above it, shorts only below it |
| Volatility filter | ATR(14) within `[MinAtrTicks, MaxAtrTicks]` |
| Entry | Bar must **close** through `orb_high + buffer` / `orb_low − buffer` (`ConfirmClose`, default on); one attempt per side per day; cutoff 11:30 ET |
| Initial stop | `max(1.5×ATR, 0.5×ORB range)`, then **capped so the position sells off at exactly 5% of buying power** (size reduces first when the cap can't fit the quantity) |
| Target / trail | 1.5R fixed target; at +1R, 50% scales out, stop → breakeven, remainder trails 1.25×ATR |
| Session control | No entries after 11:30 ET; flatten at 15:55 ET — no overnight exposure |
| Position sizing | `floor(RiskPerTradeUsd / (stop_pts × PointValue + round_turn_fee))`, capped at `MaxContracts` |
| Fee guards | Sizing is net of round-turn fees; daily loss limit is fee-inclusive; trades whose 1R gross < 3× round-turn fee (`min_r_fee_mult`) are skipped |
| Guardrails | Daily loss limit; max 2 trades/day; requires both ES and MES series or refuses to run |

### 1.1 Installing it in NinjaTrader 8

1. Copy `ORBTrendATRStrategy.cs` into
   `Documents\NinjaTrader 8\bin\Custom\Strategies` and compile via the
   NinjaScript Editor (F5).
2. Attach to a 5-minute **ES or MES front-month chart** with an RTH session
   template ("US Index Futures RTH") — the sibling contract (`MES 12-25`
   for an `ES 12-25` chart, and vice versa) is added automatically.
3. Backtest in Strategy Analyzer with real tick data before any live/sim
   use. In the Analyzer the account reports no meaningful buying power, so
   routing uses `FallbackBuyingPowerUsd` (default $10,000) plus realized
   strategy P&L — meaning the Analyzer will also exercise the MES→ES
   switchover if simulated equity crosses $20,000.

Written to NinjaTrader 8 conventions but **not compiled against actual
NinjaTrader assemblies here** (none available in this environment) — let
the NinjaScript compiler have the final word on any API drift.

---

## 2. Backtest methodology — and its limits

NinjaTrader's own backtester needs a tick-level feed not reachable from
this environment, so `backtest/` re-implements the exact rules in Python
(`engine.py` documents the fill model precisely: signal on bar close →
entry at next bar's open ± 1 tick slippage; stops/targets fill intrabar
off high/low, stop wins ties) and runs them against real ES=F / MES=F
history from Yahoo Finance, **hard-filtered to RTH**.

### 2.1 Fee model (per contract, per side, all-in)

| Component | ES | MES |
|---|---|---|
| Broker commission (NinjaTrader free plan) | $1.29 | $0.35 |
| CME exchange & clearing | $1.40 | $0.37 |
| NFA regulatory | $0.02 | $0.02 |
| **Total per side** | **$2.71** | **$0.74** |
| **Round turn** | **$5.42** | **$1.48** |

These are defaults in `strategy.py` (`ES_SPEC` / `MES_SPEC`) and NinjaScript
parameters (`EsFeePerSideUsd` / `MesFeePerSideUsd`) — substitute your
broker's actual schedule. Fees are charged on every fill leg and folded
into sizing, the 5% stop cap, and the daily loss limit. Slippage: 1 tick
per fill ($12.50/contract ES, $1.25 MES).

### 2.2 Known limitations

- Yahoo caps 5-minute history at 60 days; the 5-min runs are a
  ~45-trading-day sample — real data, but statistically thin.
- The 1-hour/730-day run exists for regime coverage; hourly bars are
  stamped on the hour, so its opening range is the 10:00 bar and its
  flatten is the 15:00 bar (which closes at the 16:00 session close). It
  is a coarser cousin of the 5-minute strategy, not the same instance.
- Continuous front-month series splice contract rolls.
- Same-bar stop+target ambiguity resolves to the stop (conservative).
- MES fills are priced off MES's own bars (timestamp-aligned to the ES
  signal series, ES fallback on feed gaps).

**Bottom line:** this validates rule logic and surfaces failure patterns;
it is not a substitute for Strategy Analyzer on tick data plus sim
forward-testing.

### 2.3 Reproducing

```bash
cd ninjatrader_es_mes/backtest
pip install -r requirements.txt
python run_backtest.py --refresh   # omit --refresh to reuse cached CSVs
```

---

## 3. Backtest results (v2)

Starting equity **$10,000** — deliberately below the $20k switch threshold
so the MES-first rule is exercised. All entries/exits verified inside
09:30–16:00 ET.

All figures below use the corrected (closers-only) accounting and the v3
defaults (confirming close, 1.5R):

| Run | Execution | Trades | Win rate | Profit factor | Net P&L | Return | Max DD | Longest losing streak |
|---|---|---|---|---|---|---|---|---|
| `DUAL_5m_60d` | **all 55 on MES** (equity never crossed $20k) | 55 | 49.1% | 1.089 | **+$892** | +8.9% | 31.2% | 5 |
| `ES_5m_60d_ref` | pinned to ES (rule ignored) | 55 | 29.1% | 0.477 | **−$6,534** | −65.3% | 70.8% | 11 |
| `DUAL_1h_730d` | all 231 on MES | 231 | 53.7% | 1.017 | **+$416** | +4.2% | 25.3% | 6 |

Full stats in `backtest/output/results.json`; equity curves in
`equity_*.png`; per-trade logs in `trades_*.csv`.

**The reference run is the point.** Identical signals, identical days: MES
execution on a $10k account returns +10.5%, ES execution destroys 70% of
the account. Two compounding mechanisms cause this, and both are exactly
what the routing rule and stop cap are designed to prevent: (1) an ES
point is $50, so the same stop distance risks 10× more per contract, and
(2) as equity shrinks, the 5% cap forces ever-tighter stops on ES until
routine noise clips them (win rate collapses from 39.7% → 23.8%). MES's
smaller unit lets the cap breathe. **The $20k threshold is not cosmetic —
below it, ES sizing genuinely cannot fit inside a sane risk envelope.**

Note the MES→ES switchover never fired: neither dual run's equity crossed
$20,000 in-sample. The routing logic is exercised every trade (it
evaluates and picks MES); the ES branch is exercised by the reference run.

### 3.1 Small-account behavior (the 5% sell-off cap in action)

`python run_backtest.py --equity 250` runs the same DUAL 5-min backtest
from a $250 balance:

| Balance | Trades | Skipped | Outcome |
|---|---|---|---|
| $250 | 19 | 41 | **−$157 (−63%), win rate 15.8%, frozen at $93.26** |

With the sell-off cap at exactly 5% of buying power, a $250 account can
trade — 1 MES contract with a 2.5-point stop — but that stop is a quarter
of the ~10-point ATR stop the entries were designed around, so routine
noise clips it (3 wins in 19 trades). Each loss shrinks buying power and
therefore the next stop, until equity reaches ~$93, where 5% can no
longer buy even a 1-point stop and the guards freeze the account (all 46
remaining signals skipped). This is the same death-by-tight-stops
mechanism as the ES reference run, in miniature — the cap contains each
individual loss exactly as specified, but it cannot manufacture edge at a
size where the designed stop doesn't fit. Practical floor for this
strategy remains roughly $8k–$10k.

---

## 4. Failure point analysis: systemic vs. one-off

Method unchanged from v1 (`failure_analysis.py`): each losing round-trip is
tagged by probable cause; a tag is **systemic** when it recurs 3+ times
within a 5-day window or repeatedly across unrelated weeks, **one-off**
when isolated. v2 numbers:

### 4.1 Systemic: fake breakouts that immediately reverse (`FADE_INTO_TREND`)

**60.7% of 5-min losses (17 of 28, −$6,416); 64.1% on the ES reference
run — even after the confirming-close entry (v3) removed the worst
wick-touch cases.** The breakout close fires, the next bars give it back. Clusters
repeat across unrelated weeks — still the structural weak point, though
the confirming-close entry (v3, adopted) already cut both its frequency
and the overall drawdown. Remaining fix candidates: widen the buffer on
low-ATR days, or a cooldown before re-arming a stopped-out side.

### 4.2 Systemic: marginal-conviction entries stopped in chop (`CHOP_STOPPED`)

**25.0% of 5-min losses (7, −$1,962).** Bottom-quartile-ATR entries that
died same-session, clustering mid-June and mid-July. The static 12-tick
ATR floor is too low; make it adaptive (percentile of trailing 20-day ATR).

### 4.3 Mostly one-off: high-ATR stop-outs (`WIDE_RANGE_STOP`)

**14.3% of 5-min losses (4, −$1,665)**, concentrated in the one genuinely
volatile stretch (June 8–12) plus scattered singles; on the 1-hour run it
is 2 losses / 1.2%. Verdict unchanged: predominantly event-driven
one-offs. An explicit macro-calendar blackout (FOMC/CPI/NFP) would address
these without tightening the ATR ceiling on good trend days.

### 4.4 The 1-hour variant: systemic weakness transformed, not cured

v1's headline failure was the 1-hour run's unrecovered 54% drawdown under
ES economics. v2's 1-hour run (MES execution, fee-aware sizing, 5% cap)
ends +35.1% — but look closer before celebrating: **75% of its losses
(120, −$19,343) are tagged `OTHER`**, i.e. positions that neither hit
their stop nor their target but bled out at the session flatten, and its
average R-multiple is −0.72. The hourly timeframe still doesn't give the
2R target room to resolve inside one session — a structural mismatch
(SYSTEMIC), consistent with v1's conclusion. The micro sizing and stop cap
now contain the damage per trade, which is why the equity curve survives;
they don't fix the underlying timeframe problem. **Recommendation stands:
don't deploy the hourly configuration; the 5-minute one is the strategy.**

### 4.5 One-off examples (for contrast)

- `2026-05-27` `FADE_INTO_TREND` — the only isolated instance of its tag in
  the 5-min run; every other one belongs to a multi-day cluster.
- `2024-08-06` `WIDE_RANGE_STOP` (1-hour run) — no repeats within weeks on
  either side; consistent with an isolated volatility event.

These are the cost of doing business with stops — not rule weaknesses to
chase with more parameters.

---

---

## 5. Alternative tested: MA momentum / direction, every 5 minutes

`ma_momentum.py` + `run_momentum.py` implement and evaluate a different
premise from the ORB strategy: instead of one setup per day, **re-evaluate
on every 5-minute bar** and hold the position implied by the moving
average's direction and the market's momentum.

    slope = MA(t) - MA(t - slope_lookback)      → direction
    roc   = close(t) - close(t - roc_lookback)  → momentum
    want  = +1 if slope > +thresh and roc > 0
            -1 if slope < -thresh and roc < 0
             0 otherwise (flat / chop zone)

Two readings of "trade every 5 minutes" are both tested: `churn` closes and
re-opens every single bar; `flip` holds while the signal is unchanged and
trades only on a change. Same conservative fill model as the ORB engine
(signal on close → fill at next bar's open ± 1 tick, full fee stack per
leg, RTH only, flat by 15:55).

### 5.1 Results — 5-minute MES, 44 sessions, 1 contract, $10k

| Config | Trades | /day | Win rate | PF | **Net** | Gross (no costs) | Costs | Max DD |
|---|---|---|---|---|---|---|---|---|
| `churn_every_bar` | 2,270 | 46.3 | 42.4% | 0.69 | **−$8,643** | +$391 | $9,035 | 93.6% |
| `flip_ema20` | 593 | 12.1 | 36.6% | 0.84 | **−$1,969** | +$391 | $2,360 | 40.5% |
| `flip_ema9_fast` | 658 | 13.4 | 27.8% | 0.87 | **−$1,863** | +$756 | $2,619 | 41.4% |
| `flip_sma50_slow` | 152 | 4.2 | 44.1% | 1.12 | **+$348** | +$953 | $605 | 8.4% |

### 5.2 Why it fails — the cost decomposition is the whole story

The gross (zero-fee, zero-slippage) edge of this signal is **roughly
constant at $390–950 regardless of how often it trades** — trading more
frequently just slices the same underlying move into more pieces. Costs,
however, scale linearly at a measured **$3.98 per round trip** ($1.48 fees
+ 2 ticks of slippage). That single asymmetry decides everything:

- at 4.2 trades/day → $605 of costs against $953 gross → **+$348**
- at 12.1 trades/day → $2,360 of costs against $391 gross → **−$1,969**
- at 46.3 trades/day → $9,035 of costs against $391 gross → **−$8,643**

The literal every-bar version's equity curve
(`equity_MOMO_churn_every_bar.png`) is a near-straight line from $10,000 to
~$1,300 — a textbook cost bleed, not a market-direction failure. **The
frequency itself is the losing decision.**

### 5.3 The one profitable config does not survive validation

`flip_sma50_slow` (+$348) is the only meaningfully positive cell out of
**144 swept configurations** — about what pure chance produces at that
sample size. Three checks confirm it is noise:

1. **Its neighbors are losers.** Changing the slope lookback 1→2 gives
   −$1,306; 1→3 gives −$1,620; MA 50→40 gives −$1,488; SMA→EMA gives
   −$1,234. A real edge does not sit on an isolated island.
2. **Walk-forward is a coin flip.** Optimizing on the first half and
   testing on the second: the best in-sample config returns **+$77** out
   of sample, and configs ranked 3–5 were *negative* in-sample yet
   positive out-of-sample — the ordering carries no information. Mean
   out-of-sample P&L of the top five: **+$92** over five weeks, roughly
   $1.16/trade against a $3.98/trade cost.
3. **It inverts on the long sample.** Run on ~2 years of hourly bars, the
   same rules lose **−$4,301** (PF 0.82, 64% drawdown). Every one of the
   four configs is negative there, the fast one by −$11,424.

### 5.4 Verdict

**Rejected.** Not deployed, no NinjaScript port. The MA-momentum signal has
a small real gross edge, but it is an order of magnitude too small to pay
for the frequency it implies — which is the same arithmetic that killed the
`scalper.py` experiment (§0, item 7), arrived at from a completely different
direction. Reproduce with `python run_momentum.py`; the module stays in the
repo as documented negative research.

For contrast, on the identical 44 sessions and account, the ORB strategy
turned **55 trades into +$892** — 3% of the churn variant's trade count and
a $9,500 swing in outcome. When trade frequency is a free parameter, the
evidence here says push it down, not up.

## 6. Recommendations before any live/sim deployment

1. **Re-validate in NinjaTrader's Strategy Analyzer** on several years of
   tick data; then forward-test on sim for weeks (the systemic patterns
   recur on a multi-week cadence — a few days proves nothing).
2. **Fix the two systemic 5-minute failure modes** (confirming-close
   requirement; adaptive ATR floor) — both are small mechanical edits.
3. **Keep the MES-first rule.** The reference run is the evidence: ES on a
   sub-$20k account is a 70% drawdown machine on the exact same signals.
4. **Don't deploy the hourly configuration** (see §4.4).
5. **Add a macro-event blackout** (FOMC/CPI/NFP) for the residual
   high-ATR one-offs.

This is a research/engineering exercise, not investment advice. Futures
trading carries substantial risk of loss; past and simulated performance
does not guarantee future results.
