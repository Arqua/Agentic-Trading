"""
run_backtest.py — Orchestrates data fetch, backtest run, metrics and
failure analysis for ES and MES, and writes a report + equity-curve charts
to backtest/output/.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import time as dtime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from data import get_bars
from engine import Backtest
from strategy import Params
from metrics import summarize
from failure_analysis import analyze, drawdown_episodes

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def run_one(label: str, symbol: str, rng: str, interval: str, point_value: float,
            tick_size: float, refresh: bool = False, param_overrides: dict = None) -> dict:
    bars = get_bars(symbol, rng, interval, refresh=refresh)
    params = Params(point_value=point_value, tick_size=tick_size, **(param_overrides or {}))
    bt = Backtest(symbol=symbol, bars=bars, params=params)
    trades = bt.run()
    stats = summarize(trades)
    fail = analyze(trades)
    dd_episodes = drawdown_episodes(stats["equity_curve"])

    os.makedirs(OUT_DIR, exist_ok=True)
    _plot_equity(label, stats["equity_curve"], stats["starting_equity"])
    _write_trade_log(label, trades)

    return {
        "label": label, "symbol": symbol, "interval": interval,
        "n_bars": len(bars), "params": params.__dict__,
        "stats": {k: v for k, v in stats.items() if k not in ("equity_curve", "daily_pnl")},
        "failure_analysis": fail,
        "drawdown_episodes": dd_episodes,
    }


def _plot_equity(label, curve, starting_equity):
    if not curve:
        return
    xs = [c[0] for c in curve]
    ys = [c[1] for c in curve]
    plt.figure(figsize=(10, 4.5))
    plt.plot(xs, ys, linewidth=1.2, color="#2563eb")
    plt.axhline(starting_equity, color="#999", linewidth=0.8, linestyle="--")
    plt.title(f"Equity curve — {label}")
    plt.ylabel("Account equity (USD)")
    plt.xlabel("Date")
    plt.grid(alpha=0.25)
    plt.tight_layout()
    path = os.path.join(OUT_DIR, f"equity_{label}.png")
    plt.savefig(path, dpi=130)
    plt.close()


def _write_trade_log(label, trades):
    path = os.path.join(OUT_DIR, f"trades_{label}.csv")
    with open(path, "w") as f:
        f.write("day,symbol,side,entry_time,entry_price,exit_time,exit_price,qty,"
                "exit_reason,r_multiple,pnl_usd,entry_atr_ticks,scaled\n")
        for t in trades:
            f.write(
                f"{t.day},{t.symbol},{t.side},{t.entry_time},{t.entry_price:.2f},"
                f"{t.exit_time},{t.exit_price:.2f},{t.qty},{t.exit_reason},"
                f"{t.r_multiple:.3f},{t.pnl_usd:.2f},{t.entry_atr_ticks:.1f},{t.scaled}\n"
            )


def main():
    refresh = "--refresh" in sys.argv
    results = []

    # 1) ES, 5-minute bars, last ~60 days — the most faithful proxy for the
    #    actual NinjaScript running on a 5-min chart in RTH.
    results.append(run_one("ES_5m_60d", "ES=F", "60d", "5m",
                            point_value=50.0, tick_size=0.25, refresh=refresh))

    # 2) MES on the same window — economics-only rescale check (1/10 size).
    results.append(run_one("MES_5m_60d", "MES=F", "60d", "5m",
                            point_value=5.0, tick_size=0.25, refresh=refresh))

    # 3) ES, 1-hour bars, last ~2 years — broader regime coverage. The
    #    opening-range window is scaled up proportionally (60 min instead of
    #    15) since a 15-minute range makes no sense on an hourly chart, and
    #    the entry cutoff is pushed later for the same reason.
    results.append(run_one("ES_1h_730d", "ES=F", "730d", "1h",
                            point_value=50.0, tick_size=0.25, refresh=refresh,
                            param_overrides={"orb_minutes": 60,
                                              "max_entry_time": dtime(13, 0)}))

    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)

    for r in results:
        print("=" * 78)
        print(f"{r['label']}  ({r['symbol']}, {r['interval']}, {r['n_bars']} bars)")
        print("-" * 78)
        for k, v in r["stats"].items():
            print(f"  {k:26s}: {v}")
        print(f"  drawdown episodes         : {len(r['drawdown_episodes'])}")
        fa = r["failure_analysis"]
        print(f"  losing trades tagged      : {fa.get('n_losses', 0)}")
        for mv in fa.get("mode_verdicts", []):
            print(f"    - {mv['tag']:16s} n={mv['occurrences']:3d} "
                  f"({mv['pct_of_all_losses']:5.1f}% of losses)  "
                  f"pnl=${mv['total_pnl_usd']:>9.2f}  -> {mv['verdict']}")
    print("=" * 78)
    print(f"Full JSON + trade logs + equity charts written to {OUT_DIR}")


if __name__ == "__main__":
    main()
