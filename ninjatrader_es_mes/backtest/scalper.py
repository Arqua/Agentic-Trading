"""
scalper.py — Trend-pullback scalper for MES (high-frequency companion to
the ORB strategy).

The profile this targets — many trades per day at a high win rate — comes
from inverting the ORB's geometry: instead of a 40-50% win rate with 2R
winners, the scalper takes a SMALL fixed target with a WIDER stop, in the
direction of the prevailing trend, many times a day. That produces
85-95% win rates mechanically; whether it produces MONEY depends on the
tail: the rare loss is several times the typical win, so fees, slippage
and the loss containment rules decide everything. The backtest exists to
find configurations where the arithmetic actually closes.

Rules
-----
  Trend   : close > EMA(50) → longs only; close < EMA(50) → shorts only.
  Setup   : price pulls back through EMA(9) intrabar and closes back on
            the trend side of it (a rejection bar).
  Entry   : market at next bar open (+1 tick slippage).
  Target  : +tp_ticks, resting limit — fills only if the bar trades
            THROUGH it by a tick (conservative limit-fill rule; no
            slippage on a limit).
  Stop    : -sl_ticks, stop-market (1 tick slippage), additionally capped
            by the 5%-of-buying-power sell-off rule (size reduces first).
  Time    : if neither side is hit within time_stop_bars bars, exit at
            the close — scalps that go nowhere are dead inventory.
  Session : entries 09:35-15:30 ET, everything flat by 15:55. RTH bars
            only. One position at a time; max_trades_per_day; daily loss
            limit halts new entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from data import Bar
from strategy import ET, InstrumentSpec, Trade, atr_series, ema_series


@dataclass
class ScalpParams:
    ema_fast: int = 9
    ema_slow: int = 50
    atr_period: int = 14
    min_atr_ticks: int = 8
    max_atr_ticks: int = 160

    tp_ticks: int = 8            # +2.0 pts target
    sl_ticks: int = 24           # -6.0 pts protective stop
    time_stop_bars: int = 12     # give up after an hour of nothing

    entry_start: dtime = dtime(9, 35)
    entry_end: dtime = dtime(15, 30)
    flatten_time: dtime = dtime(15, 55)

    max_trades_per_day: int = 25
    daily_loss_limit_usd: float = 300.0
    risk_per_trade_usd: float = 100.0
    max_contracts: int = 3

    starting_equity_usd: float = 10_000.0
    bp_stop_cap_pct: float = 0.05
    min_stop_ticks: int = 4
    min_tp_fee_mult: float = 3.0   # gross target must be >= this x round-turn fee

    slippage_ticks: float = 1.0
    tick_size: float = 0.25


class ScalpBacktest:
    def __init__(self, bars: List[Bar], spec: InstrumentSpec, params: ScalpParams):
        self.bars = bars
        self.spec = spec
        self.p = params
        self.times = [datetime.fromtimestamp(b.ts, tz=ET) for b in bars]
        self.opens = [b.open for b in bars]
        self.highs = [b.high for b in bars]
        self.lows = [b.low for b in bars]
        self.closes = [b.close for b in bars]
        self.n = len(bars)

        closes = self.closes
        self.ema_fast = ema_series(closes, params.ema_fast)
        self.ema_slow = ema_series(closes, params.ema_slow)
        self.atr = atr_series(self.highs, self.lows, closes, params.atr_period)

        self.trades: List[Trade] = []
        self.skip_log: List[dict] = []
        self.halt_log: List[dict] = []

    # ------------------------------------------------------------------

    def run(self) -> List[Trade]:
        p = self.p
        spec = self.spec
        tick = spec.tick_size
        equity = p.starting_equity_usd

        cur_day = None
        trades_today = 0
        day_pnl = 0.0
        halted = False
        pending: Optional[str] = None   # "LONG"/"SHORT" signaled on prior bar
        pos = None

        # Gross target must clear the fee bar at all — otherwise scalping
        # this instrument is structurally pointless.
        if p.tp_ticks * tick * spec.point_value < p.min_tp_fee_mult * 2 * spec.fee_per_side:
            return self.trades

        for i in range(self.n):
            t = self.times[i]
            day = t.date()
            tod = t.time()

            if day != cur_day:
                if pos is not None:   # half-day guard
                    equity += self._close(pos, self.closes[i - 1], self.times[i - 1],
                                            "SESSION_FLATTEN")
                    pos = None
                cur_day = day
                trades_today = 0
                day_pnl = 0.0
                halted = False
                pending = None

            # ── fill pending entry at this bar's open ────────────────────
            if pending is not None:
                side = pending
                pending = None
                plan = self._plan(equity)
                if plan is None:
                    self.skip_log.append({"day": str(day), "time": str(tod)})
                else:
                    qty, sl_dist = plan
                    slip = p.slippage_ticks * tick
                    fill = self.opens[i] + slip if side == "LONG" else self.opens[i] - slip
                    tp_d = p.tp_ticks * tick
                    pos = {
                        "side": side, "qty": qty, "entry_price": fill,
                        "entry_time": t, "entry_bar": i, "day": day,
                        "tp": fill + tp_d if side == "LONG" else fill - tp_d,
                        "stop": fill - sl_dist if side == "LONG" else fill + sl_dist,
                        "one_r": sl_dist,
                        "entry_atr_ticks": (self.atr[i] or 0) / tick,
                        "entry_fee": spec.fee_per_side * qty,
                    }
                    trades_today += 1

            # ── flatten window ───────────────────────────────────────────
            if tod >= p.flatten_time:
                if pos is not None:
                    pnl = self._close(pos, self.closes[i], t, "SESSION_FLATTEN")
                    day_pnl += pnl
                    equity += pnl
                    pos = None
                continue

            # ── manage open position ─────────────────────────────────────
            if pos is not None:
                res = self._manage(pos, i, t)
                if res is not None:
                    day_pnl += res
                    equity += res
                    pos = None
                continue

            # ── new-entry gates ──────────────────────────────────────────
            if halted or trades_today >= p.max_trades_per_day:
                continue
            if not (p.entry_start <= tod < p.entry_end):
                continue
            if p.daily_loss_limit_usd > 0 and day_pnl <= -abs(p.daily_loss_limit_usd):
                if not halted:
                    halted = True
                    self.halt_log.append({"day": str(day), "time": str(tod),
                                           "pnl": day_pnl})
                continue

            ef, es_, a = self.ema_fast[i], self.ema_slow[i], self.atr[i]
            if ef is None or es_ is None or a is None:
                continue
            atr_ticks = a / tick
            if not (p.min_atr_ticks <= atr_ticks <= p.max_atr_ticks):
                continue

            c, lo, hi = self.closes[i], self.lows[i], self.highs[i]

            # Pullback-rejection setups
            if c > es_ and lo <= ef and c > ef:
                pending = "LONG"
            elif c < es_ and hi >= ef and c < ef:
                pending = "SHORT"

        if pos is not None:
            self._close(pos, self.closes[-1], self.times[-1], "END_OF_DATA")

        return self.trades

    # ------------------------------------------------------------------

    def _plan(self, equity: float) -> Optional[Tuple[int, float]]:
        """Size the scalp; apply the 5% sell-off cap (size shrinks first)."""
        p = self.p
        spec = self.spec
        tick = spec.tick_size
        sl_dist = p.sl_ticks * tick
        rt_fee = 2 * spec.fee_per_side

        per_contract = sl_dist * spec.point_value + rt_fee
        qty = int(p.risk_per_trade_usd // per_contract)
        qty = max(1, min(qty, p.max_contracts))

        max_loss = p.bp_stop_cap_pct * equity
        min_stop = p.min_stop_ticks * tick
        while qty > 1 and max_loss / (spec.point_value * qty) < min_stop:
            qty -= 1
        cap_dist = max_loss / (spec.point_value * qty)
        if cap_dist < sl_dist:
            if cap_dist < min_stop:
                return None
            sl_dist = cap_dist
        return qty, sl_dist

    def _manage(self, pos, i, t) -> Optional[float]:
        p = self.p
        hi, lo, close = self.highs[i], self.lows[i], self.closes[i]
        tick = self.spec.tick_size

        if pos["side"] == "LONG":
            hit_stop = lo <= pos["stop"]
            hit_tp = hi >= pos["tp"] + tick    # must trade THROUGH the limit
        else:
            hit_stop = hi >= pos["stop"]
            hit_tp = lo <= pos["tp"] - tick

        if hit_stop:                             # stop wins ties (conservative)
            return self._close(pos, pos["stop"], t, "STOP", slip=True)
        if hit_tp:
            return self._close(pos, pos["tp"], t, "TARGET", slip=False)
        if i - pos["entry_bar"] >= p.time_stop_bars:
            return self._close(pos, close, t, "TIME", slip=True)
        return None

    def _close(self, pos, exit_price, t, reason, slip=True) -> float:
        p = self.p
        spec = self.spec
        s = p.slippage_ticks * spec.tick_size if slip else 0.0
        fill = exit_price - s if pos["side"] == "LONG" else exit_price + s
        direction = 1 if pos["side"] == "LONG" else -1
        pnl = (direction * (fill - pos["entry_price"]) * pos["qty"] * spec.point_value
                - spec.fee_per_side * pos["qty"] - pos["entry_fee"])
        r = (direction * (fill - pos["entry_price"]) / pos["one_r"]) if pos["one_r"] else 0.0
        self.trades.append(Trade(
            symbol=spec.name, side=pos["side"], entry_time=pos["entry_time"],
            entry_price=pos["entry_price"], exit_time=t, exit_price=fill,
            qty=pos["qty"], exit_reason=reason, stop_price=pos["stop"],
            target_price=pos["tp"], r_multiple=r, pnl_usd=pnl, scaled=False,
            day=str(pos["day"]), entry_atr_ticks=pos["entry_atr_ticks"],
        ))
        return pnl
