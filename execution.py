"""
execution.py — Async order execution engine.

Wraps all robin_stocks (rs) calls in a ThreadPoolExecutor so the async
event loop is never blocked by synchronous HTTP I/O.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, Optional

import robin_stocks.robinhood as rs

from logger import log_order, log_fill, log_cash, log_error
from strategies import Signal

log = logging.getLogger("trading_bot")

_executor = ThreadPoolExecutor(max_workers=10)


# ─── Position dataclass ───────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:          str
    strategy:        str
    qty:             float
    entry_price:     float
    buy_order_id:    str
    tp_price:        float
    sl_stop_price:   float
    sl_limit_price:  float
    alloc_usd:       float
    tp_order_id:     Optional[str] = field(default=None)
    sl_order_id:     Optional[str] = field(default=None)


# ─── ExecutionEngine ─────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Async execution engine.  All rs.* calls are dispatched to a thread pool
    so the event loop remains responsive.

    Parameters
    ----------
    account_number : Robinhood account number (from env).
    risk_mgr       : RiskManager instance for buying-power checks.
    """

    def __init__(self, account_number: str, risk_mgr) -> None:
        self.account_number = account_number
        self.risk_mgr       = risk_mgr
        self.positions:     Dict[str, Position] = {}
        self._pending_fills: set = set()   # buy order_ids awaiting fill confirmation

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_in_executor(self, func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor,
            lambda: func(*args, **kwargs),
        )

    async def _get_order_info(self, order_id: str) -> Optional[dict]:
        try:
            info = await self._run_in_executor(rs.get_stock_order_info, order_id)
            return info
        except Exception as exc:
            log_error(f"get_order_info({order_id})", exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_signal(self, signal: Signal) -> Optional[str]:
        """
        Place a limit buy order for the given signal.

        Steps:
          1. Ask the risk manager if we can afford the position.
          2. Compute share quantity from signal.entry_price.
          3. Place a GFD limit buy order via robin_stocks.
          4. Register the position; record the buy with the risk manager.

        Parameters
        ----------
        signal : Signal namedtuple from one of the strategy classes.

        Returns
        -------
        str | None
            The Robinhood order ID on success, None on failure.
        """
        # ── Determine allocation ─────────────────────────────────────
        # alloc_usd comes from the strategy's ALLOC_PCT * buying_power;
        # the bot's main loop pre-computes this and embeds it in the
        # signal's entry_price context.  Here we derive a safe dollar
        # amount from the risk manager's current buying power.
        try:
            current_bp = await self.get_buying_power()
        except Exception as exc:
            log_error("execute_signal: get_buying_power", exc)
            return None

        # Map strategy name to allocation fraction
        strategy_alloc = {
            "ORB":            0.02,
            "REVERSAL":       0.02,
            "GAP_AND_GO":     0.05,
            "VWAP_REVERSION": 0.03,
        }
        alloc_pct = strategy_alloc.get(signal.strategy, 0.02)
        alloc_usd = current_bp * alloc_pct

        # ── Affordability check ──────────────────────────────────────
        can_buy, available_bp = self.risk_mgr.can_afford(alloc_usd)
        if not can_buy:
            log.warning(
                "[BLOCKED] %s %s: need $%.2f, available $%.2f",
                signal.strategy, signal.symbol, alloc_usd, available_bp,
            )
            return None

        # ── Quantity ─────────────────────────────────────────────────
        if signal.entry_price <= 0:
            log.error("execute_signal: entry_price <= 0 for %s", signal.symbol)
            return None
        qty = alloc_usd / signal.entry_price

        # ── Place limit buy ──────────────────────────────────────────
        log.info(
            "[EXECUTE] %s %s  qty=%.6f  limit=%.4f  alloc=$%.2f",
            signal.strategy, signal.symbol, qty, signal.entry_price, alloc_usd,
        )
        try:
            order = await self._run_in_executor(
                rs.order_buy_limit,
                signal.symbol,
                qty,
                signal.entry_price,
                self.account_number,
                timeInForce="gfd",
            )
        except Exception as exc:
            log_error(f"order_buy_limit({signal.symbol})", exc)
            return None

        if not order or not order.get("id"):
            log.error("order_buy_limit returned no ID for %s: %s", signal.symbol, order)
            return None

        order_id = order["id"]

        # ── Register position ────────────────────────────────────────
        pos = Position(
            symbol=signal.symbol,
            strategy=signal.strategy,
            qty=qty,
            entry_price=signal.entry_price,
            buy_order_id=order_id,
            tp_price=signal.tp_price,
            sl_stop_price=signal.sl_stop,
            sl_limit_price=signal.sl_limit,
            alloc_usd=alloc_usd,
        )
        self.positions[signal.symbol] = pos
        self._pending_fills.add(order_id)

        self.risk_mgr.record_buy(order_id, alloc_usd)
        log_order(
            action="BUY_LIMIT",
            symbol=signal.symbol,
            qty=qty,
            price=signal.entry_price,
            order_id=order_id,
            order_type="limit",
            extra=f"strategy={signal.strategy}",
        )
        log_cash(available_bp - alloc_usd, label="post-buy estimate")
        return order_id

    async def place_bracket(self, symbol: str) -> None:
        """
        Place take-profit (TP) and stop-loss (SL) orders for an open position.

        TP:  GTC limit sell at tp_price
        SL:  GTC stop-limit sell (whole shares only; logs warning if fractional)

        Called automatically by check_fills() when a buy order is confirmed.
        """
        pos = self.positions.get(symbol)
        if not pos:
            log.warning("place_bracket: no position found for %s", symbol)
            return

        qty           = pos.qty
        tp_price      = pos.tp_price
        sl_stop_price = pos.sl_stop_price
        sl_limit_price = pos.sl_limit_price

        # ── Take-profit (limit sell) ─────────────────────────────────
        try:
            tp_order = await self._run_in_executor(
                rs.order_sell_limit,
                symbol,
                qty,
                tp_price,
                self.account_number,
                timeInForce="gtc",
            )
            tp_id = tp_order.get("id") if tp_order else None
        except Exception as exc:
            log_error(f"order_sell_limit TP ({symbol})", exc)
            tp_id = None

        pos.tp_order_id = tp_id
        log_order(
            action="SELL_TP",
            symbol=symbol,
            qty=qty,
            price=tp_price,
            order_id=tp_id,
            order_type="limit",
            extra=f"strategy={pos.strategy}",
        )

        # ── Stop-loss (stop-limit sell — whole shares only) ───────────
        whole_qty = int(qty)
        if whole_qty < 1:
            log.warning(
                "[SL_SKIP] %s: fractional qty=%.6f — Robinhood stop orders require "
                "whole shares. SL not placed; monitor position manually.",
                symbol, qty,
            )
            return

        try:
            sl_order = await self._run_in_executor(
                rs.order_sell_stop_limit,
                symbol,
                whole_qty,
                sl_limit_price,
                sl_stop_price,
                self.account_number,
                timeInForce="gtc",
            )
            sl_id = sl_order.get("id") if sl_order else None
        except Exception as exc:
            log_error(f"order_sell_stop_limit SL ({symbol})", exc)
            sl_id = None

        pos.sl_order_id = sl_id
        log_order(
            action="SELL_SL",
            symbol=symbol,
            qty=float(whole_qty),
            price=sl_stop_price,
            order_id=sl_id,
            order_type="stop_limit",
            extra=f"sl_limit={sl_limit_price:.4f}  strategy={pos.strategy}",
        )

    async def check_fills(self) -> None:
        """
        Poll all pending buy orders.  When a fill is confirmed, call
        place_bracket() to attach TP and SL orders.

        This is called on every main-loop cycle.
        """
        if not self._pending_fills:
            return

        filled_ids = set()
        for order_id in list(self._pending_fills):
            info = await self._get_order_info(order_id)
            if info is None:
                continue

            state     = info.get("state", "")
            filled_qty = float(info.get("filled_quantity") or 0)
            avg_price  = float(info.get("average_price") or 0)

            if state == "filled" and filled_qty > 0:
                filled_ids.add(order_id)
                # Find the matching position
                symbol = None
                for sym, pos in self.positions.items():
                    if pos.buy_order_id == order_id:
                        symbol = sym
                        # Update position with actual fill data
                        pos.qty          = filled_qty
                        pos.entry_price  = avg_price
                        break

                if symbol:
                    log_fill(symbol, filled_qty, avg_price, order_id)
                    log.info(
                        "[FILL CONFIRMED] %s  qty=%.6f  avg=%.4f — placing bracket",
                        symbol, filled_qty, avg_price,
                    )
                    await self.place_bracket(symbol)
                else:
                    log.warning(
                        "check_fills: order %s filled but no matching position found",
                        order_id,
                    )

            elif state in ("cancelled", "failed", "rejected"):
                filled_ids.add(order_id)
                log.warning(
                    "[ORDER %s] order_id=%s  qty=%.6f — removed from pending",
                    state.upper(), order_id, filled_qty,
                )

        self._pending_fills -= filled_ids

    async def get_buying_power(self) -> float:
        """
        Fetch current buying power from Robinhood.

        Returns
        -------
        float
            Current cash buying power, or 0.0 on error.
        """
        try:
            profile = await self._run_in_executor(
                rs.load_account_profile,
                account_number=self.account_number,
            )
            bp = float(profile.get("buying_power") or 0)
            log_cash(bp, label="check")
            return bp
        except Exception as exc:
            log_error("get_buying_power", exc)
            return 0.0
