"""
Reddit WSB + financial news RSS sentiment engine.

No API keys required — uses Reddit's public JSON endpoint and
standard RSS feeds. Results are cached for 15 minutes.
"""

import re
import time
import logging
from typing import Dict, List, Set

import requests
import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log = logging.getLogger(__name__)

_REDDIT_URL = "https://www.reddit.com/r/wallstreetbets/hot.json?limit=100"
_HEADERS    = {"User-Agent": "MAI-TradingBot/1.0"}

_NEWS_FEEDS = [
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
]

_CACHE_TTL = 900.0   # 15 minutes
_TICKER_RE = re.compile(r'\$([A-Z]{2,5})\b|\b([A-Z]{2,5})\b')


class SentimentEngine:
    def __init__(self) -> None:
        self._sia        = SentimentIntensityAnalyzer()
        self._cache:      Dict[str, float] = {}
        self._cache_time: float            = 0.0

    # ── Public API ────────────────────────────────────────────────────

    def get_boost(self, symbol: str) -> float:
        """Return cached sentiment multiplier for one symbol. 1.0 = neutral."""
        return self._cache.get(symbol, 1.0)

    def refresh(self, symbols: List[str]) -> Dict[str, float]:
        """
        Fetch WSB posts + news headlines, score each symbol.
        Returns {symbol: boost_multiplier} and updates the internal cache.
        Cache is valid for 15 minutes; stale calls return the prior result.
        """
        if time.time() - self._cache_time < _CACHE_TTL:
            return self._cache

        symbol_set = set(symbols)
        scores: Dict[str, List[float]] = {s: [] for s in symbols}

        self._fetch_wsb(symbol_set, scores)
        self._fetch_news(symbol_set, scores)

        result: Dict[str, float] = {}
        for sym in symbols:
            vals = scores.get(sym, [])
            avg  = sum(vals) / len(vals) if vals else 0.0
            # compound in [-1, 1]; boost = 1.0 + 0.5*avg  → [0.5, 1.5]
            result[sym] = max(0.5, min(2.0, 1.0 + 0.5 * avg))

        self._cache      = result
        self._cache_time = time.time()
        log.info("Sentiment refreshed — %d symbols scored", len(symbols))
        return result

    # ── Private helpers ───────────────────────────────────────────────

    def _fetch_wsb(self, symbol_set: Set[str], scores: Dict[str, List[float]]) -> None:
        try:
            resp = requests.get(_REDDIT_URL, headers=_HEADERS, timeout=10)
            if resp.status_code != 200:
                return
            posts = resp.json().get("data", {}).get("children", [])
            for post in posts:
                pd    = post.get("data", {})
                text  = f"{pd.get('title', '')} {pd.get('selftext', '')}"
                self._score_text(text, symbol_set, scores)
        except Exception as exc:
            log.debug("WSB fetch error: %s", exc)

    def _fetch_news(self, symbol_set: Set[str], scores: Dict[str, List[float]]) -> None:
        for url in _NEWS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:40]:
                    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
                    self._score_text(text, symbol_set, scores)
            except Exception as exc:
                log.debug("News feed error (%s): %s", url, exc)

    def _score_text(
        self, text: str, symbol_set: Set[str], scores: Dict[str, List[float]]
    ) -> None:
        mentioned = self._extract_tickers(text, symbol_set)
        if not mentioned:
            return
        compound = self._sia.polarity_scores(text)["compound"]
        for sym in mentioned:
            scores[sym].append(compound)

    def _extract_tickers(self, text: str, symbol_set: Set[str]) -> List[str]:
        found: List[str] = []
        for m in _TICKER_RE.finditer(text):
            ticker = m.group(1) or m.group(2)
            if ticker and ticker in symbol_set:
                found.append(ticker)
        return list(set(found))
