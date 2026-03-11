"""Strategy registry for lifecycle management.

Manages strategy records in the database and the corresponding file
structure under strategies/. Enforces forward-only lifecycle transitions
(hypothesis -> backtest -> robustness -> paper -> live) with explicit
demote and kill paths.
"""

import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("strategies.registry")

# Valid lifecycle stages in forward order
LIFECYCLE_STAGES = ["hypothesis", "backtest", "robustness", "paper", "live"]

# Stage -> subdirectory mapping
STAGE_DIRS = {
    "hypothesis": "strategies/hypotheses",
    "backtest": "strategies/backtest",
    "robustness": "strategies/backtest",
    "paper": "strategies/paper",
    "live": "strategies/active",
    "graveyard": "strategies/graveyard",
}


class StrategyRegistry:
    """Manages strategy lifecycle and file organization.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def register(
        self,
        hypothesis_id: str,
        agent_id: str,
        namespace: str,
        config: dict,
    ) -> str:
        """Register a new strategy in hypothesis stage.

        Args:
            hypothesis_id: ID of the originating research note/hypothesis.
            agent_id: Agent that owns this strategy.
            namespace: Strategy namespace for scoping.
            config: Strategy configuration dict.

        Returns:
            The newly generated strategy_id.
        """
        strategy_id = f"{namespace}_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        conn = get_db(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO strategy_registry
                    (strategy_id, agent_id, namespace, hypothesis_id,
                     stage, created_at, updated_at, config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    agent_id,
                    namespace,
                    hypothesis_id,
                    "hypothesis",
                    now,
                    now,
                    json.dumps(config),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Ensure directory exists
        stage_dir = STAGE_DIRS["hypothesis"]
        os.makedirs(stage_dir, exist_ok=True)

        logger.info(
            "Registered strategy %s (agent=%s, namespace=%s)",
            strategy_id,
            agent_id,
            namespace,
        )
        return strategy_id

    def advance(self, strategy_id: str, new_stage: str) -> None:
        """Advance a strategy to the next lifecycle stage.

        Enforces forward-only transitions. Moving from one stage to
        a non-adjacent forward stage is allowed (e.g. hypothesis -> backtest).

        Args:
            strategy_id: The strategy to advance.
            new_stage: Target stage (must be forward from current).

        Raises:
            ValueError: If the transition is not forward-only.
            KeyError: If strategy_id is not found.
        """
        if new_stage not in LIFECYCLE_STAGES:
            raise ValueError(f"Invalid stage: {new_stage}")

        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT stage, namespace FROM strategy_registry WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()

            if not row:
                raise KeyError(f"Strategy not found: {strategy_id}")

            current_stage = row["stage"]

            if current_stage not in LIFECYCLE_STAGES:
                raise ValueError(
                    f"Cannot advance from stage '{current_stage}' "
                    f"(strategy may be in graveyard)"
                )

            current_idx = LIFECYCLE_STAGES.index(current_stage)
            new_idx = LIFECYCLE_STAGES.index(new_stage)

            if new_idx <= current_idx:
                raise ValueError(
                    f"Cannot move backward: {current_stage} -> {new_stage}"
                )

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE strategy_registry
                SET stage = ?, updated_at = ?
                WHERE strategy_id = ?
                """,
                (new_stage, now, strategy_id),
            )
            conn.commit()

            # Move files between directories
            self._move_strategy_files(strategy_id, current_stage, new_stage)

            logger.info(
                "Advanced strategy %s: %s -> %s",
                strategy_id,
                current_stage,
                new_stage,
            )

        finally:
            conn.close()

    def demote(self, strategy_id: str, reason: str) -> None:
        """Demote a live strategy back to paper.

        Only valid for strategies currently in 'live' stage.

        Args:
            strategy_id: The strategy to demote.
            reason: Reason for demotion.

        Raises:
            ValueError: If strategy is not in 'live' stage.
            KeyError: If strategy_id is not found.
        """
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT stage, config FROM strategy_registry WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()

            if not row:
                raise KeyError(f"Strategy not found: {strategy_id}")

            if row["stage"] != "live":
                raise ValueError(
                    f"Can only demote live strategies, current stage: {row['stage']}"
                )

            # Update config with demotion reason
            config = json.loads(row["config"]) if row["config"] else {}
            config["demote_reason"] = reason
            config["demoted_at"] = datetime.now(timezone.utc).isoformat()

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE strategy_registry
                SET stage = 'paper', updated_at = ?, config = ?
                WHERE strategy_id = ?
                """,
                (now, json.dumps(config), strategy_id),
            )
            conn.commit()

            self._move_strategy_files(strategy_id, "live", "paper")

            logger.info(
                "Demoted strategy %s: live -> paper (reason: %s)",
                strategy_id,
                reason,
            )

        finally:
            conn.close()

    def kill(self, strategy_id: str, reason: str) -> None:
        """Kill a strategy and move it to graveyard.

        Valid from any lifecycle stage.

        Args:
            strategy_id: The strategy to kill.
            reason: Reason for killing.

        Raises:
            KeyError: If strategy_id is not found.
        """
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT stage, config FROM strategy_registry WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()

            if not row:
                raise KeyError(f"Strategy not found: {strategy_id}")

            current_stage = row["stage"]
            config = json.loads(row["config"]) if row["config"] else {}
            config["kill_reason"] = reason
            config["killed_at"] = datetime.now(timezone.utc).isoformat()
            config["killed_from_stage"] = current_stage

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE strategy_registry
                SET stage = 'graveyard', updated_at = ?, config = ?
                WHERE strategy_id = ?
                """,
                (now, json.dumps(config), strategy_id),
            )
            conn.commit()

            self._move_strategy_files(strategy_id, current_stage, "graveyard")

            logger.info(
                "Killed strategy %s: %s -> graveyard (reason: %s)",
                strategy_id,
                current_stage,
                reason,
            )

        finally:
            conn.close()

    def get_strategies_by_stage(
        self, agent_id: str, stage: str
    ) -> list[dict]:
        """Get all strategies for an agent in a given stage.

        Args:
            agent_id: Agent identifier.
            stage: Lifecycle stage to filter by.

        Returns:
            List of strategy dicts.
        """
        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT strategy_id, agent_id, namespace, hypothesis_id,
                       stage, created_at, updated_at, config,
                       backtest_results, robustness_results, paper_results
                FROM strategy_registry
                WHERE agent_id = ? AND stage = ?
                ORDER BY updated_at DESC
                """,
                (agent_id, stage),
            ).fetchall()

            results = []
            for r in rows:
                d = dict(r)
                # Parse JSON fields
                for field in ("config", "backtest_results", "robustness_results", "paper_results"):
                    if d[field]:
                        try:
                            d[field] = json.loads(d[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                results.append(d)
            return results

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _move_strategy_files(
        self, strategy_id: str, from_stage: str, to_stage: str
    ) -> None:
        """Move strategy files between stage directories.

        If the source file/directory exists, copies it to the target
        directory. Creates target directories as needed.
        """
        from_dir = STAGE_DIRS.get(from_stage, "")
        to_dir = STAGE_DIRS.get(to_stage, "")

        if not from_dir or not to_dir or from_dir == to_dir:
            return

        os.makedirs(to_dir, exist_ok=True)

        # Check for strategy file
        from_path = os.path.join(from_dir, f"{strategy_id}.py")
        to_path = os.path.join(to_dir, f"{strategy_id}.py")

        if os.path.exists(from_path):
            shutil.copy2(from_path, to_path)
            os.remove(from_path)
            logger.info(
                "Moved strategy file: %s -> %s", from_path, to_path
            )

        # Also check for a directory (some strategies may have multiple files)
        from_dir_path = os.path.join(from_dir, strategy_id)
        to_dir_path = os.path.join(to_dir, strategy_id)

        if os.path.isdir(from_dir_path):
            if os.path.exists(to_dir_path):
                shutil.rmtree(to_dir_path)
            shutil.copytree(from_dir_path, to_dir_path)
            shutil.rmtree(from_dir_path)
            logger.info(
                "Moved strategy directory: %s -> %s",
                from_dir_path,
                to_dir_path,
            )
