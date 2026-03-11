"""Polymarket prediction market feed.

Fetches active markets from the Polymarket Gamma API and returns
probability estimates for crypto, macro, and regulatory markets.
Free API, no authentication required.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from data_collector.feeds.base_feed import SupplementaryFeed
from logging_config import get_logger

logger = get_logger("data_collector.feeds.polymarket")

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false"

_RELEVANCE_KEYWORDS = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "avax",
               "crypto", "defi", "altcoin", "stablecoin", "web3"],
    "macro":  ["fed", "fomc", "federal reserve", "rate cut", "rate hike",
               "cpi", "inflation", "gdp", "recession", "tariff", "trade war",
               "dxy", "dollar"],
    "regulatory": ["sec", "etf", "regulation", "crypto ban", "stablecoin bill",
                   "bitcoin reserve", "cbdc"],
}


class PolymarketFeed(SupplementaryFeed):
    """Polymarket prediction market probabilities via Gamma API.

    Returns one record per relevant active market, with probability (0.0–1.0)
    and 24h/7d delta metadata computed from prior DB records.
    """

    def __init__(self):
        self._db_path: str | None = None
        self._feed_config: dict = {}

    def name(self) -> str:
        return "polymarket"

    def source(self) -> str:
        return "polymarket.com"

    def resolution(self) -> str:
        return "hourly"

    def requires_api_key(self) -> bool:
        return False

    def estimated_monthly_cost(self) -> float:
        return 0.0

    def configure(self, db_path: str, config: dict | None = None) -> None:
        self._db_path = db_path
        self._feed_config = config or {}

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch(self) -> list[dict]:
        """Fetch and filter active Polymarket markets.

        Returns:
            List of feed records with metadata including probability,
            delta_24h, delta_7d, volume, liquidity, and category.
        """
        try:
            req = urllib.request.Request(
                GAMMA_API_URL,
                headers={"User-Agent": "agentic-quant-system/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                markets = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            logger.warning("Failed to fetch Polymarket markets: %s", exc)
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Error parsing Polymarket response: %s", exc)
            return []

        if not isinstance(markets, list):
            logger.warning("Unexpected Polymarket response format: %s", type(markets))
            return []

        min_volume = self._feed_config.get("min_volume_usd", 10000)
        now = datetime.now(timezone.utc)
        records = []

        for market in markets:
            category = _classify(market.get("question", ""))
            if category is None:
                continue

            probability = _parse_probability(market.get("outcomePrices"))
            if probability is None:
                continue

            volume = float(market.get("volume") or 0)
            if volume < min_volume:
                continue

            market_id = str(market.get("id", ""))
            market_title = market.get("question", "")
            liquidity = float(market.get("liquidity") or 0)
            resolution_date = _parse_date(market.get("endDate", ""))

            delta_24h, delta_7d, prob_24h_ago = self._compute_deltas(
                market_id, probability, now
            )

            records.append({
                "feed_name": self.name(),
                "timestamp": now.isoformat(),
                "value": round(probability, 4),
                "source": self.source(),
                "metadata": {
                    "market_id": market_id,
                    "market_title": market_title,
                    "probability": round(probability, 4),
                    "probability_24h_ago": prob_24h_ago,
                    "delta_24h": delta_24h,
                    "delta_7d": delta_7d,
                    "volume_24h_usd": round(volume, 2),
                    "liquidity_usd": round(liquidity, 2),
                    "category": category,
                    "resolution_date": resolution_date,
                },
            })

        logger.info("Polymarket: fetched %d relevant markets", len(records))
        return records

    # ------------------------------------------------------------------
    # Delta computation
    # ------------------------------------------------------------------

    def _compute_deltas(
        self,
        market_id: str,
        current_prob: float,
        now: datetime,
    ) -> tuple[float | None, float | None, float | None]:
        """Query DB for prior probability values and compute deltas.

        Returns:
            (delta_24h, delta_7d, prob_24h_ago) — all None on first run
            or if DB is not configured.
        """
        if not self._db_path:
            return None, None, None

        try:
            from database.schema import get_db
            conn = get_db(self._db_path)
            try:
                cutoff_24h = (now - timedelta(hours=24)).isoformat()
                cutoff_7d = (now - timedelta(days=7)).isoformat()

                rows = conn.execute(
                    """
                    SELECT timestamp, value, metadata
                    FROM supplementary_feeds
                    WHERE feed_name = 'polymarket'
                      AND timestamp >= ?
                    ORDER BY timestamp ASC
                    """,
                    (cutoff_7d,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Could not query DB for Polymarket deltas: %s", exc)
            return None, None, None

        # Filter rows for this specific market_id
        prob_at_24h = None
        prob_at_7d = None
        prob_24h_ago = None

        for row in rows:
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if meta.get("market_id") != market_id:
                continue

            ts = row["timestamp"]
            prob = row["value"]

            if ts >= cutoff_24h and prob_at_24h is None:
                prob_at_24h = float(prob)
                prob_24h_ago = prob_at_24h
            if prob_at_7d is None:
                prob_at_7d = float(prob)

        delta_24h = None
        delta_7d = None

        if prob_at_24h is not None:
            delta_24h = round((current_prob - prob_at_24h) * 100, 1)  # pp
        if prob_at_7d is not None:
            delta_7d = round((current_prob - prob_at_7d) * 100, 1)  # pp

        return delta_24h, delta_7d, prob_24h_ago


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _classify(question: str) -> str | None:
    """Return category string if question matches relevance keywords, else None."""
    q = question.lower()
    for category, keywords in _RELEVANCE_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return None


def _parse_probability(outcome_prices) -> float | None:
    """Parse Polymarket outcomePrices field to a Yes probability (0.0–1.0)."""
    if outcome_prices is None:
        return None
    try:
        # outcomePrices is a JSON-encoded string: '["0.63", "0.37"]'
        if isinstance(outcome_prices, str):
            prices = json.loads(outcome_prices)
        else:
            prices = list(outcome_prices)
        if prices:
            return float(prices[0])
    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
        pass
    return None


def _parse_date(date_str: str) -> str | None:
    """Return ISO date string or None."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(
            date_str.replace("Z", "+00:00")
        ).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return date_str[:10] if len(date_str) >= 10 else None
