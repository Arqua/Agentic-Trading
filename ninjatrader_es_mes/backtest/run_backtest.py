"""
run_backtest.py — Orchestrates data fetch, backtest run, metrics and
failure analysis, and writes a report + equity-curve charts to
backtest/output/.

Runs (v2):
  DUAL_5m_60d   — signals on ES 5-min RTH bars; execution routed to MES
                  while buying power <= $20k, ES above. Starting equity
                  $10k, so this exercises the MES-first rule.
  ES_5m_60d_ref — identical signals, execution pinned to ES regardless of
                  buying power. Reference run: shows what ignoring the
                  MES-first rule costs a $10k account.
  DUAL_1h_730d  — same routing on 1-hour bars over ~2 years (opening range
                  scaled to 60 min, flatten at the 15:00 bar whose close is
                  the 16:00 session close). Regime stress test only.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import time as dtime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from data import filter_rth, get_bars
from engine import Backtest
from strategy import ES_SPEC, MES_SPEC, Params
from metrics import summarize
from failure_analysis import analyze, drawdown_episodes

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def load_rth(symbol: str, rng: str, interval: str, refresh: bool):
    return filter_rth(get_bars(symbol, rng, interval, refresh=refresh))


def run_one(label: str, rng: str, interval: str, refresh: bool = False,
            force_instrument: str = None, param_overrides: dict = None) -> dict:
    es_bars = load_rth("ES=F", rng, interval, refresh)
    mes_bars = load_rth("MES=F", rng, interval, refresh)
    feeds = {
        "ES": (ES_SPEC, {b.ts: b for b in es_bars}),
        "MES": (MES_SPEC, {b.ts: b for b in mes_bars}),
    }
    params = Params(**(param_overrides or {}))
    bt = Backtest(symbol="ES=F", bars=es_bars, params=params,
                   exec_feeds=feeds, force_instrument=force_instrument)
    trades = bt.run()
    stats = summarize(trades, starting_equity=params.starting_equity_usd)

    fail = analyze(trades)
    dd_episodes = drawdown_episodes(stats["equity_curve"])
    by_instr = Counter(t.symbol for t in trades if t.exit_reason != "SCALE_OUT")

    os.makedirs(OUT_DIR, exist_ok=True)
    _plot_equity(label, stats["equity_curve"], stats["starting_equity"])
    _write_trade_log(label, trades)

    return {
        "label": label, "signal_symbol": "ES=F", "interval": interval,
        "n_signal_bars": len(es_bars), "n_mes_bars": len(mes_bars),
        "force_instrument": force_instrument,
        "params": {k: str(v) for k, v in params.__dict__.items()},
        "trades_by_instrument": dict(by_instr),
        "n_skipped_by_bp_cap": len(bt.skip_log),
        "skip_log": bt.skip_log,
        "halt_log": bt.halt_log,
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
    plt.savefig(os.path.join(OUT_DIR, f"equity_{label}.png"), dpi=130)
    plt.close()


def _write_trade_log(label, trades):
    path = os.path.join(OUT_DIR, f"trades_{label}.csv")
    with open(path, "w") as f:
        f.write("day,instrument,side,entry_time,entry_price,exit_time,exit_price,qty,"
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

    # Optional: --equity N adds a DUAL 5-min run at that starting balance
    # (e.g. --equity 250) alongside the standard runs.
    extra_equity = None
    if "--equity" in sys.argv:
        extra_equity = float(sys.argv[sys.argv.index("--equity") + 1])

    results.append(run_one("DUAL_5m_60d", "60d", "5m", refresh=refresh))

    if extra_equity is not None:
        results.append(run_one(f"EQ{int(extra_equity)}_5m_60d", "60d", "5m",
                                refresh=refresh,
                                param_overrides={"starting_equity_usd": extra_equity}))

    results.append(run_one("ES_5m_60d_ref", "60d", "5m", refresh=refresh,
                            force_instrument="ES"))

    # Hourly bars are stamped on the hour ET; after the RTH filter the last
    # bar of a normal day is 15:00 (covering 15:00-16:00, closing at the
    # session close), so the flatten check must fire on that bar.
    results.append(run_one("DUAL_1h_730d", "730d", "1h", refresh=refresh,
                            param_overrides={"orb_minutes": 60,
                                              "max_entry_time": dtime(13, 0),
                                              "flatten_time": dtime(15, 0)}))

    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)

    for r in results:
        print("=" * 78)
        print(f"{r['label']}  (signals: ES, {r['interval']}, {r['n_signal_bars']} RTH bars"
              + (f", exec pinned to {r['force_instrument']}" if r["force_instrument"] else
                 ", exec: MES until BP > $20k then ES") + ")")
        print("-" * 78)
        print(f"  trades by instrument      : {r['trades_by_instrument']}")
        print(f"  skipped by 5% BP cap      : {r['n_skipped_by_bp_cap']}")
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
