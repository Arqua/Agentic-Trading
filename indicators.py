"""
indicators.py — Pure technical-indicator functions.

Dependencies: numpy only (no pandas, no ta-lib).
"""

import numpy as np


def calculate_rsi(closes: np.ndarray, period: int = 14) -> float:
    """
    Wilder's Smoothed RSI.

    Parameters
    ----------
    closes : np.ndarray
        Array of closing prices, oldest first.
    period : int
        RSI look-back period (default 14).

    Returns
    -------
    float
        RSI value in [0, 100].  Returns 50.0 when there are fewer than
        period + 1 values (not enough data to compute a meaningful result).
    """
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return 50.0

    # Price deltas
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed the first smoothed average over the first `period` deltas
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    # Wilder's smoothing over remaining deltas
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_vwap_bands(bars: list) -> tuple:
    """
    Compute VWAP and +/- 2 standard-deviation bands from intraday bars.

    Parameters
    ----------
    bars : list[dict]
        Each bar dict must contain:
          'high_price', 'low_price', 'close_price', 'volume'
        Values may be strings (robin_stocks returns strings); they are cast
        to float internally.

    Returns
    -------
    tuple[float, float, float]
        (vwap, upper_band, lower_band)
        upper_band = vwap + 2 * std_dev
        lower_band = vwap - 2 * std_dev
        Returns (0.0, 0.0, 0.0) when bars is empty.
    """
    if not bars:
        return (0.0, 0.0, 0.0)

    cum_tpv = 0.0   # cumulative (typical_price * volume)
    cum_vol = 0.0   # cumulative volume
    cum_tp2v = 0.0  # cumulative (typical_price^2 * volume)

    for bar in bars:
        try:
            high  = float(bar["high_price"])
            low   = float(bar["low_price"])
            close = float(bar["close_price"])
            vol   = float(bar["volume"])
        except (KeyError, TypeError, ValueError):
            continue

        if vol <= 0:
            continue

        tp = (high + low + close) / 3.0
        cum_tpv  += tp * vol
        cum_tp2v += tp * tp * vol
        cum_vol  += vol

    if cum_vol == 0.0:
        return (0.0, 0.0, 0.0)

    vwap = cum_tpv / cum_vol

    # Volume-weighted variance: E[x^2] - (E[x])^2
    vw_variance = (cum_tp2v / cum_vol) - (vwap ** 2)
    # Guard against floating-point negatives
    vw_std = float(np.sqrt(max(vw_variance, 0.0)))

    upper_band = vwap + 2.0 * vw_std
    lower_band = vwap - 2.0 * vw_std

    return (vwap, upper_band, lower_band)


def consolidation_check(
    highs: list,
    lows: list,
    open_price: float,
    gap_pct: float,
) -> bool:
    """
    Determine whether price consolidated tightly without filling the opening gap.

    Conditions (both must be True):
      1. Tight range: (max(highs) - min(lows)) / open_price  < 0.015  (< 1.5%)
      2. Gap held:    min(lows) > open_price * (1 - gap_pct * 0.5)
                      i.e., price did not retrace more than half the gap

    Parameters
    ----------
    highs : list[float]
        Intraday bar highs during the consolidation window.
    lows : list[float]
        Intraday bar lows during the consolidation window.
    open_price : float
        The market open price of the symbol.
    gap_pct : float
        The gap size as a decimal fraction (e.g. 0.04 for a 4% gap).

    Returns
    -------
    bool
        True if both conditions are satisfied, False otherwise.
        Returns False if highs or lows are empty.
    """
    if not highs or not lows or open_price <= 0:
        return False

    bar_high = max(highs)
    bar_low  = min(lows)

    range_pct = (bar_high - bar_low) / open_price
    tight_range = range_pct < 0.015

    half_gap_retrace = open_price * (1.0 - gap_pct * 0.5)
    gap_held = bar_low > half_gap_retrace

    return tight_range and gap_held
