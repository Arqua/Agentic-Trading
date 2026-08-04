"""
strategy.py — Python reimplementation of ORBTrendATRStrategy.cs.

This mirrors the NinjaScript rules bar-for-bar so the Python backtest is a
faithful proxy for what the .cs strategy would do inside NinjaTrader:

  1. Build the opening range over the first `orb_minutes` of RTH (09:30 ET).
  2. Freeze it; arm one long attempt and one short attempt for the day.
  3. Trend filter: EMA(trend_period). Longs only above EMA, shorts only below.
  4. Volatility filter: ATR(atr_period) must be within [min_atr_ticks, max_atr_ticks].
  5. Entry: stop-trigger at orb_high + buffer (long) / orb_low - buffer (short),
     live until max_entry_time, one shot per side per day.
  6. Initial stop = max(atr_stop_mult * ATR, 0.5 * ORB range width), floored.
  7. Target = stop_distance * reward_risk_ratio.
  8. At +1R, scale out scale_out_pct of size, move stop to breakeven, trail
     the remainder by atr_trail_mult * ATR.
  9. Flatten everything at flatten_time. No new entries after max_entry_time.
 10. Daily loss limit and max-trades-per-day halt new entries (existing
     bracket orders still manage to their stop/target).

Position sizing: contracts = floor(risk_per_trade_usd / (stop_distance_pts *
point_value)), capped at max_contracts. point_value is $50 for ES, $5 for MES.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class InstrumentSpec:
    """
    Per-contract economics, including the full per-order fee stack.

    fee_per_side is the all-in cost of one fill on one contract:
      broker commission + CME exchange & clearing fee + NFA regulatory fee.
    Defaults use NinjaTrader brokerage's free-license plan plus published
    CME/NFA fees (2025 schedule):
        ES : $1.29 commission + $1.40 exchange/clearing + $0.02 NFA = $2.71/side
        MES: $0.35 commission + $0.37 exchange/clearing + $0.02 NFA = $0.74/side
    i.e. round-turn ≈ $5.42 (ES) / $1.48 (MES). Swap in your broker's actual
    schedule via these fields.
    """
    name: str
    point_value: float
    fee_per_side: float
    tick_size: float = 0.25


ES_SPEC = InstrumentSpec("ES", 50.0, 2.71)
MES_SPEC = InstrumentSpec("MES", 5.0, 0.74)


@dataclass
class Params:
    orb_minutes: int = 15
    buffer_ticks: int = 2
    trend_period: int = 50
    atr_period: int = 14
    min_atr_ticks: int = 12
    max_atr_ticks: int = 160
    atr_stop_mult: float = 1.5
    atr_trail_mult: float = 1.25
    reward_risk_ratio: float = 2.0
    scale_out_pct: float = 50.0
    risk_per_trade_usd: float = 500.0
    max_contracts: int = 5
    daily_loss_limit_usd: float = 1200.0
    max_trades_per_day: int = 2
    max_entry_time: dtime = dtime(11, 30)
    flatten_time: dtime = dtime(15, 55)
    session_open: dtime = dtime(9, 30)

    # Account, routing and catastrophic-stop rules
    starting_equity_usd: float = 10_000.0
    bp_switch_threshold_usd: float = 20_000.0  # trade MES at/below this buying power, ES above
    bp_stop_cap_pct: float = 0.05              # stop placed just above the price where a
                                                # full stop-out would consume 5% of buying power
    min_stop_ticks: int = 4                    # if the 5% cap can't leave even this much
                                                # stop room, skip the trade entirely

    # Signal-series tick size (ES and MES both tick in 0.25)
    tick_size: float = 0.25

    # Cost model — per-instrument fees live in InstrumentSpec.fee_per_side;
    # slippage is charged per fill in ticks (converted to USD via the
    # executing instrument's point value).
    slippage_ticks: float = 1.0


@dataclass
class Trade:
    symbol: str
    side: str                 # LONG / SHORT
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    qty: int = 1
    exit_reason: str = ""
    stop_price: float = 0.0
    target_price: float = 0.0
    r_multiple: float = 0.0
    pnl_usd: float = 0.0
    scaled: bool = False
    day: str = ""
    entry_atr_ticks: float = 0.0
    fills: List[dict] = field(default_factory=list)  # partial exits


def ema_series(closes: List[float], period: int) -> List[Optional[float]]:
    """Simple EMA, seeded with an SMA of the first `period` values."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    sma = sum(closes[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr_series(highs, lows, closes, period: int) -> List[Optional[float]]:
    """Wilder ATR, seeded with a simple average of the first `period` true ranges."""
    n = len(closes)
    trs: List[float] = [0.0] * n
    for i in range(n):
        if i == 0:
            trs[i] = highs[i] - lows[i]
        else:
            trs[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    avg = sum(trs[1:period + 1]) / period if n > period else sum(trs[:period]) / period
    idx = period
    out[idx] = avg
    prev = avg
    for i in range(idx + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out
