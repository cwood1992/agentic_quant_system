"""Abstract base class for supplementary data feeds.

Every feed plugin in the feeds/ directory should subclass
SupplementaryFeed and implement the required abstract methods.
"""

from abc import ABC, abstractmethod


class SupplementaryFeed(ABC):
    """Base class for all supplementary data feed plugins.

    Subclasses must implement name(), source(), resolution(), and fetch().
    Optional methods have sensible defaults.
    """

    @abstractmethod
    def name(self) -> str:
        """Return the unique feed name (e.g. 'fear_greed_index')."""
        ...

    @abstractmethod
    def source(self) -> str:
        """Return the data source identifier (e.g. 'alternative.me')."""
        ...

    @abstractmethod
    def resolution(self) -> str:
        """Return the data resolution (e.g. 'daily', 'hourly')."""
        ...

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Fetch the latest data points from the source.

        Returns:
            List of dicts, each with at minimum:
                feed_name: str
                timestamp: str (ISO-8601)
                value: float
                source: str
        """
        ...

    def requires_api_key(self) -> bool:
        """Whether this feed requires an API key. Default False."""
        return False

    def estimated_monthly_cost(self) -> float:
        """Estimated monthly cost in USD. Default 0.0 (free)."""
        return 0.0

    def configure(self, db_path: str, config: dict | None = None) -> None:
        """Optional hook called after discovery.

        Override to store db_path and per-feed config (e.g. for delta
        computation against historical records). No-op by default.
        """
        pass
