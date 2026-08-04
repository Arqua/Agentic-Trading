"""
engine.py — Bar-by-bar backtest simulator for ORBTrendATRStrategy.

Fill model (documented explicitly because it materially affects results):

  * Entry: the NinjaScript checks the breakout condition inside
    OnBarUpdate() under Calculate.OnBarClose, so the confirming bar is
    already closed before EnterLong/EnterShort fires. That is a *market*
    order submitted after the close of the signal bar. We fill it at the
    NEXT bar's open, plus `slippage_ticks`. This is the realistic behavior
    of the .cs file as written, not a same-bar fill at the exact breakout
    price.

  * Stop-loss / profit-target: SetStopLoss/SetProfitTarget register real
    resting orders that NinjaTrader fills intrabar off High/Low even in
    OnBarClose mode. We check each bar's High/Low against the stop and
    target from the entry bar onward. If both would be touched in the same
    bar, we conservatively assume the stop fills first (worst case — real
    fills depend on intrabar tick path that OHLC bars can't tell us).

  * Scale-out (+1R, move stop to breakeven, trail): this is an explicit
    ExitLong/ExitShort call inside OnBarUpdate, so — like entries — it is
    evaluated once per bar at the close, using Close[i].

  * Session flatten: position (if any) is closed at the flatten-time bar's
    close.

  * Costs: `commission_per_side` (USD) and `slippage_ticks` (converted to
    USD via point_value) are applied to every fill (entry, scale-out,
    stop, target, flatten).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import List

from data import Bar
from strategy import ET, Params, Trade, atr_series, ema_series


def _to_et(bars: List[Bar]):
    return [datetime.fromtimestamp(b.ts, tz=ET) for b in bars]


class Backtest:
    def __init__(self, symbol: str, bars: List[Bar], params: Params):
        self.symbol = symbol
        self.bars = bars
        self.p = params
        self.times = _to_et(bars)
        self.opens = [b.open for b in bars]
        self.highs = [b.high for b in bars]
        self.lows = [b.low for b in bars]
        self.closes = [b.close for b in bars]
        self.n = len(bars)

        self.ema = ema_series(self.closes, params.trend_period)
        self.atr = atr_series(self.highs, self.lows, self.closes, params.atr_period)

        self.trades: List[Trade] = []
        self.equity_curve: List[tuple] = []  # (datetime, cumulative_pnl_usd)
        self.halt_log: List[dict] = []

    # ------------------------------------------------------------------

    def run(self) -> List[Trade]:
        p = self.p
        cum_pnl = 0.0

        cur_day = None
        orb_high = orb_low = None
        orb_frozen = False
        long_armed = short_armed = False
        long_triggered = short_triggered = False
        trades_today = 0
        day_realized_pnl = 0.0
        halted = False

        # pending entry: signaled at bar i, fills at bar i+1 open
        pending_entry = None  # dict: side, trigger_bar_idx, atr_at_signal

        # open position state
        pos = None  # dict with side, qty, entry_price, entry_time, stop, target,
                    # scaled, one_r, day, entry_atr_ticks

        for i in range(self.n):
            t = self.times[i]
            day = t.date()
            tod = t.time()

            if day != cur_day:
                cur_day = day
                orb_high, orb_low = None, None
                orb_frozen = False
                long_armed = short_armed = False
                long_triggered = short_triggered = False
                trades_today = 0
                day_realized_pnl = 0.0
                halted = False
                pending_entry = None

            orb_end = _add_minutes(p.session_open, p.orb_minutes)

            # ── fill a pending entry from the previous bar's signal ─────
            if pending_entry is not None:
                side = pending_entry["side"]
                fill_price = self.opens[i]
                slip = p.slippage_ticks * p.tick_size
                fill_price = fill_price + slip if side == "LONG" else fill_price - slip

                atr_val = pending_entry["atr_at_signal"]
                stop_dist = max(p.atr_stop_mult * atr_val, pending_entry["min_stop_floor"])
                target_dist = stop_dist * p.reward_risk_ratio
                qty = _size_for_risk(p, stop_dist)

                if side == "LONG":
                    stop_price = fill_price - stop_dist
                    target_price = fill_price + target_dist
                else:
                    stop_price = fill_price + stop_dist
                    target_price = fill_price - target_dist

                pos = {
                    "side": side,
                    "qty": qty,
                    "orig_qty": qty,
                    "entry_price": fill_price,
                    "entry_time": t,
                    "stop": stop_price,
                    "target": target_price,
                    "one_r": stop_dist,
                    "scaled": False,
                    "day": day,
                    "entry_atr_ticks": atr_val / p.tick_size,
                    "scale_pnl": 0.0,
                    "scale_qty": 0,
                }
                trades_today += 1
                pending_entry = None
                if side == "LONG":
                    long_triggered = True
                else:
                    short_triggered = True

            flatten_time = p.flatten_time

            # ── flatten window ───────────────────────────────────────────
            if tod >= flatten_time:
                if pos is not None:
                    exit_price = self.closes[i]
                    pnl = self._close_position(pos, exit_price, t, "SESSION_FLATTEN")
                    day_realized_pnl += pnl
                    cum_pnl += pnl
                    self.equity_curve.append((t, cum_pnl))
                    pos = None
                continue

            # ── build opening range ──────────────────────────────────────
            if p.session_open <= tod < orb_end:
                bh, bl = self.highs[i], self.lows[i]
                orb_high = bh if orb_high is None else max(orb_high, bh)
                orb_low = bl if orb_low is None else min(orb_low, bl)
                continue

            if orb_high is None:
                continue  # no range established yet today (e.g. pre-open/holiday)

            if not orb_frozen and tod >= orb_end:
                orb_frozen = True
                long_armed = True
                short_armed = True

            # ── daily loss limit ─────────────────────────────────────────
            if not halted and p.daily_loss_limit_usd > 0:
                open_pnl = self._unrealized(pos, self.closes[i]) if pos else 0.0
                if day_realized_pnl + open_pnl <= -abs(p.daily_loss_limit_usd):
                    halted = True
                    self.halt_log.append({"day": str(day), "time": str(tod),
                                           "reason": "daily_loss_limit",
                                           "pnl": day_realized_pnl + open_pnl})

            # ── manage open position (intrabar stop/target, bar-close scale/trail) ──
            if pos is not None:
                closed = self._manage_position(pos, i, t)
                if closed:
                    pnl = closed["pnl"]
                    day_realized_pnl += pnl
                    cum_pnl += pnl
                    self.equity_curve.append((t, cum_pnl))
                    pos = None
                continue

            if halted or trades_today >= p.max_trades_per_day:
                continue
            if tod >= p.max_entry_time:
                continue

            atr_val = self.atr[i]
            ema_val = self.ema[i]
            if atr_val is None or ema_val is None:
                continue

            atr_ticks = atr_val / p.tick_size
            if not (p.min_atr_ticks <= atr_ticks <= p.max_atr_ticks):
                continue

            price = self.closes[i]
            buffer = p.buffer_ticks * p.tick_size
            min_stop_floor = max((orb_high - orb_low) * 0.5, 2 * p.tick_size)

            if long_armed and not long_triggered and price > ema_val:
                trigger = orb_high + buffer
                if self.highs[i] >= trigger:
                    pending_entry = {"side": "LONG", "atr_at_signal": atr_val,
                                      "min_stop_floor": min_stop_floor}
                    continue

            if short_armed and not short_triggered and price < ema_val:
                trigger = orb_low - buffer
                if self.lows[i] <= trigger:
                    pending_entry = {"side": "SHORT", "atr_at_signal": atr_val,
                                      "min_stop_floor": min_stop_floor}
                    continue

        # close any still-open position at the last available bar
        if pos is not None:
            pnl = self._close_position(pos, self.closes[-1], self.times[-1], "END_OF_DATA")
            cum_pnl += pnl
            self.equity_curve.append((self.times[-1], cum_pnl))

        return self.trades

    # ------------------------------------------------------------------

    def _unrealized(self, pos, price):
        if pos is None:
            return 0.0
        direction = 1 if pos["side"] == "LONG" else -1
        return direction * (price - pos["entry_price"]) * pos["qty"] * self.p.point_value

    def _manage_position(self, pos, i, t):
        p = self.p
        side = pos["side"]
        high, low, close = self.highs[i], self.lows[i], self.closes[i]

        # ── intrabar stop / target check (conservative: stop wins ties) ──
        if side == "LONG":
            hit_stop = low <= pos["stop"]
            hit_target = high >= pos["target"]
        else:
            hit_stop = high >= pos["stop"]
            hit_target = low <= pos["target"]

        if hit_stop:
            pnl = self._close_position(pos, pos["stop"], t, "STOP")
            return {"pnl": pnl}
        if hit_target:
            pnl = self._close_position(pos, pos["target"], t, "TARGET")
            return {"pnl": pnl}

        # ── bar-close scale-out / breakeven / trail ──────────────────────
        gain = (close - pos["entry_price"]) if side == "LONG" else (pos["entry_price"] - close)
        if not pos["scaled"] and gain >= pos["one_r"] and p.scale_out_pct > 0:
            scale_qty = round(pos["orig_qty"] * (p.scale_out_pct / 100.0))
            scale_qty = max(1, min(scale_qty, pos["qty"] - 1)) if pos["qty"] > 1 else 0
            if scale_qty > 0:
                fill = close - (p.slippage_ticks * p.tick_size if side == "LONG"
                                 else -p.slippage_ticks * p.tick_size)
                leg_pnl = self._pnl_for(pos, fill, scale_qty) - p.commission_per_side * scale_qty
                pos["scale_pnl"] += leg_pnl
                pos["scale_qty"] += scale_qty
                pos["qty"] -= scale_qty
                pos["scaled"] = True
                pos["stop"] = pos["entry_price"]  # breakeven
                self.trades.append(_partial_trade_record(self.symbol, pos, t, fill,
                                                           leg_pnl, scale_qty, "SCALE_OUT"))

        if pos["scaled"]:
            trail_dist = p.atr_trail_mult * (self.atr[i] or pos["one_r"])
            if side == "LONG":
                new_stop = close - trail_dist
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
            else:
                new_stop = close + trail_dist
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop

        return None

    def _pnl_for(self, pos, exit_price, qty):
        direction = 1 if pos["side"] == "LONG" else -1
        return direction * (exit_price - pos["entry_price"]) * qty * self.p.point_value

    def _close_position(self, pos, exit_price, t, reason):
        p = self.p
        remaining_qty = pos["qty"]
        slip = p.slippage_ticks * p.tick_size
        # Exiting a long = selling (adverse = lower fill); exiting a short =
        # buying (adverse = higher fill) — regardless of stop/target/flatten.
        fill = exit_price - slip if pos["side"] == "LONG" else exit_price + slip

        leg_pnl = self._pnl_for(pos, fill, remaining_qty) - p.commission_per_side * remaining_qty
        total_pnl = leg_pnl + pos.get("scale_pnl", 0.0)
        total_qty = remaining_qty + pos.get("scale_qty", 0)

        r_mult = 0.0
        if pos["one_r"] > 0:
            direction = 1 if pos["side"] == "LONG" else -1
            avg_move = direction * (fill - pos["entry_price"])
            r_mult = avg_move / pos["one_r"]

        trade = Trade(
            symbol=self.symbol,
            side=pos["side"],
            entry_time=pos["entry_time"],
            entry_price=pos["entry_price"],
            exit_time=t,
            exit_price=fill,
            qty=total_qty,
            exit_reason=reason,
            stop_price=pos["stop"],
            target_price=pos["target"],
            r_multiple=r_mult,
            pnl_usd=total_pnl,
            scaled=pos["scaled"],
            day=str(pos["day"]),
            entry_atr_ticks=pos["entry_atr_ticks"],
        )
        self.trades.append(trade)
        return total_pnl


def _partial_trade_record(symbol, pos, t, fill, pnl, qty, reason):
    return Trade(
        symbol=symbol, side=pos["side"], entry_time=pos["entry_time"],
        entry_price=pos["entry_price"], exit_time=t, exit_price=fill, qty=qty,
        exit_reason=reason, stop_price=pos["stop"], target_price=pos["target"],
        r_multiple=0.0, pnl_usd=pnl, scaled=True, day=str(pos["day"]),
        entry_atr_ticks=pos["entry_atr_ticks"],
    )


def _size_for_risk(p: Params, stop_dist: float) -> int:
    dollars_per_contract = stop_dist * p.point_value
    if dollars_per_contract <= 0:
        return 1
    qty = int(p.risk_per_trade_usd // dollars_per_contract)
    return max(1, min(qty, p.max_contracts))


def _add_minutes(t, minutes):
    total = t.hour * 60 + t.minute + minutes
    h, m = divmod(total, 60)
    return t.replace(hour=h % 24, minute=m)
