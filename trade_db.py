"""SQLite trade history database stored in ~/.mai/trades.db."""

import sqlite3
import os
from datetime import date, datetime
from typing import Optional

DB_DIR  = os.path.expanduser("~/.mai")
DB_PATH = os.path.join(DB_DIR, "trades.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                symbol     TEXT    NOT NULL,
                strategy   TEXT    NOT NULL,
                side       TEXT    NOT NULL,
                quantity   REAL    NOT NULL,
                price      REAL    NOT NULL,
                order_id   TEXT,
                alloc_usd  REAL    NOT NULL DEFAULT 0,
                pnl        REAL
            )
        """)
        conn.commit()


def record_trade(
    symbol: str,
    strategy: str,
    side: str,
    quantity: float,
    price: float,
    order_id: str,
    alloc_usd: float,
) -> int:
    now   = datetime.utcnow().isoformat()
    today = date.today().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (timestamp, date, symbol, strategy, side, quantity, price, order_id, alloc_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, today, symbol, strategy, side, quantity, price, order_id, alloc_usd),
        )
        conn.commit()
        return cur.lastrowid


def record_pnl(order_id: str, pnl: float) -> None:
    with _conn() as conn:
        conn.execute("UPDATE trades SET pnl = ? WHERE order_id = ?", (pnl, order_id))
        conn.commit()


def get_recent_trades(limit: int = 100) -> list:
    """Return the most recent trades, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Return aggregate stats for today and all time."""
    today = date.today().isoformat()
    with _conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*), SUM(alloc_usd), COALESCE(SUM(pnl), 0) FROM trades"
        ).fetchone()
        day = conn.execute(
            "SELECT COUNT(*), SUM(alloc_usd), COALESCE(SUM(pnl), 0) FROM trades WHERE date = ?",
            (today,),
        ).fetchone()
    return {
        "total_trades": total[0] or 0,
        "value_total":  total[1] or 0.0,
        "pnl_total":    total[2] or 0.0,
        "trades_today": day[0] or 0,
        "value_today":  day[1] or 0.0,
        "pnl_today":    day[2] or 0.0,
    }
