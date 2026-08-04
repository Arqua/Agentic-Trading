"""metrics.py — Performance statistics for a completed trade list."""

from __future__ import annotations

import math
from typing import List

from strategy import Trade


def summarize(trades: List[Trade], starting_equity: float = 50000.0) -> dict:
    # Only count "closing" legs for win/loss stats (SCALE_OUT legs are partial
    # realizations of a trade that isn't finished yet, but they DO count
    # toward total P&L, so total_pnl sums everything).
    closers = [t for t in trades if t.exit_reason in ("STOP", "TARGET",
                                                        "SESSION_FLATTEN", "END_OF_DATA")]
    scale_legs = [t for t in trades if t.exit_reason == "SCALE_OUT"]

    total_pnl = sum(t.pnl_usd for t in trades)
    n_round_trips = len(closers)
    wins = [t for t in closers if t.pnl_usd > 0]
    losses = [t for t in closers if t.pnl_usd <= 0]

    gross_win = sum(t.pnl_usd for t in wins) + sum(t.pnl_usd for t in scale_legs if t.pnl_usd > 0)
    gross_loss = sum(t.pnl_usd for t in losses) + sum(t.pnl_usd for t in scale_legs if t.pnl_usd < 0)

    win_rate = len(wins) / n_round_trips if n_round_trips else 0.0
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss != 0 else float("inf")
    avg_win = (sum(t.pnl_usd for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_usd for t in losses) / len(losses)) if losses else 0.0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if n_round_trips else 0.0

    # Equity curve & drawdown: walk every leg (scale-outs and closers) in
    # time order, since scale-out legs realize P&L before the trade closes.
    closers_sorted = sorted(closers, key=lambda t: t.exit_time)
    all_sorted = sorted(trades, key=lambda t: t.exit_time)
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = []
    for t in all_sorted:
        equity += t.pnl_usd
        peak = max(peak, equity)
        dd = peak - equity
        dd_pct = dd / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        curve.append((t.exit_time, equity))

    # Daily returns for a (very) rough Sharpe estimate
    daily_pnl: dict = {}
    for t in trades:
        daily_pnl.setdefault(t.day, 0.0)
        daily_pnl[t.day] += t.pnl_usd
    daily_returns = list(daily_pnl.values())
    sharpe = _sharpe(daily_returns)

    # Longest losing streak (by round-trip trades, in time order)
    streak = 0
    max_streak = 0
    for t in closers_sorted:
        if t.pnl_usd <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    r_multiples = [t.r_multiple for t in closers if t.exit_reason in ("STOP", "TARGET")]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

    return {
        "n_trades": n_round_trips,
        "n_scale_legs": len(scale_legs),
        "total_pnl_usd": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else None,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "expectancy_usd": round(expectancy, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct * 100, 2),
        "trading_days": len(daily_pnl),
        "daily_sharpe_annualized": round(sharpe, 3),
        "longest_losing_streak": max_streak,
        "avg_r_multiple": round(avg_r, 3),
        "ending_equity": round(equity, 2),
        "starting_equity": starting_equity,
        "total_return_pct": round((equity - starting_equity) / starting_equity * 100, 2),
        "equity_curve": curve,
        "daily_pnl": daily_pnl,
    }


def _sharpe(daily_returns: List[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((x - mean) ** 2 for x in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    # Annualize assuming ~252 trading days
    return (mean / std) * math.sqrt(252)
