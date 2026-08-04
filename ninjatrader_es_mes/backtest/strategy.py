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

    # Instrument economics
    tick_size: float = 0.25
    point_value: float = 50.0   # ES = 50, MES = 5

    # Cost model
    commission_per_side: float = 2.25   # round-turn ~$4.50 typical ES retail; per side here
    slippage_ticks: float = 1.0         # assumed slippage per fill, in ticks


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
