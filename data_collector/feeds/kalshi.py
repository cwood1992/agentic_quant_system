"""Kalshi prediction market feed.

Fetches active markets from the Kalshi API. Requires a free Kalshi account
and API key (set KALSHI_API_KEY in .env). Disabled by default.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

from data_collector.feeds.base_feed import SupplementaryFeed
from logging_config import get_logger

logger = get_logger("data_collector.feeds.kalshi")

KALSHI_API_URL = "https://trading-api.kalshi.com/trade-api/v2/markets"

_RELEVANCE_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "crypto", "fed", "fomc",
    "rate cut", "rate hike", "cpi", "inflation", "tariff", "recession",
    "sec", "etf", "stablecoin",
]


class KalshiFeed(SupplementaryFeed):
    """Kalshi prediction market probabilities.

    Requires KALSHI_API_KEY environment variable. Returns [] if not set.
    """

    def __init__(self):
        self._db_path: str | None = None
        self._feed_config: dict = {}

    def name(self) -> str:
        return "kalshi"

    def source(self) -> str:
        return "kalshi.com"

    def resolution(self) -> str:
        return "hourly"

    def requires_api_key(self) -> bool:
        return True

    def estimated_monthly_cost(self) -> float:
        return 0.0

    def configure(self, db_path: str, config: dict | None = None) -> None:
        self._db_path = db_path
        self._feed_config = config or {}

    def fetch(self) -> list[dict]:
        """Fetch relevant Kalshi markets.

        Returns [] if KALSHI_API_KEY is not set.
        """
        api_key = os.environ.get("KALSHI_API_KEY", "")
        if not api_key:
            logger.debug("KALSHI_API_KEY not set — skipping Kalshi feed")
            return []

        try:
            req = urllib.request.Request(
                KALSHI_API_URL,
                headers={
                    "User-Agent": "agentic-quant-system/1.0",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            logger.warning("Failed to fetch Kalshi markets: %s", exc)
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Error parsing Kalshi response: %s", exc)
            return []

        markets = data.get("markets", [])
        now = datetime.now(timezone.utc)
        min_volume = self._feed_config.get("min_volume_usd", 10000)
        records = []

        for market in markets:
            title = market.get("title", "").lower()
            if not any(kw in title for kw in _RELEVANCE_KEYWORDS):
                continue

            yes_price = market.get("yes_bid")
            if yes_price is None:
                continue
            probability = float(yes_price)

            volume = float(market.get("volume") or 0)
            if volume < min_volume:
                continue

            market_id = "kalshi_" + str(market.get("ticker", market.get("id", "")))

            records.append({
                "feed_name": self.name(),
                "timestamp": now.isoformat(),
                "value": round(probability, 4),
                "source": self.source(),
                "metadata": {
                    "market_id": market_id,
                    "market_title": market.get("title", ""),
                    "probability": round(probability, 4),
                    "probability_24h_ago": None,
                    "delta_24h": None,
                    "delta_7d": None,
                    "volume_24h_usd": round(volume, 2),
                    "liquidity_usd": 0.0,
                    "category": "macro",
                    "resolution_date": market.get("close_time", "")[:10],
                },
            })

        logger.info("Kalshi: fetched %d relevant markets", len(records))
        return records
