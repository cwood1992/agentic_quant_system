"""Graveyard archiver for killed strategies.

Archives full strategy documentation (config, backtest results,
robustness results, kill reason) when strategies are moved to graveyard.
Provides summary views for agent digest consumption.
"""

import json
import os
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("strategies.graveyard")


class GraveyardArchiver:
    """Archives killed strategies and provides graveyard summaries.

    Args:
        db_path: Path to the SQLite database file.
        archive_dir: Directory for graveyard archive files.
    """

    def __init__(self, db_path: str, archive_dir: str = "strategies/graveyard"):
        self.db_path = db_path
        self.archive_dir = archive_dir

    def archive(
        self,
        strategy_id: str,
        reason: str,
        agent_id: str,
        namespace: str,
    ) -> str:
        """Archive a strategy into the graveyard with full documentation.

        Reads all strategy data from the database, writes a comprehensive
        archive JSON file, and logs the archival.

        Args:
            strategy_id: ID of the strategy to archive.
            reason: Reason for killing the strategy.
            agent_id: Agent that owned the strategy.
            namespace: Strategy namespace.

        Returns:
            Path to the archive file.
        """
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT strategy_id, agent_id, namespace, hypothesis_id,
                       stage, created_at, updated_at, config,
                       backtest_results, robustness_results, paper_results
                FROM strategy_registry
                WHERE strategy_id = ?
                """,
                (strategy_id,),
            ).fetchone()

            if row:
                strategy_data = dict(row)
                for field in ("config", "backtest_results", "robustness_results", "paper_results"):
                    if strategy_data[field]:
                        try:
                            strategy_data[field] = json.loads(strategy_data[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
            else:
                strategy_data = {
                    "strategy_id": strategy_id,
                    "agent_id": agent_id,
                    "namespace": namespace,
                }

        finally:
            conn.close()

        # Build archive document
        archive = {
            "strategy_id": strategy_id,
            "agent_id": agent_id,
            "namespace": namespace,
            "kill_reason": reason,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "strategy_data": strategy_data,
            "failure_type": self._classify_failure(reason, strategy_data),
        }

        # Write archive file
        os.makedirs(self.archive_dir, exist_ok=True)
        filename = f"{strategy_id}_archive.json"
        filepath = os.path.join(self.archive_dir, filename)

        with open(filepath, "w") as f:
            json.dump(archive, f, indent=2, default=str)

        logger.info("Archived strategy %s to %s", strategy_id, filepath)
        return filepath

    def get_graveyard_summary(self, agent_id: str) -> dict:
        """Get a summary of the graveyard for an agent.

        Args:
            agent_id: Agent whose graveyard to summarize.

        Returns:
            Dict with:
                total_count: Total strategies in graveyard.
                by_failure_type: Dict mapping failure type -> count.
                recent_5: List of the 5 most recently killed strategies.
        """
        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT strategy_id, namespace, config, updated_at
                FROM strategy_registry
                WHERE agent_id = ? AND stage = 'graveyard'
                ORDER BY updated_at DESC
                """,
                (agent_id,),
            ).fetchall()

            total_count = len(rows)
            by_failure_type: dict[str, int] = {}
            recent: list[dict] = []

            for r in rows:
                config = {}
                if r["config"]:
                    try:
                        config = json.loads(r["config"])
                    except (json.JSONDecodeError, TypeError):
                        pass

                reason = config.get("kill_reason", "unknown")
                failure_type = self._classify_failure(reason, config)
                by_failure_type[failure_type] = by_failure_type.get(failure_type, 0) + 1

                if len(recent) < 5:
                    recent.append({
                        "strategy_id": r["strategy_id"],
                        "namespace": r["namespace"],
                        "kill_reason": reason,
                        "killed_at": r["updated_at"],
                        "failure_type": failure_type,
                    })

            return {
                "total_count": total_count,
                "by_failure_type": by_failure_type,
                "recent_5": recent,
            }

        finally:
            conn.close()

    @staticmethod
    def _classify_failure(reason: str, strategy_data: dict) -> str:
        """Classify the failure type from the kill reason.

        Categories:
            - insufficient_trades: Not enough trades in backtest
            - poor_performance: Negative returns or low Sharpe
            - robustness_failure: Failed random entry or permutation tests
            - excessive_drawdown: Drawdown exceeded threshold
            - manual_kill: Killed by agent or operator
            - other: Uncategorized

        Args:
            reason: Kill reason string.
            strategy_data: Strategy data dict (may include backtest/robustness results).

        Returns:
            Failure type classification string.
        """
        reason_lower = reason.lower() if reason else ""

        if "insufficient" in reason_lower or "too few trades" in reason_lower:
            return "insufficient_trades"
        if "robustness" in reason_lower or "random entry" in reason_lower or "permutation" in reason_lower:
            return "robustness_failure"
        if "drawdown" in reason_lower:
            return "excessive_drawdown"
        if "performance" in reason_lower or "sharpe" in reason_lower or "return" in reason_lower:
            return "poor_performance"
        if "manual" in reason_lower or "operator" in reason_lower:
            return "manual_kill"

        return "other"
