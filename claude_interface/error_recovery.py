"""Error recovery utilities for agent cycle failures.

Tracks consecutive failures per agent and implements auto-pause logic
when an agent exceeds the failure threshold (3 consecutive failures).
"""

import json
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("claude_interface.error_recovery")

# Number of consecutive failures before auto-pausing an agent
CONSECUTIVE_FAILURE_THRESHOLD = 3


def log_failed_cycle(
    db_path: str,
    agent_id: str,
    cycle: int,
    raw_output: str,
    error: str,
    wake_reason: str,
    model: str,
) -> None:
    """Insert a row into the failed_cycles table.

    Args:
        db_path: Path to the SQLite database.
        agent_id: Unique identifier for the agent.
        cycle: Cycle number that failed.
        raw_output: The raw text output (may be empty).
        error: Description of the failure.
        wake_reason: Why the agent was woken for this cycle.
        model: Model identifier used for this cycle.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_db(db_path)
        conn.execute(
            """INSERT INTO failed_cycles
               (agent_id, cycle, timestamp, raw_output, error, wake_reason, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, cycle, now, raw_output, error, wake_reason, model),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error(
            "Failed to log failed cycle for agent %s: %s", agent_id, exc
        )


def check_consecutive_failures(db_path: str, agent_id: str) -> int:
    """Count consecutive recent failures for an agent.

    Finds the timestamp of the last successful cycle_complete event, then
    counts how many failed_cycles entries exist after that timestamp.
    If there are no successful cycles, counts all failures.

    Args:
        db_path: Path to the SQLite database.
        agent_id: Unique identifier for the agent.

    Returns:
        Number of consecutive failures (0 if the last cycle succeeded).
    """
    conn = get_db(db_path)
    try:
        # Find the timestamp of the last successful cycle for this agent
        row = conn.execute(
            """SELECT timestamp FROM events
               WHERE agent_id = ? AND event_type = 'cycle_complete'
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()

        if row:
            last_success_ts = row["timestamp"]
            # Count failures after the last success
            count_row = conn.execute(
                """SELECT COUNT(*) as cnt FROM failed_cycles
                   WHERE agent_id = ? AND timestamp > ?""",
                (agent_id, last_success_ts),
            ).fetchone()
        else:
            # No successful cycles -- count all failures
            count_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM failed_cycles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()

        return count_row["cnt"] if count_row else 0

    finally:
        conn.close()


def check_auto_pause(db_path: str, agent_id: str) -> bool:
    """Check if an agent should be auto-paused due to consecutive failures.

    If the agent has >= CONSECUTIVE_FAILURE_THRESHOLD consecutive failures,
    marks the agent as paused in system_state and returns True.

    Args:
        db_path: Path to the SQLite database.
        agent_id: Unique identifier for the agent.

    Returns:
        True if the agent should be paused, False otherwise.
    """
    failures = check_consecutive_failures(db_path, agent_id)

    if failures >= CONSECUTIVE_FAILURE_THRESHOLD:
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db(db_path)
        try:
            key = f"agent_paused:{agent_id}"
            conn.execute(
                """INSERT INTO system_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?""",
                (
                    key,
                    json.dumps({
                        "paused": True,
                        "reason": f"{failures} consecutive failures",
                        "paused_at": now,
                    }),
                    now,
                    json.dumps({
                        "paused": True,
                        "reason": f"{failures} consecutive failures",
                        "paused_at": now,
                    }),
                    now,
                ),
            )

            # Also log an event for visibility
            conn.execute(
                """INSERT INTO events
                   (timestamp, event_type, agent_id, cycle, source, payload)
                   VALUES (?, 'agent_auto_paused', ?, 0, 'error_recovery', ?)""",
                (
                    now,
                    agent_id,
                    json.dumps({
                        "consecutive_failures": failures,
                        "threshold": CONSECUTIVE_FAILURE_THRESHOLD,
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.warning(
            "Agent %s auto-paused after %d consecutive failures",
            agent_id, failures,
        )
        return True

    return False
