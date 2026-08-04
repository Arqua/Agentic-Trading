"""
data.py — Historical ES / MES futures data fetcher (Yahoo Finance chart API).

Yahoo's continuous-front-month futures symbols (ES=F, MES=F) are used as a
stand-in for NinjaTrader's own historical feed (Kinetick / CQG / Continuum),
which is not reachable from this sandboxed environment. Ranges are capped by
Yahoo per-interval:
    5m   -> last 60 days
    1h   -> last 730 days
    1d   -> full history (ES=F back to ~2011 on this endpoint)

Bars are returned in the exchange's local time (America/New_York) and include
the overnight Globex session — callers that care about RTH-only behavior
(e.g. the ORB strategy) must filter by session themselves (see strategy.py).
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timezone
from typing import List, NamedTuple

import requests

from datetime import time as dtime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

CA_BUNDLE = "/root/.ccr/ca-bundle.crt"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
UA = {"User-Agent": "Mozilla/5.0 (compatible; backtest-fetcher/1.0)"}


class Bar(NamedTuple):
    ts: int          # unix seconds, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


def _chart_url(symbol: str, rng: str, interval: str) -> str:
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={rng}&interval={interval}"
    )


def fetch_bars(symbol: str, rng: str, interval: str, retries: int = 4) -> List[Bar]:
    """Fetch OHLCV bars for `symbol` from the Yahoo chart API."""
    url = _chart_url(symbol, rng, interval)
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=UA, timeout=20, verify=CA_BUNDLE)
            resp.raise_for_status()
            data = resp.json()
            result = data["chart"]["result"]
            if not result:
                err = data["chart"].get("error")
                raise RuntimeError(f"Yahoo chart error for {symbol}: {err}")
            r = result[0]
            ts_list = r["timestamp"]
            quote = r["indicators"]["quote"][0]
            bars = []
            for i, ts in enumerate(ts_list):
                o, h, l, c, v = (
                    quote["open"][i],
                    quote["high"][i],
                    quote["low"][i],
                    quote["close"][i],
                    quote["volume"][i],
                )
                if None in (o, h, l, c):
                    continue
                bars.append(Bar(ts, float(o), float(h), float(l), float(c), float(v or 0)))
            return bars
        except Exception as exc:  # noqa: BLE001 - retry loop
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {symbol} {rng}/{interval}: {last_exc}")


def _cache_path(symbol: str, interval: str) -> str:
    safe = symbol.replace("=", "_")
    return os.path.join(DATA_DIR, f"{safe}_{interval}.csv")


def save_csv(symbol: str, interval: str, bars: List[Bar]) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _cache_path(symbol, interval)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_utc", "iso_utc", "open", "high", "low", "close", "volume"])
        for b in bars:
            iso = datetime.fromtimestamp(b.ts, tz=timezone.utc).isoformat()
            w.writerow([b.ts, iso, b.open, b.high, b.low, b.close, b.volume])
    return path


def load_csv(symbol: str, interval: str) -> List[Bar]:
    path = _cache_path(symbol, interval)
    bars = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            bars.append(
                Bar(
                    int(row["ts_utc"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                )
            )
    return bars


def filter_rth(bars: List[Bar]) -> List[Bar]:
    """
    Keep only regular-trading-hours bars: weekdays, 09:30 <= t < 16:00 ET.

    Yahoo's futures feed includes the full Globex extended session (~72% of
    all intraday bars). The strategy is RTH-only, and — critically — its
    EMA/ATR filters must be computed over RTH bars alone to match a
    NinjaTrader chart running an RTH session template. Feeding ETH bars to
    the indicators (as backtest v1 did) both distorts the signals and, on
    early-close holidays, lets a position survive into the 18:00 ET Globex
    reopen. Everything downstream of data loading uses this filter.
    """
    out = []
    for b in bars:
        t = datetime.fromtimestamp(b.ts, tz=_ET)
        if t.weekday() >= 5:
            continue
        if dtime(9, 30) <= t.time() < dtime(16, 0):
            out.append(b)
    return out


def get_bars(symbol: str, rng: str, interval: str, refresh: bool = False) -> List[Bar]:
    """Cache-through accessor: read from local CSV cache unless refresh=True."""
    path = _cache_path(symbol, interval)
    if not refresh and os.path.exists(path):
        return load_csv(symbol, interval)
    bars = fetch_bars(symbol, rng, interval)
    save_csv(symbol, interval, bars)
    return bars


if __name__ == "__main__":
    for sym, rng, iv in [
        ("ES=F", "60d", "5m"),
        ("MES=F", "60d", "5m"),
        ("ES=F", "730d", "1h"),
        ("ES=F", "15y", "1d"),
    ]:
        bars = get_bars(sym, rng, iv, refresh=True)
        print(f"{sym:6s} {iv:3s}: {len(bars):6d} bars  "
              f"{datetime.fromtimestamp(bars[0].ts, tz=timezone.utc).date()} -> "
              f"{datetime.fromtimestamp(bars[-1].ts, tz=timezone.utc).date()}")
