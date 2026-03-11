"""Feed manager for supplementary data feeds.

Auto-discovers feed plugins, runs active feeds on schedule,
and inserts results into the supplementary_feeds table.
"""

import importlib
import inspect
import json
import os
import pkgutil
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger
from data_collector.feeds.base_feed import SupplementaryFeed

logger = get_logger("data_collector.feeds.feed_manager")


class FeedManager:
    """Manages supplementary data feed plugins.

    Auto-discovers SupplementaryFeed subclasses in the feeds/ directory,
    runs active feeds, and stores results in the database.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str, config: dict | None = None):
        self.db_path = db_path
        self.config = config or {}
        self.feeds: dict[str, SupplementaryFeed] = {}
        self._discover_feeds()

    # ------------------------------------------------------------------
    # Plugin discovery
    # ------------------------------------------------------------------

    def _discover_feeds(self) -> None:
        """Auto-discover SupplementaryFeed subclasses in the feeds package."""
        feeds_dir = os.path.dirname(os.path.abspath(__file__))
        package_name = "data_collector.feeds"

        for _importer, modname, _ispkg in pkgutil.iter_modules([feeds_dir]):
            if modname in ("base_feed", "feed_manager", "__init__"):
                continue
            try:
                module = importlib.import_module(f"{package_name}.{modname}")
                for _name, obj in inspect.getmembers(module, inspect.isclass):
                    if (
                        issubclass(obj, SupplementaryFeed)
                        and obj is not SupplementaryFeed
                    ):
                        instance = obj()
                        feed_cfg = self.config.get("feeds", {}).get(instance.name())
                        instance.configure(self.db_path, feed_cfg)
                        self.feeds[instance.name()] = instance
                        logger.info("Discovered feed: %s", instance.name())
            except Exception:
                logger.exception("Error discovering feed in module %s", modname)

    # ------------------------------------------------------------------
    # Run feeds
    # ------------------------------------------------------------------

    def run_active_feeds(self) -> dict[str, int]:
        """Run all active feeds and insert results into the database.

        Returns:
            Dict mapping feed_name -> number of records inserted.
        """
        conn = get_db(self.db_path)
        results: dict[str, int] = {}

        try:
            for feed_name, feed in self.feeds.items():
                # Check if feed is active in registry
                row = conn.execute(
                    "SELECT status FROM feed_registry WHERE feed_name = ?",
                    (feed_name,),
                ).fetchone()

                if row and row["status"] != "active":
                    continue

                try:
                    records = feed.fetch()
                    inserted = self._insert_records(conn, feed, records)
                    results[feed_name] = inserted
                    self._update_registry(conn, feed, success=True)
                    logger.info(
                        "Feed %s: inserted %d records", feed_name, inserted
                    )
                except Exception:
                    logger.exception("Error running feed %s", feed_name)
                    self._update_registry(conn, feed, success=False)
                    results[feed_name] = 0

            conn.commit()
        finally:
            conn.close()

        return results

    def run_single_feed(self, feed_name: str) -> int:
        """Run a single feed by name.

        Args:
            feed_name: Name of the feed to run.

        Returns:
            Number of records inserted.

        Raises:
            KeyError: If feed_name is not found.
        """
        if feed_name not in self.feeds:
            raise KeyError(f"Feed not found: {feed_name}")

        feed = self.feeds[feed_name]
        conn = get_db(self.db_path)
        try:
            records = feed.fetch()
            inserted = self._insert_records(conn, feed, records)
            self._update_registry(conn, feed, success=True)
            conn.commit()
            return inserted
        except Exception:
            self._update_registry(conn, feed, success=False)
            conn.commit()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Data request processing
    # ------------------------------------------------------------------

    def process_data_requests(self) -> list[str]:
        """Process pending data_request events from agents.

        Looks for events with event_type='data_request'. If the request
        asks for a feed that exists, activate and run it.

        Returns:
            List of feed names that were activated/run.
        """
        conn = get_db(self.db_path)
        activated: list[str] = []

        try:
            rows = conn.execute(
                """
                SELECT id, agent_id, payload
                FROM events
                WHERE event_type = 'data_request'
                ORDER BY timestamp ASC
                """
            ).fetchall()

            for row in rows:
                try:
                    request = json.loads(row["payload"])
                except (json.JSONDecodeError, TypeError):
                    continue

                if request.get("_processed"):
                    continue

                feed_name = request.get("feed_name", "")

                if feed_name in self.feeds:
                    # Ensure feed is registered and active
                    self._ensure_registered(conn, self.feeds[feed_name], row["agent_id"])
                    activated.append(feed_name)

                # Mark as processed
                conn.execute(
                    "UPDATE events SET payload = ? WHERE id = ?",
                    (json.dumps({**request, "_processed": True}), row["id"]),
                )

            conn.commit()
        finally:
            conn.close()

        return activated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _insert_records(
        conn, feed: SupplementaryFeed, records: list[dict]
    ) -> int:
        """Insert feed records into supplementary_feeds table."""
        count = 0
        for rec in records:
            metadata = rec.get("metadata")
            metadata_json = json.dumps(metadata) if metadata else None

            conn.execute(
                """
                INSERT INTO supplementary_feeds
                    (feed_name, timestamp, value, metadata, source, resolution)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.get("feed_name", feed.name()),
                    rec["timestamp"],
                    rec.get("value"),
                    metadata_json,
                    rec.get("source", feed.source()),
                    feed.resolution(),
                ),
            )
            count += 1
        return count

    @staticmethod
    def _update_registry(
        conn, feed: SupplementaryFeed, success: bool
    ) -> None:
        """Update (or insert) the feed_registry row."""
        now = datetime.now(timezone.utc).isoformat()

        existing = conn.execute(
            "SELECT feed_name FROM feed_registry WHERE feed_name = ?",
            (feed.name(),),
        ).fetchone()

        if existing:
            if success:
                conn.execute(
                    """
                    UPDATE feed_registry
                    SET last_fetch = ?, status = 'active'
                    WHERE feed_name = ?
                    """,
                    (now, feed.name()),
                )
            else:
                conn.execute(
                    """
                    UPDATE feed_registry
                    SET error_count = error_count + 1
                    WHERE feed_name = ?
                    """,
                    (feed.name(),),
                )
        else:
            conn.execute(
                """
                INSERT INTO feed_registry
                    (feed_name, feed_type, source, resolution, status, activated_at, last_fetch, error_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feed.name(),
                    "supplementary",
                    feed.source(),
                    feed.resolution(),
                    "active",
                    now,
                    now if success else None,
                    0 if success else 1,
                ),
            )

    @staticmethod
    def _ensure_registered(
        conn, feed: SupplementaryFeed, requested_by: str
    ) -> None:
        """Ensure a feed is registered and active in feed_registry."""
        now = datetime.now(timezone.utc).isoformat()

        existing = conn.execute(
            "SELECT feed_name FROM feed_registry WHERE feed_name = ?",
            (feed.name(),),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE feed_registry SET status = 'active' WHERE feed_name = ?",
                (feed.name(),),
            )
        else:
            conn.execute(
                """
                INSERT INTO feed_registry
                    (feed_name, feed_type, source, resolution, status,
                     requested_by, activated_at, error_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    feed.name(),
                    "supplementary",
                    feed.source(),
                    feed.resolution(),
                    "active",
                    requested_by,
                    now,
                ),
            )

    def get_available_feeds(self) -> list[dict]:
        """Return metadata for all discovered feeds.

        Returns:
            List of dicts with name, source, resolution, requires_api_key,
            estimated_monthly_cost.
        """
        return [
            {
                "name": feed.name(),
                "source": feed.source(),
                "resolution": feed.resolution(),
                "requires_api_key": feed.requires_api_key(),
                "estimated_monthly_cost": feed.estimated_monthly_cost(),
            }
            for feed in self.feeds.values()
        ]
