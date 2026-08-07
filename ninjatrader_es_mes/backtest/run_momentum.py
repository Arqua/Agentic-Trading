"""
run_momentum.py — Backtest report for the MA momentum/direction alternative.

Answers the question "what if we trade every 5 minutes off the moving
average's direction and momentum, instead of once a day off the opening
range?" — with the cost decomposition, a parameter sweep, walk-forward
validation and a long-sample regime check that together decide it.

    python run_momentum.py            # full report + charts
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from data import filter_rth, get_bars
from ma_momentum import MomentumBacktest, MomoParams
from metrics import summarize
from strategy import ET, InstrumentSpec, MES_SPEC

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
FREE_SPEC = InstrumentSpec("MES", 5.0, 0.0)   # zero-fee twin for cost decomposition

# Canonical configurations reported below.
CONFIGS = {
    "churn_every_bar":  dict(mode="churn"),
    "flip_ema20":       dict(),
    "flip_ema9_fast":   dict(ma_period=9, slope_lookback=1,
                              slope_thresh_ticks=0.5, require_agree=False),
    "flip_sma50_slow":  dict(ma_period=50, ma_type="sma", slope_lookback=1,
                              slope_thresh_ticks=4.0),
}


def _run(bars, spec, **kw):
    bt = MomentumBacktest(bars, spec, MomoParams(**kw))
    trades = bt.run()
    return summarize(trades, starting_equity=10_000.0), trades


def _plot(label, curve, start=10_000.0):
    if not curve:
        return
    plt.figure(figsize=(10, 4.5))
    plt.plot([c[0] for c in curve], [c[1] for c in curve], linewidth=1.1, color="#b45309")
    plt.axhline(start, color="#999", linewidth=0.8, linestyle="--")
    plt.title(f"Equity curve — MA momentum {label}")
    plt.ylabel("Account equity (USD)")
    plt.xlabel("Date")
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(os.path.join(OUT_DIR, f"equity_MOMO_{label}.png"), dpi=130)
    plt.close()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    mes5 = filter_rth(get_bars("MES=F", "60d", "5m"))
    mes1h = filter_rth(get_bars("MES=F", "730d", "1h"))
    report = {"n_bars_5m": len(mes5), "n_bars_1h": len(mes1h), "configs": {}}

    print("=" * 84)
    print("MA MOMENTUM / DIRECTION — 5-minute MES, RTH only, 1 contract")
    print("=" * 84)
    print(f"{'config':>18} | {'trades':>6} {'/day':>5} {'win%':>6} {'PF':>6} "
          f"{'net$':>9} {'gross$':>9} {'costs$':>9} {'DD%':>6}")

    for label, kw in CONFIGS.items():
        net, trades = _run(mes5, MES_SPEC, **kw)
        gross, _ = _run(mes5, FREE_SPEC, slippage_ticks=0.0, **kw)
        costs = gross["total_pnl_usd"] - net["total_pnl_usd"]
        d = max(net["trading_days"], 1)
        print(f"{label:>18} | {net['n_trades']:>6} {net['n_trades']/d:>5.1f} "
              f"{net['win_rate']:>6.1%} {str(net['profit_factor']):>6} "
              f"{net['total_pnl_usd']:>9.2f} {gross['total_pnl_usd']:>9.2f} "
              f"{costs:>9.2f} {net['max_drawdown_pct']:>6.1f}")
        _plot(label, net["equity_curve"])
        report["configs"][label] = {
            "net": {k: v for k, v in net.items() if k not in ("equity_curve", "daily_pnl")},
            "gross_pnl_usd": gross["total_pnl_usd"],
            "total_costs_usd": round(costs, 2),
            "cost_per_trade_usd": round(costs / max(net["n_trades"], 1), 2),
        }

    # ── Walk-forward: optimize on first half, test on second ──────────────
    mid = len(mes5) // 2
    IS, OOS = mes5[:mid], mes5[mid:]
    grid = [dict(ma_period=ma, ma_type=mt, slope_lookback=sl,
                  slope_thresh_ticks=th, require_agree=ag)
            for ma in (9, 20, 50) for mt in ("ema", "sma")
            for sl in (1, 3, 6) for th in (0.5, 1.0, 2.0, 4.0) for ag in (True, False)]
    scored = []
    for kw in grid:
        s, _ = _run(IS, MES_SPEC, **kw)
        if s["n_trades"] >= 20:
            scored.append((s["total_pnl_usd"], kw, s["n_trades"]))
    scored.sort(reverse=True)

    print(f"\nWALK-FORWARD  (in-sample "
          f"{datetime.fromtimestamp(IS[0].ts, tz=ET).date()}→"
          f"{datetime.fromtimestamp(IS[-1].ts, tz=ET).date()}, out-of-sample "
          f"{datetime.fromtimestamp(OOS[0].ts, tz=ET).date()}→"
          f"{datetime.fromtimestamp(OOS[-1].ts, tz=ET).date()})")
    print(f"{'rank':>4} {'IS $':>9} {'OOS $':>9}  config")
    wf = []
    for rank, (pnl, kw, n) in enumerate(scored[:5], 1):
        o, _ = _run(OOS, MES_SPEC, **kw)
        wf.append({"rank": rank, "is_pnl": pnl, "oos_pnl": o["total_pnl_usd"], "config": kw})
        cfg = (f"ma{kw['ma_period']}{kw['ma_type']} slb{kw['slope_lookback']} "
               f"th{kw['slope_thresh_ticks']} agree{int(kw['require_agree'])}")
        print(f"{rank:>4} {pnl:>9.2f} {o['total_pnl_usd']:>9.2f}  {cfg}")
    mean_oos = sum(w["oos_pnl"] for w in wf) / len(wf)
    n_pos = sum(1 for p, _, _ in scored if p > 0)
    print(f"     mean out-of-sample P&L of the 5 best in-sample configs: ${mean_oos:.2f}")
    print(f"     profitable in-sample: {n_pos}/{len(scored)} configurations")
    report["walk_forward"] = {"results": wf, "mean_oos_pnl": round(mean_oos, 2),
                               "n_profitable_in_sample": n_pos, "n_configs": len(scored)}

    # ── Long-sample regime check on 1-hour bars ───────────────────────────
    print(f"\nREGIME CHECK — same rules on {len(mes1h)} hourly bars (~2 years)")
    print(f"{'config':>18} | {'trades':>6} {'win%':>6} {'PF':>6} {'net$':>10} {'DD%':>6}")
    report["regime_check_1h"] = {}
    for label, kw in CONFIGS.items():
        s, _ = _run(mes1h, MES_SPEC, entry_start=dtime(10, 0), **kw)
        print(f"{label:>18} | {s['n_trades']:>6} {s['win_rate']:>6.1%} "
              f"{str(s['profit_factor']):>6} {s['total_pnl_usd']:>10.2f} "
              f"{s['max_drawdown_pct']:>6.1f}")
        report["regime_check_1h"][label] = {
            k: v for k, v in s.items() if k not in ("equity_curve", "daily_pnl")}

    with open(os.path.join(OUT_DIR, "momentum_results.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("=" * 84)
    print(f"Report + charts written to {OUT_DIR}")


if __name__ == "__main__":
    main()
