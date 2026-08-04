"""
engine.py — Bar-by-bar backtest simulator for ORBTrendATRStrategy (v2).

v2 changes after reassessment of the v1 engine
----------------------------------------------
  * RTH-only data: the caller must supply bars already passed through
    data.filter_rth(). v1 fed the full Globex session (~72% of bars) into
    the EMA/ATR filters and, on early-close holidays, exited positions at
    the 18:00 ET Globex reopen — four confirmed extended-hours exits.
  * Dual-instrument routing: signals are always computed on the ES series
    (the deep, price-discovering contract); each entry executes on MES
    while buying power <= bp_switch_threshold_usd ($20k default) and on ES
    above it. Fills are priced off the executing instrument's own bars
    (timestamp-aligned; falls back to the ES bar when MES has a gap —
    the two track within a tick).
  * Full fee accounting: v1 only charged commission on exit legs (entry
    side was never charged — costs were undercounted ~50%) and used one
    flat rate for both symbols. v2 charges InstrumentSpec.fee_per_side
    (broker commission + CME exchange/clearing + NFA) on every fill leg:
    entry, scale-out, and final exit.
  * Fees inside the limits: position sizing subtracts the round-turn fee
    from the per-contract risk budget; the daily loss limit sees
    fee-inclusive P&L; a viability guard skips any trade whose 1R gross
    profit wouldn't cover at least 10x the round-turn fee.
  * Catastrophic stop cap: the stop-loss order is placed one tick above
    the price at which a full stop-out (including fees) would consume
    bp_stop_cap_pct (5%) of current buying power. If the ATR stop is
    already tighter, it stands; if even the capped stop would be tighter
    than min_stop_ticks, the trade is skipped and logged.
  * Half-day guard: if a session's data ends early (13:00 ET close) with
    a position still open, it is closed at that day's final bar instead
    of silently surviving into the next session.

Fill model (unchanged from v1 where it was correct)
---------------------------------------------------
  * Entry: signal evaluated on bar close (Calculate.OnBarClose), market
    order fills at the NEXT bar's open +/- slippage_ticks.
  * Stop/target: resting orders, filled intrabar off each bar's high/low.
    If both could fill in one bar, the stop is assumed to fill first.
  * Scale-out/trail: evaluated once per bar at the close, like the
    NinjaScript's OnBarUpdate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from data import Bar
from strategy import ET, InstrumentSpec, Params, Trade, atr_series, ema_series


def _to_et(bars: List[Bar]):
    return [datetime.fromtimestamp(b.ts, tz=ET) for b in bars]


class Backtest:
    def __init__(
        self,
        symbol: str,
        bars: List[Bar],
        params: Params,
        exec_feeds: Dict[str, Tuple[InstrumentSpec, Dict[int, Bar]]],
        force_instrument: Optional[str] = None,
    ):
        """
        Parameters
        ----------
        symbol          : Signal-series symbol (informational).
        bars            : RTH-filtered signal bars (ES).
        params          : Strategy parameters.
        exec_feeds      : {"MES": (MES_SPEC, {ts: Bar}), "ES": (ES_SPEC, {ts: Bar})}
                          — execution instruments, keyed by root, each with
                          its spec and a timestamp-indexed bar lookup.
        force_instrument: Pin execution to one root (used by the ES-only
                          reference run); None = route by buying power.
        """
        self.symbol = symbol
        self.bars = bars
        self.p = params
        self.exec_feeds = exec_feeds
        self.force_instrument = force_instrument

        self.times = _to_et(bars)
        self.opens = [b.open for b in bars]
        self.highs = [b.high for b in bars]
        self.lows = [b.low for b in bars]
        self.closes = [b.close for b in bars]
        self.n = len(bars)

        self.ema = ema_series(self.closes, params.trend_period)
        self.atr = atr_series(self.highs, self.lows, self.closes, params.atr_period)

        self.trades: List[Trade] = []
        self.equity_curve: List[tuple] = []   # (datetime, equity_usd)
        self.halt_log: List[dict] = []
        self.skip_log: List[dict] = []        # trades skipped by the 5% BP cap / fee guard

    # ------------------------------------------------------------------

    def _route(self, equity: float) -> str:
        """MES until buying power exceeds the threshold, ES after."""
        if self.force_instrument:
            return self.force_instrument
        if "MES" in self.exec_feeds and equity <= self.p.bp_switch_threshold_usd:
            return "MES"
        return "ES" if "ES" in self.exec_feeds else next(iter(self.exec_feeds))

    def _exec_bar(self, instr: str, i: int) -> Bar:
        """The executing instrument's bar at signal-bar i's timestamp
        (falls back to the signal bar itself on a feed gap)."""
        spec, by_ts = self.exec_feeds[instr]
        return by_ts.get(self.bars[i].ts) or self.bars[i]

    # ------------------------------------------------------------------

    def run(self) -> List[Trade]:
        p = self.p
        equity = p.starting_equity_usd

        cur_day = None
        orb_high = orb_low = None
        orb_frozen = False
        long_armed = short_armed = False
        long_triggered = short_triggered = False
        trades_today = 0
        day_realized_pnl = 0.0
        halted = False

        pending_entry = None  # signaled at bar i, fills at bar i+1's open
        pos = None

        for i in range(self.n):
            t = self.times[i]
            day = t.date()
            tod = t.time()

            if day != cur_day:
                # Half-day guard: a position that survived to the end of a
                # short session closes at that session's final bar, not here.
                if pos is not None:
                    pnl = self._close_position(pos, self.closes[i - 1],
                                                self.times[i - 1], "SESSION_FLATTEN")
                    day_realized_pnl += pnl
                    equity += pnl
                    self.equity_curve.append((self.times[i - 1], equity))
                    pos = None
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
                instr = pending_entry["instr"]
                spec, _ = self.exec_feeds[instr]
                eb = self._exec_bar(instr, i)
                slip = p.slippage_ticks * spec.tick_size
                side = pending_entry["side"]
                fill_price = eb.open + slip if side == "LONG" else eb.open - slip

                plan = self._plan_order(spec, pending_entry["atr_at_signal"],
                                          pending_entry["min_stop_floor"], equity)
                if plan is None:
                    self.skip_log.append({"day": str(day), "time": str(tod),
                                            "side": side, "instr": instr,
                                            "reason": "bp_cap_or_fee_guard"})
                    pending_entry = None
                else:
                    qty, stop_dist = plan
                    target_dist = stop_dist * p.reward_risk_ratio
                    if side == "LONG":
                        stop_price = fill_price - stop_dist
                        target_price = fill_price + target_dist
                    else:
                        stop_price = fill_price + stop_dist
                        target_price = fill_price - target_dist

                    pos = {
                        "side": side,
                        "instr": instr,
                        "spec": spec,
                        "qty": qty,
                        "orig_qty": qty,
                        "entry_price": fill_price,
                        "entry_time": t,
                        "stop": stop_price,
                        "target": target_price,
                        "one_r": stop_dist,
                        "scaled": False,
                        "day": day,
                        "entry_atr_ticks": pending_entry["atr_at_signal"] / spec.tick_size,
                        "scale_pnl": 0.0,
                        "scale_qty": 0,
                        # entry-side fee, charged when the position resolves
                        "entry_fee": spec.fee_per_side * qty,
                    }
                    trades_today += 1
                    pending_entry = None
                    if side == "LONG":
                        long_triggered = True
                    else:
                        short_triggered = True

            # ── flatten window ───────────────────────────────────────────
            if tod >= p.flatten_time:
                if pos is not None:
                    eb = self._exec_bar(pos["instr"], i)
                    pnl = self._close_position(pos, eb.close, t, "SESSION_FLATTEN")
                    day_realized_pnl += pnl
                    equity += pnl
                    self.equity_curve.append((t, equity))
                    pos = None
                continue

            # ── build opening range ──────────────────────────────────────
            if p.session_open <= tod < orb_end:
                bh, bl = self.highs[i], self.lows[i]
                orb_high = bh if orb_high is None else max(orb_high, bh)
                orb_low = bl if orb_low is None else min(orb_low, bl)
                continue

            if orb_high is None:
                continue

            if not orb_frozen and tod >= orb_end:
                orb_frozen = True
                long_armed = True
                short_armed = True

            # ── daily loss limit (fee-inclusive P&L) ─────────────────────
            if not halted and p.daily_loss_limit_usd > 0:
                open_pnl = self._unrealized(pos, i) if pos else 0.0
                if day_realized_pnl + open_pnl <= -abs(p.daily_loss_limit_usd):
                    halted = True
                    self.halt_log.append({"day": str(day), "time": str(tod),
                                           "reason": "daily_loss_limit",
                                           "pnl": day_realized_pnl + open_pnl})

            # ── manage open position ─────────────────────────────────────
            if pos is not None:
                closed = self._manage_position(pos, i, t)
                if closed:
                    pnl = closed["pnl"]
                    day_realized_pnl += pnl
                    equity += pnl
                    self.equity_curve.append((t, equity))
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
            instr = self._route(equity)

            if long_armed and not long_triggered and price > ema_val:
                if self.highs[i] >= orb_high + buffer:
                    pending_entry = {"side": "LONG", "instr": instr,
                                      "atr_at_signal": atr_val,
                                      "min_stop_floor": min_stop_floor}
                    continue

            if short_armed and not short_triggered and price < ema_val:
                if self.lows[i] <= orb_low - buffer:
                    pending_entry = {"side": "SHORT", "instr": instr,
                                      "atr_at_signal": atr_val,
                                      "min_stop_floor": min_stop_floor}
                    continue

        if pos is not None:
            pnl = self._close_position(pos, self.closes[-1], self.times[-1], "END_OF_DATA")
            equity += pnl
            self.equity_curve.append((self.times[-1], equity))

        return self.trades

    # ------------------------------------------------------------------

    def _plan_order(self, spec: InstrumentSpec, atr_val: float,
                     min_stop_floor: float, buying_power: float):
        """
        Size the order and apply the catastrophic stop cap.

        Returns (qty, stop_dist) or None if the trade must be skipped.

          1. stop_dist = max(ATR stop, half the opening range).
          2. qty from the risk budget NET of round-turn fees:
             floor(risk / (stop_dist * point_value + round_turn_fee)).
          3. 5% cap: the stop may never sit farther than the price at which
             a full stop-out (loss + fees) consumes bp_stop_cap_pct of
             buying power — the stop is placed one tick above that point.
             If that cap leaves less than min_stop_ticks of room, skip.
          4. Fee viability: skip if 1R gross < 10x the round-turn fee.
        """
        p = self.p
        stop_dist = max(p.atr_stop_mult * atr_val, min_stop_floor)
        rt_fee = 2 * spec.fee_per_side

        per_contract_risk = stop_dist * spec.point_value + rt_fee
        qty = int(p.risk_per_trade_usd // per_contract_risk)
        qty = max(1, min(qty, p.max_contracts))

        max_loss = p.bp_stop_cap_pct * buying_power
        cap_dist = (max_loss - rt_fee * qty) / (spec.point_value * qty) - spec.tick_size
        if cap_dist < stop_dist:
            if cap_dist < p.min_stop_ticks * spec.tick_size:
                return None
            stop_dist = cap_dist

        if stop_dist * spec.point_value < 10 * rt_fee:
            return None
        return qty, stop_dist

    def _unrealized(self, pos, i: int) -> float:
        if pos is None:
            return 0.0
        eb = self._exec_bar(pos["instr"], i)
        direction = 1 if pos["side"] == "LONG" else -1
        return direction * (eb.close - pos["entry_price"]) * pos["qty"] * pos["spec"].point_value

    def _manage_position(self, pos, i, t):
        p = self.p
        spec: InstrumentSpec = pos["spec"]
        side = pos["side"]
        eb = self._exec_bar(pos["instr"], i)
        high, low, close = eb.high, eb.low, eb.close

        # ── intrabar stop / target check (conservative: stop wins ties) ──
        if side == "LONG":
            hit_stop = low <= pos["stop"]
            hit_target = high >= pos["target"]
        else:
            hit_stop = high >= pos["stop"]
            hit_target = low <= pos["target"]

        if hit_stop:
            return {"pnl": self._close_position(pos, pos["stop"], t, "STOP")}
        if hit_target:
            return {"pnl": self._close_position(pos, pos["target"], t, "TARGET")}

        # ── bar-close scale-out / breakeven / trail ──────────────────────
        gain = (close - pos["entry_price"]) if side == "LONG" else (pos["entry_price"] - close)
        if not pos["scaled"] and gain >= pos["one_r"] and p.scale_out_pct > 0:
            scale_qty = round(pos["orig_qty"] * (p.scale_out_pct / 100.0))
            scale_qty = max(1, min(scale_qty, pos["qty"] - 1)) if pos["qty"] > 1 else 0
            if scale_qty > 0:
                slip = p.slippage_ticks * spec.tick_size
                fill = close - slip if side == "LONG" else close + slip
                leg_pnl = self._pnl_for(pos, fill, scale_qty) - spec.fee_per_side * scale_qty
                pos["scale_pnl"] += leg_pnl
                pos["scale_qty"] += scale_qty
                pos["qty"] -= scale_qty
                pos["scaled"] = True
                pos["stop"] = pos["entry_price"]  # breakeven
                self.trades.append(_partial_trade_record(pos, t, fill, leg_pnl, scale_qty))

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
        return direction * (exit_price - pos["entry_price"]) * qty * pos["spec"].point_value

    def _close_position(self, pos, exit_price, t, reason):
        p = self.p
        spec: InstrumentSpec = pos["spec"]
        remaining_qty = pos["qty"]
        slip = p.slippage_ticks * spec.tick_size
        # Exiting a long = selling (adverse = lower fill); exiting a short =
        # buying (adverse = higher fill) — regardless of stop/target/flatten.
        fill = exit_price - slip if pos["side"] == "LONG" else exit_price + slip

        # Exit-side fee on this leg + the entry-side fee for the whole
        # position (charged once, here, when the position resolves).
        leg_pnl = (self._pnl_for(pos, fill, remaining_qty)
                    - spec.fee_per_side * remaining_qty
                    - pos["entry_fee"])
        total_pnl = leg_pnl + pos.get("scale_pnl", 0.0)
        total_qty = remaining_qty + pos.get("scale_qty", 0)

        r_mult = 0.0
        if pos["one_r"] > 0:
            direction = 1 if pos["side"] == "LONG" else -1
            avg_move = direction * (fill - pos["entry_price"])
            r_mult = avg_move / pos["one_r"]

        trade = Trade(
            symbol=pos["instr"],
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


def _partial_trade_record(pos, t, fill, pnl, qty):
    return Trade(
        symbol=pos["instr"], side=pos["side"], entry_time=pos["entry_time"],
        entry_price=pos["entry_price"], exit_time=t, exit_price=fill, qty=qty,
        exit_reason="SCALE_OUT", stop_price=pos["stop"], target_price=pos["target"],
        r_multiple=0.0, pnl_usd=pnl, scaled=True, day=str(pos["day"]),
        entry_atr_ticks=pos["entry_atr_ticks"],
    )


def _add_minutes(t, minutes):
    total = t.hour * 60 + t.minute + minutes
    h, m = divmod(total, 60)
    return t.replace(hour=h % 24, minute=m)
