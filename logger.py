"""
Central logging utility. All strategy triggers, order placements,
fills, and realized P&L are written to execution_log.txt with
millisecond-precise timestamps.
"""

import logging
import os

LOG_FILE = os.environ.get("TRADING_LOG_FILE", "execution_log.txt")

def _setup() -> logging.Logger:
    logger = logging.getLogger("TradingBot")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = logging.FileHandler(LOG_FILE, mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


log = _setup()


# ─── Structured event helpers ────────────────────────────────────────────────

def log_trigger(strategy: str, symbol: str, reason: str, **values):
    parts = " ".join(f"{k}={v}" for k, v in values.items())
    log.info(f"TRIGGER  | strategy={strategy:<12} symbol={symbol:<6} reason={reason} {parts}")


def log_order(
    action: str,
    strategy: str,
    symbol: str,
    qty: float,
    price: float,
    order_id: str,
    cash_available: float,
    alloc_usd: float,
):
    log.info(
        f"ORDER    | {action:<12} strategy={strategy:<12} symbol={symbol:<6} "
        f"qty={qty:.6f} price=${price:.4f} alloc=${alloc_usd:.2f} "
        f"cash_avail=${cash_available:.2f} order_id={order_id}"
    )


def log_bracket(symbol: str, tp_price: float, sl_stop: float, sl_limit: float,
                tp_order_id: str, sl_order_id: str):
    log.info(
        f"BRACKET  | symbol={symbol:<6} "
        f"tp=${tp_price:.4f} (id={tp_order_id}) "
        f"sl_stop=${sl_stop:.4f} sl_limit=${sl_limit:.4f} (id={sl_order_id})"
    )


def log_fill(strategy: str, symbol: str, fill_price: float, qty: float, order_id: str):
    log.info(
        f"FILL     | strategy={strategy:<12} symbol={symbol:<6} "
        f"fill=${fill_price:.4f} qty={qty:.6f} order_id={order_id}"
    )


def log_pnl(strategy: str, symbol: str, entry: float, exit_price: float, qty: float):
    pnl = (exit_price - entry) * qty
    pct = (exit_price - entry) / entry if entry else 0.0
    log.info(
        f"PNL      | strategy={strategy:<12} symbol={symbol:<6} "
        f"entry=${entry:.4f} exit=${exit_price:.4f} "
        f"qty={qty:.6f} realized=${pnl:+.4f} ({pct:+.3%})"
    )


def log_blocked(reason: str, symbol: str, order_usd: float, available: float):
    log.warning(
        f"BLOCKED  | symbol={symbol:<6} reason={reason} "
        f"order_usd=${order_usd:.2f} available=${available:.2f}"
    )


def log_halt(reason: str, start_value: float, current_value: float):
    loss_pct = (start_value - current_value) / start_value if start_value else 0.0
    log.warning(
        f"HALT     | reason={reason} "
        f"start=${start_value:.2f} current=${current_value:.2f} loss={loss_pct:.2%}"
    )


def log_phase(phase: str, detail: str = ""):
    log.info(f"PHASE    | {phase} {detail}")


def log_vwap(symbol: str, vwap: float, upper: float, lower: float):
    log.debug(f"VWAP     | symbol={symbol:<6} vwap=${vwap:.4f} +2sd=${upper:.4f} -2sd=${lower:.4f}")


def log_rsi(symbol: str, rsi: float):
    log.debug(f"RSI      | symbol={symbol:<6} rsi={rsi:.2f}")
