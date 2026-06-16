#!/usr/bin/env python3
"""
Agentic Trading Bot — Robinhood

Strategy:
  Momentum (first 15-min window only):
    - Scan S&P 500 + Robinhood top-mover feeds each cycle
    - Buy any stock with >= 1% gain vs. previous close
    - Skip if bid-ask spread > 0.25% (fees/spread would erase profit)
    - Immediately place a limit sell at entry + 0.5%
    - Max 3 concurrent momentum positions; 10% of buying power each

  Large Mover (>= 3% gain, first 15-min window only):
    - Allocate exactly 5% of start-of-day account value
    - Place limit sell (take-profit) at entry + 10%
    - Place stop-limit sell (stop-loss) at entry - 5%  (limit 1% below stop)

  Risk management:
    - Check portfolio value every 15 minutes
    - If total daily loss >= 15% of start-of-day value, halt all new entries
    - Stop-loss and take-profit orders remain live for existing positions

Run:
    cp .env.example .env   # fill in credentials
    pip install -r requirements.txt
    python trading_bot.py

First run will prompt for MFA; subsequent runs use the cached session.
"""

import os
import sys
import time
import logging
import datetime
from typing import Dict, List, Optional, Tuple

import pytz
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

ACCOUNT_NUMBER: str = os.environ["RH_ACCOUNT_NUMBER"]

MARKET_TZ = pytz.timezone("America/New_York")
MARKET_OPEN = (9, 30)   # (hour, minute) ET
MARKET_CLOSE = (16, 0)

# How long to look for new entries after the open
TRADING_WINDOW_MINUTES: int = 15
# How often the main loop runs
UPDATE_INTERVAL_SECONDS: int = 15 * 60

# ── Momentum strategy ────────────────────────────────────────────────────────
MOMENTUM_GAIN_THRESHOLD: float = 0.01     # Buy if stock up >= 1% vs prev close
MOMENTUM_SELL_TARGET: float = 0.005       # Limit sell at entry + 0.5%
MAX_SPREAD_PCT: float = 0.0025            # Skip entry if (ask-bid)/ask > 0.25%
MOMENTUM_ALLOC_PCT: float = 0.10          # 10% of buying power per trade
MAX_MOMENTUM_POSITIONS: int = 3

# ── Large-mover strategy ─────────────────────────────────────────────────────
LARGE_MOVER_THRESHOLD: float = 0.03       # "Large" = up >= 3%
LARGE_MOVER_ALLOC_PCT: float = 0.05       # 5% of start-of-day account value
LARGE_MOVER_TAKE_PROFIT: float = 0.10     # Limit sell at entry + 10%
LARGE_MOVER_STOP_PCT: float = 0.05        # Stop trigger at entry - 5%
LARGE_MOVER_STOP_LIMIT_OFFSET: float = 0.01  # Limit = stop_price * (1 - 1%)
MAX_LARGE_MOVER_POSITIONS: int = 1         # One large-mover bet per day

# ── Risk ─────────────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT: float = 0.15        # Halt if down >= 15% of start value
MIN_POSITION_USD: float = 1.00            # Smallest trade size to bother with

# ─── Bot State ───────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.start_of_day_value: float = 0.0
        self.trading_halted: bool = False
        # symbol -> {qty, entry_price, buy_order_id, sell_order_id, target_price}
        self.momentum_positions: Dict[str, dict] = {}
        # symbol -> {qty, entry_price, buy_order_id, tp_order_id, sl_order_id, ...}
        self.large_mover_positions: Dict[str, dict] = {}

_state = BotState()

# ─── Robinhood import (lazy so we can unit-test config without credentials) ──

def _rh():
    import robin_stocks.robinhood as rs
    return rs


# ─── Authentication ──────────────────────────────────────────────────────────

def login():
    rs = _rh()
    username = os.environ["RH_USERNAME"]
    password = os.environ["RH_PASSWORD"]
    rs.login(
        username,
        password,
        expiresIn=86400,
        store_session=True,
    )
    log.info("Authenticated with Robinhood")


# ─── Market time helpers ──────────────────────────────────────────────────────

def _now_et() -> datetime.datetime:
    return datetime.datetime.now(MARKET_TZ)


def is_market_open() -> bool:
    n = _now_et()
    if n.weekday() >= 5:
        return False
    open_dt = n.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_dt = n.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_dt <= n < close_dt


def is_in_trading_window() -> bool:
    """True only during the first TRADING_WINDOW_MINUTES after market open."""
    n = _now_et()
    open_dt = n.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    window_end = open_dt + datetime.timedelta(minutes=TRADING_WINDOW_MINUTES)
    return open_dt <= n < window_end


# ─── Portfolio / account helpers ─────────────────────────────────────────────

def get_current_value_and_bp() -> Tuple[float, float]:
    """Returns (total_portfolio_value, buying_power)."""
    rs = _rh()
    try:
        port = rs.load_portfolio_profile(account_number=ACCOUNT_NUMBER)
        value = float(port.get("market_value") or 0) + float(port.get("cash") or 0)
    except Exception as e:
        log.error(f"Failed to load portfolio profile: {e}")
        value = _state.start_of_day_value  # best guess

    try:
        acct = rs.load_account_profile(account_number=ACCOUNT_NUMBER)
        bp = float(acct.get("buying_power") or 0)
    except Exception as e:
        log.error(f"Failed to load account profile: {e}")
        bp = 0.0

    return value, bp


# ─── Quote helpers ───────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def get_quote(symbol: str) -> Optional[dict]:
    rs = _rh()
    try:
        q = rs.get_stock_quote_by_symbol(symbol)
        return q if q else None
    except Exception as e:
        log.debug(f"Quote error for {symbol}: {e}")
        return None


def pct_change(q: dict) -> Optional[float]:
    prev = _safe_float(q.get("previous_close"))
    last = _safe_float(q.get("last_trade_price"))
    if not prev or not last or prev == 0:
        return None
    return (last - prev) / prev


def spread_pct(q: dict) -> Optional[float]:
    ask = _safe_float(q.get("ask_price"))
    bid = _safe_float(q.get("bid_price"))
    if not ask or ask == 0:
        return None
    return (ask - bid) / ask


def ask_price(q: dict) -> Optional[float]:
    return _safe_float(q.get("ask_price"))


# ─── Scanner ─────────────────────────────────────────────────────────────────

def get_candidate_symbols() -> List[str]:
    """
    Build a scan universe from:
      1. Robinhood's curated 'top movers' list
      2. S&P 500 up-movers
    """
    rs = _rh()
    symbols: set = set()

    try:
        for item in (rs.get_top_movers_robin_hood() or []):
            sym = item.get("symbol", "")
            if sym:
                symbols.add(sym)
    except Exception as e:
        log.warning(f"Could not fetch RH top movers: {e}")

    try:
        for item in (rs.get_movers_sp500(direction="up") or []):
            sym = item.get("symbol", "")
            if sym:
                symbols.add(sym)
    except Exception as e:
        log.warning(f"Could not fetch S&P 500 up-movers: {e}")

    log.info(f"Scan universe: {len(symbols)} symbols")
    return list(symbols)


def scan_movers(symbols: List[str]) -> Tuple[List[dict], List[dict]]:
    """
    Returns (momentum_list, large_mover_list) sorted by % gain descending.

    momentum_list:    1% <= gain < 3%  AND  spread <= MAX_SPREAD_PCT
    large_mover_list: gain >= 3%
    """
    momentum: List[dict] = []
    large: List[dict] = []

    for sym in symbols:
        q = get_quote(sym)
        if not q:
            continue

        gain = pct_change(q)
        if gain is None or gain < MOMENTUM_GAIN_THRESHOLD:
            continue

        ask = ask_price(q)
        if not ask or ask <= 0:
            continue

        spread = spread_pct(q)
        if spread is None:
            continue

        entry = dict(symbol=sym, gain=gain, ask=ask, spread=spread)

        if gain >= LARGE_MOVER_THRESHOLD:
            large.append(entry)
        elif spread <= MAX_SPREAD_PCT:
            # Fee guard: spread cost must not erase the 0.5% profit target
            net = MOMENTUM_SELL_TARGET - spread
            if net > 0:
                momentum.append(entry)
            else:
                log.debug(f"Skip {sym}: spread {spread:.3%} erases momentum target")

    momentum.sort(key=lambda x: x["gain"], reverse=True)
    large.sort(key=lambda x: x["gain"], reverse=True)
    return momentum, large


# ─── Order helpers ───────────────────────────────────────────────────────────

def _fill_info(order_id: str, fallback_ask: float, fallback_qty: float) -> Tuple[float, float]:
    """Returns (filled_qty, avg_price) from a placed order."""
    rs = _rh()
    try:
        # Give Robinhood a moment to process the market order
        time.sleep(3)
        info = rs.get_stock_order_info(order_id)
        qty = float(info.get("filled_quantity") or 0)
        price = float(info.get("average_price") or fallback_ask)
        return qty, price
    except Exception:
        return fallback_qty, fallback_ask


def buy_market(symbol: str, dollar_amount: float, approx_ask: float) -> Optional[str]:
    rs = _rh()
    qty = round(dollar_amount / approx_ask, 6)
    if qty < 0.000001:
        log.warning(f"Skipping {symbol}: computed qty {qty} too small")
        return None
    log.info(f"BUY MARKET {symbol}  qty={qty:.6f}  (~${dollar_amount:.2f})")
    try:
        order = rs.order_buy_fractional_by_quantity(
            symbol,
            qty,
            account_number=ACCOUNT_NUMBER,
            timeInForce="gfd",
        )
        if order and order.get("id"):
            return order["id"]
        log.error(f"Buy order returned unexpected response for {symbol}: {order}")
        return None
    except Exception as e:
        log.error(f"Buy order exception for {symbol}: {e}")
        return None


def sell_limit(symbol: str, qty: float, limit: float) -> Optional[str]:
    rs = _rh()
    log.info(f"SELL LIMIT {symbol}  qty={qty:.6f}  limit=${limit:.4f}")
    try:
        # Robinhood fractional limit sells use sell_fractional_by_quantity
        order = rs.order_sell_fractional_by_quantity(
            symbol,
            qty,
            account_number=ACCOUNT_NUMBER,
            timeInForce="gtc",  # GTC so it stays live after the trading window
            limitPrice=limit,
        )
        if order and order.get("id"):
            return order["id"]
        log.error(f"Limit sell unexpected response for {symbol}: {order}")
        return None
    except Exception as e:
        log.error(f"Limit sell exception for {symbol}: {e}")
        return None


def sell_stop_limit(symbol: str, qty: float, stop: float, limit: float) -> Optional[str]:
    """
    Stop-limit sell: triggers when price hits `stop`, then places a limit at `limit`.
    NOTE: Robinhood does NOT support stop orders on fractional shares.
    If qty is fractional, this will gracefully fall back and log a warning.
    """
    rs = _rh()
    whole_qty = int(qty)  # stop orders require whole shares
    if whole_qty < 1:
        log.warning(
            f"Cannot place stop-limit for {symbol}: fractional qty {qty:.6f} "
            f"— monitor manually or via the Robinhood app."
        )
        return None

    log.info(f"STOP-LIMIT SELL {symbol}  qty={whole_qty}  stop=${stop:.4f}  limit=${limit:.4f}")
    try:
        order = rs.order_sell_stop_limit(
            symbol,
            whole_qty,
            limitPrice=limit,
            stopPrice=stop,
            account_number=ACCOUNT_NUMBER,
            timeInForce="gtc",
        )
        if order and order.get("id"):
            return order["id"]
        log.error(f"Stop-limit unexpected response for {symbol}: {order}")
        return None
    except Exception as e:
        log.error(f"Stop-limit exception for {symbol}: {e}")
        return None


# ─── Strategy: Momentum ───────────────────────────────────────────────────────

def run_momentum(candidate: dict, buying_power: float) -> float:
    """
    Enter a momentum position. Returns the estimated buying-power consumed.
    """
    sym = candidate["symbol"]
    ask = candidate["ask"]

    if sym in _state.momentum_positions:
        return 0.0
    if len(_state.momentum_positions) >= MAX_MOMENTUM_POSITIONS:
        log.info(f"Max momentum positions reached ({MAX_MOMENTUM_POSITIONS}), skip {sym}")
        return 0.0

    alloc = min(buying_power * MOMENTUM_ALLOC_PCT, buying_power)
    if alloc < MIN_POSITION_USD:
        return 0.0

    buy_id = buy_market(sym, alloc, ask)
    if not buy_id:
        return 0.0

    est_qty = round(alloc / ask, 6)
    filled_qty, avg_price = _fill_info(buy_id, ask, est_qty)

    if filled_qty <= 0:
        log.warning(f"Momentum buy for {sym} shows 0 fill — may still be pending")
        filled_qty = est_qty
        avg_price = ask

    target = round(avg_price * (1 + MOMENTUM_SELL_TARGET), 4)
    sell_id = sell_limit(sym, filled_qty, target)

    _state.momentum_positions[sym] = dict(
        qty=filled_qty,
        entry_price=avg_price,
        target_price=target,
        buy_order_id=buy_id,
        sell_order_id=sell_id,
    )
    log.info(
        f"[MOMENTUM] {sym}: {filled_qty:.6f} sh @ ${avg_price:.4f} "
        f"→ limit sell @ ${target:.4f} (+0.5%)"
    )
    return alloc


# ─── Strategy: Large Mover ────────────────────────────────────────────────────

def run_large_mover(candidate: dict, account_value: float, buying_power: float) -> float:
    """
    Enter a large-mover position. Returns estimated buying-power consumed.
    """
    sym = candidate["symbol"]
    ask = candidate["ask"]

    if sym in _state.large_mover_positions:
        return 0.0
    if len(_state.large_mover_positions) >= MAX_LARGE_MOVER_POSITIONS:
        log.info(f"Max large-mover positions reached, skip {sym}")
        return 0.0

    # 5% of start-of-day value, capped to available buying power
    alloc = min(_state.start_of_day_value * LARGE_MOVER_ALLOC_PCT, buying_power)
    if alloc < MIN_POSITION_USD:
        log.warning(f"Insufficient funds for large-mover position in {sym}: ${alloc:.2f}")
        return 0.0

    buy_id = buy_market(sym, alloc, ask)
    if not buy_id:
        return 0.0

    est_qty = round(alloc / ask, 6)
    filled_qty, avg_price = _fill_info(buy_id, ask, est_qty)

    if filled_qty <= 0:
        log.warning(f"Large-mover buy for {sym} shows 0 fill — may still be pending")
        filled_qty = est_qty
        avg_price = ask

    take_profit = round(avg_price * (1 + LARGE_MOVER_TAKE_PROFIT), 4)
    stop_trigger = round(avg_price * (1 - LARGE_MOVER_STOP_PCT), 4)
    stop_limit   = round(stop_trigger * (1 - LARGE_MOVER_STOP_LIMIT_OFFSET), 4)

    tp_id = sell_limit(sym, filled_qty, take_profit)
    sl_id = sell_stop_limit(sym, filled_qty, stop_trigger, stop_limit)

    _state.large_mover_positions[sym] = dict(
        qty=filled_qty,
        entry_price=avg_price,
        take_profit_price=take_profit,
        stop_price=stop_trigger,
        stop_limit_price=stop_limit,
        buy_order_id=buy_id,
        tp_order_id=tp_id,
        sl_order_id=sl_id,
    )
    log.info(
        f"[LARGE MOVER] {sym}: {filled_qty:.6f} sh @ ${avg_price:.4f} "
        f"| TP: ${take_profit:.4f} (+10%) "
        f"| SL: stop=${stop_trigger:.4f} / limit=${stop_limit:.4f} (-5%)"
    )
    if sl_id is None:
        log.warning(
            f"  ⚠  Stop-limit could not be placed for {sym} (fractional shares). "
            f"Monitor price manually; sell if it drops below ${stop_trigger:.4f}."
        )
    return alloc


# ─── Risk: daily loss guard ───────────────────────────────────────────────────

def loss_limit_breached(current_value: float) -> bool:
    if _state.start_of_day_value <= 0:
        return False
    loss_pct = (_state.start_of_day_value - current_value) / _state.start_of_day_value
    if loss_pct >= DAILY_LOSS_LIMIT_PCT:
        log.warning(
            f"DAILY LOSS LIMIT HIT: -{loss_pct:.1%} "
            f"(started=${_state.start_of_day_value:.2f}, now=${current_value:.2f}). "
            f"No new entries for the rest of the day."
        )
        return True
    return False


# ─── Main cycle ───────────────────────────────────────────────────────────────

def run_cycle():
    if not is_market_open():
        log.info("Market closed — skipping cycle.")
        return

    current_value, buying_power = get_current_value_and_bp()
    log.info(
        f"Portfolio: ${current_value:.2f}  |  Buying power: ${buying_power:.2f}  "
        f"|  Start-of-day: ${_state.start_of_day_value:.2f}"
    )

    if _state.trading_halted:
        log.info("Trading halted for the day (loss limit). Existing orders remain active.")
        return

    if loss_limit_breached(current_value):
        _state.trading_halted = True
        return

    if not is_in_trading_window():
        log.info("Outside 15-minute entry window. Monitoring open positions only.")
        return

    if buying_power < MIN_POSITION_USD:
        log.info(f"Buying power ${buying_power:.2f} too low for new entries.")
        return

    symbols = get_candidate_symbols()
    if not symbols:
        log.warning("No candidates returned by scanner.")
        return

    momentum_candidates, large_mover_candidates = scan_movers(symbols)
    log.info(
        f"Movers found: {len(large_mover_candidates)} large, "
        f"{len(momentum_candidates)} momentum"
    )

    # Large movers get priority (higher upside, sized at 5% of account)
    for c in large_mover_candidates[:MAX_LARGE_MOVER_POSITIONS]:
        spent = run_large_mover(c, current_value, buying_power)
        buying_power = max(0.0, buying_power - spent)

    # Momentum trades fill remaining budget
    for c in momentum_candidates:
        if buying_power < MIN_POSITION_USD:
            break
        spent = run_momentum(c, buying_power)
        buying_power = max(0.0, buying_power - spent)


# ─── Initialization ───────────────────────────────────────────────────────────

def initialize():
    login()
    value, _ = get_current_value_and_bp()
    _state.start_of_day_value = value
    log.info(f"Start-of-day account value: ${value:.2f}")

    log.info("Configuration summary:")
    log.info(f"  Entry window:        first {TRADING_WINDOW_MINUTES} min after open")
    log.info(f"  Update interval:     every {UPDATE_INTERVAL_SECONDS // 60} min")
    log.info(f"  Momentum threshold:  >= {MOMENTUM_GAIN_THRESHOLD:.0%} gain")
    log.info(f"  Momentum target:     +{MOMENTUM_SELL_TARGET:.1%} (limit sell)")
    log.info(f"  Max spread allowed:  {MAX_SPREAD_PCT:.2%} (fee guard)")
    log.info(f"  Momentum alloc:      {MOMENTUM_ALLOC_PCT:.0%} of buying power / trade")
    log.info(f"  Large-mover entry:   >= {LARGE_MOVER_THRESHOLD:.0%} gain")
    log.info(f"  Large-mover alloc:   {LARGE_MOVER_ALLOC_PCT:.0%} of account (${value * LARGE_MOVER_ALLOC_PCT:.2f})")
    log.info(f"  Take-profit:         +{LARGE_MOVER_TAKE_PROFIT:.0%}")
    log.info(f"  Stop-loss:           -{LARGE_MOVER_STOP_PCT:.0%} (stop-limit)")
    log.info(f"  Daily loss halt:     >= {DAILY_LOSS_LIMIT_PCT:.0%} drawdown from today's open")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Agentic Trading Bot starting up")
    log.info("=" * 60)

    initialize()

    while True:
        log.info("-" * 40)
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Shutdown requested — exiting.")
            break
        except Exception as e:
            log.error(f"Unhandled error in cycle: {e}", exc_info=True)

        log.info(f"Next cycle in {UPDATE_INTERVAL_SECONDS // 60} minutes.")
        time.sleep(UPDATE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
