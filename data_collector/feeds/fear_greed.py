"""Fear & Greed Index feed from alternative.me.

Fetches the Crypto Fear & Greed Index (0-100) from the public API.
"""

from datetime import datetime, timezone

from data_collector.feeds.base_feed import SupplementaryFeed
from logging_config import get_logger

logger = get_logger("data_collector.feeds.fear_greed")

# URL for the alternative.me Fear & Greed API
FEAR_GREED_API_URL = "https://api.alternative.me/fng/"


class FearGreedFeed(SupplementaryFeed):
    """Crypto Fear & Greed Index from alternative.me.

    Returns a value from 0 (extreme fear) to 100 (extreme greed).
    Free API, no key required, daily resolution.
    """

    def name(self) -> str:
        return "fear_greed_index"

    def source(self) -> str:
        return "alternative.me"

    def resolution(self) -> str:
        return "daily"

    def requires_api_key(self) -> bool:
        return False

    def estimated_monthly_cost(self) -> float:
        return 0.0

    def fetch(self) -> list[dict]:
        """Fetch the latest Fear & Greed Index value.

        Uses urllib to avoid adding requests as a dependency.

        Returns:
            List with a single dict containing the latest index value.
        """
        import json
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(
                FEAR_GREED_API_URL,
                headers={"User-Agent": "agentic-quant-system/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if "data" not in data or not data["data"]:
                logger.warning("No data in Fear & Greed API response")
                return []

            entry = data["data"][0]
            value = int(entry.get("value", 0))
            ts_unix = int(entry.get("timestamp", 0))

            if ts_unix > 0:
                timestamp = datetime.fromtimestamp(
                    ts_unix, tz=timezone.utc
                ).isoformat()
            else:
                timestamp = datetime.now(timezone.utc).isoformat()

            classification = entry.get("value_classification", "")

            return [
                {
                    "feed_name": self.name(),
                    "timestamp": timestamp,
                    "value": value,
                    "source": self.source(),
                    "metadata": {"classification": classification},
                }
            ]

        except urllib.error.URLError as exc:
            logger.error("Failed to fetch Fear & Greed Index: %s", exc)
            return []
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Error parsing Fear & Greed response: %s", exc)
            return []
