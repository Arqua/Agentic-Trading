"""
ma_momentum.py — Always-on MA momentum / direction system (alternative to ORB).

Concept
-------
Re-evaluate on EVERY 5-minute bar close. The desired position is a function
of the moving average's DIRECTION (slope) and the market's MOMENTUM (rate of
change), rather than a once-a-day setup:

    slope_t = MA_t - MA_{t-slope_lookback}          (direction)
    roc_t   = close_t - close_{t-roc_lookback}      (momentum)

    desired = +1  if slope_t >  slope_thresh AND (not require_agree or roc_t > 0)
              -1  if slope_t < -slope_thresh AND (not require_agree or roc_t < 0)
               0  otherwise (chop / flat zone)

Two execution modes, because "trade every 5 minutes" is ambiguous:

  mode="flip"   — practical reading. Hold the position while the signal is
                  unchanged; trade only when `desired` changes. Costs are
                  paid per CHANGE.

  mode="churn"  — literal reading. Close the position and re-open it every
                  single bar, paying the full cost stack each time, even
                  when the signal is unchanged. Included to quantify what
                  literal 5-minute churn costs.

Fill model (matches engine.py's conservatism)
---------------------------------------------
  * Signal computed on bar close; the resulting order fills at the NEXT
    bar's open, plus/minus `slippage_ticks` (market order).
  * Optional protective stop / target, checked intrabar off high/low with
    the stop winning ties. With stop_ticks=0 the system is pure
    signal-reversal (exit only when the signal changes).
  * Every fill leg pays InstrumentSpec.fee_per_side.
  * RTH only; flat at `flatten_time` every session (no overnight).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import List, Optional

from data import Bar
from strategy import ET, InstrumentSpec, Trade, atr_series, ema_series


def sma_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, len(values)):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


@dataclass
class MomoParams:
    ma_period: int = 20
    ma_type: str = "ema"          # "ema" | "sma"
    slope_lookback: int = 3        # bars back for the MA slope
    slope_thresh_ticks: float = 1.0  # |slope| must exceed this to take a side
    roc_lookback: int = 3          # bars back for the momentum check
    require_agree: bool = True     # momentum must agree with MA direction

    mode: str = "flip"             # "flip" | "churn"
    allow_short: bool = True

    stop_ticks: int = 0            # 0 = no protective stop (signal exit only)
    target_ticks: int = 0          # 0 = no fixed target

    atr_period: int = 14
    min_atr_ticks: int = 0         # 0 = volatility filter off
    max_atr_ticks: int = 10_000

    entry_start: dtime = dtime(9, 35)
    entry_end: dtime = dtime(15, 45)
    flatten_time: dtime = dtime(15, 55)

    qty: int = 1
    slippage_ticks: float = 1.0
    starting_equity_usd: float = 10_000.0
    daily_loss_limit_usd: float = 0.0   # 0 = off


class MomentumBacktest:
    def __init__(self, bars: List[Bar], spec: InstrumentSpec, params: MomoParams):
        self.bars = bars
        self.spec = spec
        self.p = params
        self.times = [datetime.fromtimestamp(b.ts, tz=ET) for b in bars]
        self.opens = [b.open for b in bars]
        self.highs = [b.high for b in bars]
        self.lows = [b.low for b in bars]
        self.closes = [b.close for b in bars]
        self.n = len(bars)

        if params.ma_type == "sma":
            self.ma = sma_series(self.closes, params.ma_period)
        else:
            self.ma = ema_series(self.closes, params.ma_period)
        self.atr = atr_series(self.highs, self.lows, self.closes, params.atr_period)

        self.trades: List[Trade] = []
        self.signal_changes = 0

    # ------------------------------------------------------------------

    def _desired(self, i: int) -> int:
        """Target position for the signal computed on bar i: +1 / 0 / -1."""
        p = self.p
        tick = self.spec.tick_size
        ma_now = self.ma[i]
        j = i - p.slope_lookback
        if ma_now is None or j < 0 or self.ma[j] is None:
            return 0
        k = i - p.roc_lookback
        if k < 0:
            return 0

        if p.min_atr_ticks > 0:
            a = self.atr[i]
            if a is None:
                return 0
            at = a / tick
            if not (p.min_atr_ticks <= at <= p.max_atr_ticks):
                return 0

        slope = ma_now - self.ma[j]
        roc = self.closes[i] - self.closes[k]
        thresh = p.slope_thresh_ticks * tick

        if slope > thresh and (not p.require_agree or roc > 0):
            return 1
        if slope < -thresh and (not p.require_agree or roc < 0):
            return -1 if p.allow_short else 0
        return 0

    # ------------------------------------------------------------------

    def run(self) -> List[Trade]:
        p = self.p
        spec = self.spec
        tick = spec.tick_size
        slip = p.slippage_ticks * tick

        equity = p.starting_equity_usd
        pos = None            # {"dir", "e", "t0", "bar", "day", "stop", "tgt"}
        pending: Optional[int] = None   # desired position to establish next bar
        cur_day = None
        day_pnl = 0.0
        halted = False

        def close_pos(px, t, reason, slipping=True):
            nonlocal pos
            s = slip if slipping else 0.0
            fill = px - s if pos["dir"] > 0 else px + s
            pnl = (pos["dir"] * (fill - pos["e"]) * p.qty * spec.point_value
                    - 2 * spec.fee_per_side * p.qty)
            self.trades.append(Trade(
                symbol=spec.name, side="LONG" if pos["dir"] > 0 else "SHORT",
                entry_time=pos["t0"], entry_price=pos["e"], exit_time=t,
                exit_price=fill, qty=p.qty, exit_reason=reason,
                stop_price=pos.get("stop") or 0.0, target_price=pos.get("tgt") or 0.0,
                r_multiple=0.0, pnl_usd=pnl, scaled=False, day=str(pos["day"]),
                entry_atr_ticks=pos.get("atr_t", 0.0),
            ))
            pos = None
            return pnl

        for i in range(self.n):
            t = self.times[i]
            day = t.date()
            tod = t.time()

            if day != cur_day:
                if pos is not None:
                    equity += close_pos(self.closes[i - 1], self.times[i - 1],
                                         "SESSION_FLATTEN")
                cur_day = day
                day_pnl = 0.0
                halted = False
                pending = None

            # ── execute what the previous bar decided, at this bar's open ──
            if pending is not None:
                want = pending
                pending = None
                if pos is not None and (pos["dir"] != want or p.mode == "churn"):
                    pnl = close_pos(self.opens[i], t, "SIGNAL")
                    day_pnl += pnl
                    equity += pnl
                if want != 0 and pos is None:
                    fill = self.opens[i] + slip if want > 0 else self.opens[i] - slip
                    stop = tgt = None
                    if p.stop_ticks > 0:
                        d = p.stop_ticks * tick
                        stop = fill - d if want > 0 else fill + d
                    if p.target_ticks > 0:
                        d = p.target_ticks * tick
                        tgt = fill + d if want > 0 else fill - d
                    a = self.atr[i]
                    pos = {"dir": want, "e": fill, "t0": t, "bar": i, "day": day,
                            "stop": stop, "tgt": tgt,
                            "atr_t": (a / tick) if a else 0.0}

            # ── session flatten ────────────────────────────────────────────
            if tod >= p.flatten_time:
                if pos is not None:
                    pnl = close_pos(self.closes[i], t, "SESSION_FLATTEN")
                    day_pnl += pnl
                    equity += pnl
                continue

            # ── protective stop / target, checked intrabar ─────────────────
            if pos is not None and (pos["stop"] is not None or pos["tgt"] is not None):
                hi, lo = self.highs[i], self.lows[i]
                if pos["dir"] > 0:
                    hit_s = pos["stop"] is not None and lo <= pos["stop"]
                    hit_t = pos["tgt"] is not None and hi >= pos["tgt"]
                else:
                    hit_s = pos["stop"] is not None and hi >= pos["stop"]
                    hit_t = pos["tgt"] is not None and lo <= pos["tgt"]
                if hit_s:
                    pnl = close_pos(pos["stop"], t, "STOP")
                    day_pnl += pnl
                    equity += pnl
                elif hit_t:
                    pnl = close_pos(pos["tgt"], t, "TARGET", slipping=False)
                    day_pnl += pnl
                    equity += pnl

            if p.daily_loss_limit_usd > 0 and day_pnl <= -abs(p.daily_loss_limit_usd):
                if pos is not None:
                    pnl = close_pos(self.closes[i], t, "SESSION_FLATTEN")
                    day_pnl += pnl
                    equity += pnl
                halted = True
            if halted:
                continue

            # ── decide the position for the next bar ───────────────────────
            if not (p.entry_start <= tod < p.entry_end):
                continue
            want = self._desired(i)
            cur = pos["dir"] if pos is not None else 0
            if want != cur:
                self.signal_changes += 1
            if want != cur or p.mode == "churn":
                pending = want

        if pos is not None:
            close_pos(self.closes[-1], self.times[-1], "END_OF_DATA")

        return self.trades
