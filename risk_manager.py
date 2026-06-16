"""
risk_manager.py — Real-time buying-power and settlement tracking.

Keeps track of pending T+1 settlements so the bot never over-commits cash
that is not yet available to trade.
"""

import logging
from datetime import date, timedelta
from typing import Tuple

import robin_stocks.robinhood as rs

from logger import log_error

log = logging.getLogger("trading_bot")


class RiskManager:
    """
    Tracks buying power and pending cash settlements.

    Parameters
    ----------
    account_number : Robinhood account number string.
    """

    def __init__(self, account_number: str) -> None:
        self.account_number = account_number
        # { order_id: {"amount": float, "settle_date": date, "type": str} }
        self.pending_settlements: dict = {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_buying_power(self) -> float:
        """
        Fetch real-time buying power from Robinhood.

        Returns
        -------
        float
            Current buying power.  Returns 0.0 on any error and logs it.
        """
        try:
            profile = rs.load_account_profile(account_number=self.account_number)
            if profile is None:
                log.error("_get_buying_power: load_account_profile returned None")
                return 0.0
            bp = float(profile.get("buying_power") or 0)
            return bp
        except Exception as exc:
            log_error("_get_buying_power", exc)
            return 0.0

    def _next_settle_date(self) -> date:
        """
        Return the next business day (T+1) for settlement purposes.
        Skips weekends; does not account for market holidays.

        Returns
        -------
        date
            The settlement date.
        """
        today = date.today()
        candidate = today + timedelta(days=1)
        # Skip weekends: Saturday=5, Sunday=6
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_afford(self, order_usd: float) -> Tuple[bool, float]:
        """
        Check whether the account can fund an order of `order_usd`.

        Compares real-time buying power against the requested amount plus
        a $0.01 buffer to prevent edge-case over-drafts.

        Parameters
        ----------
        order_usd : Dollar amount of the proposed order.

        Returns
        -------
        (bool, float)
            (True, available_bp) if affordable.
            (False, available_bp) if not — also logs a BLOCKED warning.
        """
        available_bp = self._get_buying_power()
        required     = order_usd + 0.01  # $0.01 safety buffer

        if available_bp >= required:
            return (True, available_bp)

        log.warning(
            "[BLOCKED] Insufficient buying power: need $%.2f (incl. $0.01 buffer), "
            "have $%.2f",
            required, available_bp,
        )
        return (False, available_bp)

    def record_buy(self, order_id: str, amount_usd: float) -> None:
        """
        Record a buy order for pending T+1 settlement tracking.

        Parameters
        ----------
        order_id   : Robinhood order ID.
        amount_usd : Dollar amount of the buy.
        """
        settle_date = self._next_settle_date()
        self.pending_settlements[order_id] = {
            "amount":       amount_usd,
            "settle_date":  settle_date,
            "type":         "equity_buy",
        }
        log.debug(
            "[SETTLEMENT] order_id=%s  amount=$%.2f  settles=%s",
            order_id, amount_usd, settle_date.isoformat(),
        )

    def get_buying_power(self) -> float:
        """
        Public alias for _get_buying_power().

        Returns
        -------
        float
            Real-time buying power from Robinhood.
        """
        return self._get_buying_power()

    def get_pending_settlement_total(self) -> float:
        """
        Return the total dollar amount of unsettled buys still pending.

        Entries that have already settled (settle_date <= today) are
        pruned from the dict as a side effect.

        Returns
        -------
        float
            Sum of pending settlement amounts.
        """
        today = date.today()
        settled_ids = [
            oid for oid, rec in self.pending_settlements.items()
            if rec["settle_date"] <= today
        ]
        for oid in settled_ids:
            del self.pending_settlements[oid]

        return sum(rec["amount"] for rec in self.pending_settlements.values())
