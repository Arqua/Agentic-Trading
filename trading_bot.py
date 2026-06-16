"""
trading_bot.py — Async main loop for the 4-strategy intraday trading bot.

Strategies
----------
  Module 1: ORBStrategy          — Opening Range Breakout  (9:30–9:45)
  Module 2: ReversalStrategy     — Fading the Open         (9:30–9:45)
  Module 3: GapAndGoStrategy     — Gap and Go              (9:30–9:45 window, fire after)
  Module 4: VWAPReversionStrategy — VWAP Mean Reversion    (post-9:45)

Run
---
  cp .env.example .env    # add RH_USERNAME, RH_PASSWORD, RH_ACCOUNT_NUMBER
  pip install -r requirements.txt
  python trading_bot.py
"""

import asyncio
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import pytz
from dotenv import load_dotenv

import robin_stocks.robinhood as rs

from watchlist import WATCHLIST, INVERSE_ETF_MAP, INVERSE_ETF_SYMBOLS
from indicators import calculate_rsi, calculate_vwap_bands
from strategies import ORBStrategy, ReversalStrategy, GapAndGoStrategy, VWAPReversionStrategy
from execution import ExecutionEngine
from risk_manager import RiskManager
from logger import (
    log, log_phase, log_signal, log_order, log_fill,
    log_cash, log_pnl, log_halt, log_error,
)
import trade_db
from sentiment import SentimentEngine

load_dotenv()

# ── GUI-controllable stop event ───────────────────────────────────────────────
# Set this event to request a clean shutdown from outside (e.g. the GUI).
BOT_STOP: threading.Event = threading.Event()

# Shared state dict for the GUI to poll (written by bot thread, read by GUI).
BOT_STATE: Dict = {
    "running":   False,
    "strategy":  "—",
    "portfolio": 0.0,
    "status":    "idle",
}

# ─── Constants ────────────────────────────────────────────────────────────────

ACCOUNT_NUMBER:          str   = os.environ["RH_ACCOUNT_NUMBER"]
POLL_INTERVAL_SECONDS:   int   = 30    # polling cadence during ORB window
POST_ORB_POLL_SECONDS:   int   = 60    # polling cadence post 9:45
MAX_DAILY_LOSS_PCT:      float = 0.15  # halt if down >= 15%

MARKET_TZ    = pytz.timezone("America/New_York")
ORB_OPEN_H   = 9
ORB_OPEN_M   = 30
ORB_FREEZE_H = 9
ORB_FREEZE_M = 45
MARKET_CLOSE_H = 16
MARKET_CLOSE_M = 0

# Thread pool shared with ExecutionEngine internals
_executor = ThreadPoolExecutor(max_workers=10)

# ─── Module-level state ───────────────────────────────────────────────────────

# RSI values computed at 9:45 and refreshed each post-ORB scan
rsi_cache: Dict[str, float] = {}

# Start-of-day portfolio value for loss-limit comparison
start_of_day_value: float = 0.0

# Strategy singletons
orb_strat      = ORBStrategy()
reversal_strat = ReversalStrategy()
gap_go_strat   = GapAndGoStrategy()
vwap_rev       = VWAPReversionStrategy()

# Sentiment engine
sentiment_eng = SentimentEngine()

# Execution & risk management
risk_mgr  = RiskManager(account_number=ACCOUNT_NUMBER)
exec_eng  = ExecutionEngine(account_number=ACCOUNT_NUMBER, risk_mgr=risk_mgr)


# ─── Time helpers ─────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(MARKET_TZ)


def _is_market_open() -> bool:
    n = _now_et()
    if n.weekday() >= 5:
        return False
    open_dt  = n.replace(hour=ORB_OPEN_H,   minute=ORB_OPEN_M,   second=0, microsecond=0)
    close_dt = n.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_dt <= n < close_dt


def _in_orb_window() -> bool:
    """True between 9:30 and 9:45 ET."""
    n = _now_et()
    open_dt   = n.replace(hour=ORB_OPEN_H,   minute=ORB_OPEN_M,   second=0, microsecond=0)
    freeze_dt = n.replace(hour=ORB_FREEZE_H,  minute=ORB_FREEZE_M, second=0, microsecond=0)
    return open_dt <= n < freeze_dt


def _past_orb() -> bool:
    """True at or after 9:45 ET."""
    n = _now_et()
    freeze_dt = n.replace(hour=ORB_FREEZE_H, minute=ORB_FREEZE_M, second=0, microsecond=0)
    return n >= freeze_dt


def _is_orb_open_minute() -> bool:
    """True only during the 9:30 AM minute (first minute of session)."""
    n = _now_et()
    return n.hour == ORB_OPEN_H and n.minute == ORB_OPEN_M


def _today_str() -> str:
    return date.today().isoformat()   # "YYYY-MM-DD"


# ─── Authentication ───────────────────────────────────────────────────────────

def login() -> None:
    username = os.environ["RH_USERNAME"]
    password = os.environ["RH_PASSWORD"]
    rs.login(username, password, expiresIn=86400, store_session=True)
    log.info("Authenticated with Robinhood")


# ─── Data fetchers ────────────────────────────────────────────────────────────

async def fetch_batch_quotes(symbols: List[str]) -> Dict[str, dict]:
    """
    Fetch real-time quotes for a list of symbols in one batch call.

    Parameters
    ----------
    symbols : List of ticker strings.

    Returns
    -------
    dict keyed by symbol, value is the raw quote dict from robin_stocks.
    Returns an empty dict on total failure; individual missing quotes are
    silently skipped.
    """
    loop = asyncio.get_event_loop()
    try:
        raw: list = await loop.run_in_executor(
            _executor,
            lambda: rs.get_quotes(symbols) or [],
        )
    except Exception as exc:
        log_error("fetch_batch_quotes", exc)
        return {}

    result: Dict[str, dict] = {}
    if not raw:
        return result

    for i, q in enumerate(raw):
        if not q:
            continue
        sym = q.get("symbol") or (symbols[i] if i < len(symbols) else None)
        if sym:
            result[sym] = q

    return result


async def fetch_intraday_bars(symbol: str, interval: str = "5minute") -> List[dict]:
    """
    Fetch today's regular-session intraday bars for a symbol.

    Parameters
    ----------
    symbol   : Ticker string.
    interval : Bar interval ('5minute', '10minute', etc.).

    Returns
    -------
    List of bar dicts filtered to today's regular session.
    Each dict has keys: begins_at, open_price, close_price, high_price,
    low_price, volume, session, interpolated, symbol.
    """
    loop = asyncio.get_event_loop()
    today = _today_str()
    try:
        bars: list = await loop.run_in_executor(
            _executor,
            lambda: rs.get_stock_historicals(
                symbol,
                interval=interval,
                span="day",
                bounds="regular",
            ) or [],
        )
    except Exception as exc:
        log_error(f"fetch_intraday_bars({symbol})", exc)
        return []

    # Filter to today's regular-session bars only
    filtered = [
        b for b in bars
        if b
        and isinstance(b.get("begins_at"), str)
        and b["begins_at"].startswith(today)
        and b.get("session") == "reg"
    ]
    return filtered


async def fetch_15min_bars(symbol: str) -> List[dict]:
    """
    Fetch 15-minute bars spanning one week for RSI computation.

    We need a week of 15-min bars to seed the Wilder RSI with enough history
    (14 periods × 15 min = 3.5 hours; one week gives ~130 bars for a clean
    reading across sessions).

    Parameters
    ----------
    symbol : Ticker string.

    Returns
    -------
    List of bar dicts ordered oldest-first.
    """
    loop = asyncio.get_event_loop()
    try:
        bars: list = await loop.run_in_executor(
            _executor,
            lambda: rs.get_stock_historicals(
                symbol,
                interval="15minute",
                span="week",
                bounds="regular",
            ) or [],
        )
    except Exception as exc:
        log_error(f"fetch_15min_bars({symbol})", exc)
        return []

    return [b for b in bars if b]


# ─── Phase handlers ───────────────────────────────────────────────────────────

async def initialize_orb_window(quotes: Dict[str, dict]) -> None:
    """
    Called once at 9:30 AM.

    For each symbol:
      - Qualify Gap & Go (prev_close vs open_price)
      - Set Reversal open price
      - Seed ORB with the first quote price
    """
    log_phase("ORB_WINDOW_INIT", f"{len(quotes)} symbols quoted")

    for symbol, q in quotes.items():
        if symbol in INVERSE_ETF_SYMBOLS:
            continue

        try:
            prev_close  = float(q.get("previous_close") or 0)
            open_price  = float(q.get("last_trade_price") or q.get("ask_price") or 0)
        except (TypeError, ValueError):
            continue

        if open_price <= 0:
            continue

        # Reversal: record open price
        reversal_strat.set_open(symbol, open_price)

        # Gap & Go: register if gap >= 3%
        if prev_close > 0:
            gap_go_strat.qualify(symbol, prev_close, open_price)

        # ORB: seed first price
        orb_strat.update(symbol, open_price, in_window=True)

    active_gap_symbols = gap_go_strat.get_active_symbols()
    log.info("Gap&Go candidates at open: %d symbols — %s", len(active_gap_symbols), active_gap_symbols)


async def scan_orb_window(quotes: Dict[str, dict]) -> None:
    """
    Called every POLL_INTERVAL_SECONDS during 9:30–9:45.

    Updates ORB high/low and Gap & Go tracking for every quoted symbol.
    """
    now_str = _now_et().strftime("%H:%M:%S ET")
    log.debug("ORB scan tick  %s  symbols=%d", now_str, len(quotes))

    for symbol, q in quotes.items():
        if symbol in INVERSE_ETF_SYMBOLS:
            continue
        try:
            price = float(q.get("last_trade_price") or q.get("ask_price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        orb_strat.update(symbol, price, in_window=True)
        gap_go_strat.update(symbol, price)


async def initialize_post_orb(quotes: Dict[str, dict]) -> None:
    """
    Called once at 9:45 AM.

    1. Freeze ORB and Gap & Go boundaries for all symbols.
    2. Fetch RSI (15-min bars) and VWAP (5-min bars) concurrently in
       batches of 10 symbols.
    3. Cache RSI values in the module-level rsi_cache dict.
    4. Register VWAP bands with VWAPReversionStrategy.
    """
    global rsi_cache

    log_phase("POST_ORB_INIT", "freezing ORB + computing RSI/VWAP")

    # ── Freeze all symbols ───────────────────────────────────────────
    all_symbols = [s for s in WATCHLIST if s not in INVERSE_ETF_SYMBOLS]
    for symbol in all_symbols:
        orb_strat.freeze(symbol)
        gap_go_strat.freeze(symbol)

    # ── Batch fetch RSI + VWAP concurrently ─────────────────────────
    BATCH_SIZE = 10

    async def process_symbol_indicators(symbol: str) -> None:
        """Compute and store RSI and VWAP for one symbol."""
        # RSI from 15-min bars
        bars_15m = await fetch_15min_bars(symbol)
        if bars_15m:
            closes = []
            for b in bars_15m:
                try:
                    closes.append(float(b["close_price"]))
                except (KeyError, TypeError, ValueError):
                    pass
            if closes:
                import numpy as np
                rsi_val = calculate_rsi(np.array(closes))
                rsi_cache[symbol] = rsi_val
            else:
                rsi_cache[symbol] = 50.0
        else:
            rsi_cache[symbol] = 50.0

        # VWAP from 5-min intraday bars
        bars_5m = await fetch_intraday_bars(symbol, interval="5minute")
        if bars_5m:
            vwap, upper, lower = calculate_vwap_bands(bars_5m)
            if vwap > 0:
                vwap_rev.set_vwap(symbol, vwap, upper, lower)

    # Process in batches to avoid hammering the API
    for i in range(0, len(all_symbols), BATCH_SIZE):
        batch = all_symbols[i : i + BATCH_SIZE]
        tasks = [process_symbol_indicators(sym) for sym in batch]
        await asyncio.gather(*tasks)
        log.debug("post-ORB indicator batch %d/%d done", i // BATCH_SIZE + 1,
                  (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE)

    # ── Summary log ─────────────────────────────────────────────────
    rsi_values_computed = len([v for v in rsi_cache.values() if v != 50.0])
    log.info(
        "[POST_ORB] Indicators ready: RSI computed for %d/%d symbols  "
        "| Gap&Go active: %d",
        rsi_values_computed,
        len(all_symbols),
        len(gap_go_strat.get_active_symbols()),
    )


async def check_signals(quotes: Dict[str, dict]) -> None:
    """
    Called every POST_ORB_POLL_SECONDS after 9:45.

    For each symbol with a valid quote, evaluates all four strategies in
    priority order and calls exec_eng.execute_signal() when triggered.
    """
    for symbol, q in quotes.items():
        try:
            price = float(q.get("last_trade_price") or q.get("ask_price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        # ── Strategy 1: ORB ──────────────────────────────────────────
        if symbol not in INVERSE_ETF_SYMBOLS:
            boost = sentiment_eng.get_boost(symbol)
            sig = orb_strat.check_signal(symbol, price, INVERSE_ETF_MAP, sentiment_boost=boost)
            if sig is not None:
                # If INVERSE direction, reprice entry using current inverse quote
                if sig.direction == "INVERSE":
                    inv_q = quotes.get(sig.symbol)
                    if inv_q:
                        try:
                            inv_price = float(inv_q.get("last_trade_price") or
                                              inv_q.get("ask_price") or 0)
                            if inv_price > 0:
                                tp    = round(inv_price * (1 + orb_strat.TP_PCT), 4)
                                sl_st = round(inv_price * (1 - orb_strat.SL_PCT), 4)
                                sl_lm = round(sl_st * 0.995, 4)
                                from strategies import Signal as _Sig
                                sig = _Sig(
                                    strategy=sig.strategy,
                                    direction=sig.direction,
                                    symbol=sig.symbol,
                                    entry_price=inv_price,
                                    tp_price=tp,
                                    sl_stop=sl_st,
                                    sl_limit=sl_lm,
                                )
                        except (TypeError, ValueError):
                            pass
                log_signal(sig)
                order_id = await exec_eng.execute_signal(sig)
                if order_id:
                    alloc = round(price * (sig.entry_price / price if price else 1) * orb_strat.ALLOC_PCT, 2)
                    trade_db.record_trade(sig.symbol, sig.strategy, "BUY",
                                          0.0, sig.entry_price, order_id, alloc)

        # ── Strategy 2: Reversal ─────────────────────────────────────
        if symbol not in INVERSE_ETF_SYMBOLS:
            rsi = rsi_cache.get(symbol, 50.0)
            sig = reversal_strat.check_signal(symbol, price, rsi, INVERSE_ETF_MAP)
            if sig is not None:
                if sig.direction == "INVERSE":
                    inv_q = quotes.get(sig.symbol)
                    if inv_q:
                        try:
                            inv_price = float(inv_q.get("last_trade_price") or
                                              inv_q.get("ask_price") or 0)
                            if inv_price > 0:
                                tp    = round(inv_price * (1 + reversal_strat.TP_PCT), 4)
                                sl_st = round(inv_price * (1 - reversal_strat.SL_PCT), 4)
                                sl_lm = round(sl_st * 0.996, 4)
                                from strategies import Signal as _Sig
                                sig = _Sig(
                                    strategy=sig.strategy,
                                    direction=sig.direction,
                                    symbol=sig.symbol,
                                    entry_price=inv_price,
                                    tp_price=tp,
                                    sl_stop=sl_st,
                                    sl_limit=sl_lm,
                                )
                        except (TypeError, ValueError):
                            pass
                log_signal(sig)
                order_id = await exec_eng.execute_signal(sig)
                if order_id:
                    alloc = round(sig.entry_price * reversal_strat.ALLOC_PCT, 2)
                    trade_db.record_trade(sig.symbol, sig.strategy, "BUY",
                                          0.0, sig.entry_price, order_id, alloc)

        # ── Strategy 3: Gap and Go ───────────────────────────────────
        if symbol not in INVERSE_ETF_SYMBOLS:
            sig = gap_go_strat.check_signal(symbol, price)
            if sig is not None:
                log_signal(sig)
                order_id = await exec_eng.execute_signal(sig)
                if order_id:
                    alloc = round(sig.entry_price * gap_go_strat.ALLOC_PCT, 2)
                    trade_db.record_trade(sig.symbol, sig.strategy, "BUY",
                                          0.0, sig.entry_price, order_id, alloc)

        # ── Strategy 4: VWAP Reversion ───────────────────────────────
        if symbol not in INVERSE_ETF_SYMBOLS:
            sig = vwap_rev.check_signal(symbol, price)
            if sig is not None:
                log_signal(sig)
                order_id = await exec_eng.execute_signal(sig)
                if order_id:
                    alloc = round(sig.entry_price * vwap_rev.ALLOC_PCT, 2)
                    trade_db.record_trade(sig.symbol, sig.strategy, "BUY",
                                          0.0, sig.entry_price, order_id, alloc)


# ─── Portfolio value ──────────────────────────────────────────────────────────

async def _get_portfolio_value() -> float:
    """
    Fetch current portfolio value (market value + cash).

    Returns 0.0 on error.
    """
    loop = asyncio.get_event_loop()
    try:
        port = await loop.run_in_executor(
            _executor,
            lambda: rs.load_portfolio_profile(account_number=ACCOUNT_NUMBER),
        )
        if not port:
            return 0.0
        market_val = float(port.get("market_value") or 0)
        cash       = float(port.get("cash") or 0)
        return market_val + cash
    except Exception as exc:
        log_error("_get_portfolio_value", exc)
        return 0.0


async def check_daily_loss_limit() -> bool:
    """
    Check whether the daily loss limit has been breached.

    Returns
    -------
    bool
        True  → halt trading (loss >= MAX_DAILY_LOSS_PCT of start_of_day_value).
        False → continue.
    """
    global start_of_day_value
    if start_of_day_value <= 0:
        return False

    current_value = await _get_portfolio_value()
    if current_value <= 0:
        # Can't determine value; don't halt on bad data
        return False

    loss_pct = (start_of_day_value - current_value) / start_of_day_value
    if loss_pct >= MAX_DAILY_LOSS_PCT:
        log_halt(
            f"Daily loss limit breached: -{loss_pct:.1%}  "
            f"(start=${start_of_day_value:.2f}  now=${current_value:.2f})  "
            f"threshold={MAX_DAILY_LOSS_PCT:.0%}"
        )
        return True

    log.debug(
        "Loss check: current=$%.2f  start=$%.2f  drawdown=%.2f%%",
        current_value, start_of_day_value, loss_pct * 100,
    )
    return False


# ─── Main loop ────────────────────────────────────────────────────────────────

async def main() -> None:
    """
    Async entry point.

    Phase routing:
      Before 9:30       → wait
      9:30 (first tick) → initialize_orb_window
      9:30–9:45         → scan_orb_window  (every POLL_INTERVAL_SECONDS)
      9:45 (first tick) → initialize_post_orb
      9:45–16:00        → check_signals    (every POST_ORB_POLL_SECONDS)
      16:00             → market closed, exit loop
    """
    global start_of_day_value

    log.info("=" * 70)
    log.info("Agentic Trading Bot  (4-strategy intraday)")
    log.info("=" * 70)

    # ── Initialise trade database ────────────────────────────────────
    trade_db.init_db()

    # ── Authentication ───────────────────────────────────────────────
    login()

    # ── Start-of-day snapshot ────────────────────────────────────────
    start_of_day_value = await _get_portfolio_value()
    log.info("Start-of-day portfolio value: $%.2f", start_of_day_value)
    log_cash(risk_mgr.get_buying_power(), label="open")

    # ── Pre-fetch sentiment for all symbols ──────────────────────────
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor, lambda: sentiment_eng.refresh(WATCHLIST)
    )

    BOT_STATE["running"]   = True
    BOT_STATE["status"]    = "waiting"
    BOT_STATE["portfolio"] = start_of_day_value

    # ── Phase tracking ───────────────────────────────────────────────
    orb_initialized      = False
    post_orb_initialized = False
    trading_halted       = False
    last_sentiment_time  = 0.0

    while True:
        if BOT_STOP.is_set():
            log.info("BOT_STOP event set — shutting down cleanly.")
            break
        try:
            now_et = _now_et()

            # ── Market closed ────────────────────────────────────────
            if not _is_market_open():
                # If we've been running and the market just closed, exit cleanly
                if orb_initialized:
                    log.info("Market closed — shutting down.")
                    break
                log.info("Waiting for market open at 09:30 ET …")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # ── Daily loss guard ─────────────────────────────────────
            if not trading_halted and post_orb_initialized:
                trading_halted = await check_daily_loss_limit()
                if trading_halted:
                    log_phase("HALTED", "no new entries for remainder of session")

            # ── Fetch quotes for all 100 symbols ─────────────────────
            quotes = await fetch_batch_quotes(WATCHLIST)
            if not quotes:
                log.warning("Empty quote batch; retrying after sleep.")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            log.debug("Fetched %d/%d quotes", len(quotes), len(WATCHLIST))

            # ── Phase: ORB window (9:30–9:45) ────────────────────────
            if _in_orb_window():
                if not orb_initialized:
                    await initialize_orb_window(quotes)
                    orb_initialized = True
                    log_phase("ORB_WINDOW", "tracking high/low")
                else:
                    await scan_orb_window(quotes)

                # Check fills even during ORB (rare but possible from previous day)
                await exec_eng.check_fills()
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            # ── Phase: Post-ORB (9:45–16:00) ─────────────────────────
            elif _past_orb():
                if not post_orb_initialized:
                    await initialize_post_orb(quotes)
                    post_orb_initialized = True
                    log_phase("POST_ORB", "scanning signals")

                if not trading_halted:
                    BOT_STATE["strategy"] = "scanning"
                    await check_signals(quotes)

                await exec_eng.check_fills()

                bp = risk_mgr.get_buying_power()
                log_cash(bp, label="cycle")

                # Refresh sentiment every 15 minutes
                import time as _time
                now_ts = _time.time()
                if now_ts - last_sentiment_time > 900:
                    await loop.run_in_executor(
                        _executor, lambda: sentiment_eng.refresh(WATCHLIST)
                    )
                    last_sentiment_time = now_ts

                # Update shared state for GUI
                BOT_STATE["portfolio"] = await _get_portfolio_value()

                await asyncio.sleep(POST_ORB_POLL_SECONDS)

            else:
                # Fractional minute before 9:30 — shouldn't normally occur
                await asyncio.sleep(5)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — shutting down.")
            break
        except Exception as exc:
            log_error("main loop", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    BOT_STATE["running"] = False
    BOT_STATE["status"]  = "stopped"
    log.info("Bot stopped.  Execution log: execution_log.txt")


if __name__ == "__main__":
    asyncio.run(main())
