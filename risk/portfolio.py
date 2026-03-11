"""Portfolio risk gate for the agentic quant trading system.

Implements per-agent limits, global limits, circuit breaker logic,
and the main check_and_approve entry point.  Every instruction flows
through this gate before execution.

Reference: BUILD.md — "Instruction Queue and Portfolio Risk Gate"
"""

import json
import datetime

from database.schema import get_db
from logging_config import get_logger
from risk.limits import (
    CIRCUIT_BREAKER_DRAWDOWN_PCT,
    DEFAULT_MAX_POSITIONS_PER_AGENT,
    GLOBAL_MAX_GROSS_EXPOSURE,
    GLOBAL_MAX_PAIR_EXPOSURE,
)

logger = get_logger("risk.portfolio")


# ---------------------------------------------------------------------------
# Per-agent checks
# ---------------------------------------------------------------------------

def check_agent_limits(
    signal_payload: dict,
    agent_id: str,
    agent_positions: list,
    agent_capital: float,
    agent_config: dict,
) -> tuple[bool, str]:
    """Check per-agent risk limits.

    Args:
        signal_payload: Parsed signal dict with keys such as action, pair,
            size_usd.
        agent_id: ID of the agent that emitted the signal.
        agent_positions: List of dicts representing current open positions for
            this agent.  Each dict must contain at least ``size_usd``.
        agent_capital: Dollar amount of capital allocated to this agent.
        agent_config: Agent configuration dict; may contain ``max_positions``.

    Returns:
        ``(True, "passed")`` when the signal is within limits, or
        ``(False, <reason>)`` when it would violate a limit.
    """
    action = signal_payload.get("action", "").lower()

    # Close / sell signals are always allowed — they reduce risk.
    if action != "buy":
        return True, "passed"

    # --- Capital ceiling ---
    existing_exposure = sum(abs(p.get("size_usd", 0)) for p in agent_positions)
    new_size = abs(signal_payload.get("size_usd", 0))
    if existing_exposure + new_size > agent_capital:
        return False, "Would exceed agent capital allocation"

    # --- Max concurrent positions ---
    max_positions = agent_config.get(
        "max_positions", DEFAULT_MAX_POSITIONS_PER_AGENT
    )
    if len(agent_positions) >= max_positions:
        return False, "Agent at max concurrent positions"

    return True, "passed"


# ---------------------------------------------------------------------------
# Global checks
# ---------------------------------------------------------------------------

def check_global_limits(
    signal_payload: dict,
    agent_id: str,
    all_positions: list,
    portfolio_value: float,
    db_path: str,
) -> tuple[bool, str]:
    """Check system-wide exposure limits and detect cross-agent conflicts.

    Cross-agent conflicts (same pair, opposing direction) are logged and
    escalated to the portfolio manager via the agent_messages table, but they
    do **not** block the signal.

    Args:
        signal_payload: Parsed signal dict (action, pair, size_usd, …).
        agent_id: Originating agent.
        all_positions: All open positions across every agent.  Each dict must
            contain at least ``size_usd``, ``pair``, ``agent_id``, and
            ``action`` (direction).
        portfolio_value: Total portfolio equity in USD.
        db_path: Path to the SQLite database (used for agent_messages on
            conflict detection).

    Returns:
        ``(True, "passed")`` or ``(False, <reason>)``.
    """
    if portfolio_value <= 0:
        return False, "Portfolio value is zero or negative"

    signal_size = abs(signal_payload.get("size_usd", 0))
    signal_pair = signal_payload.get("pair", "")

    # --- Gross exposure limit ---
    gross_exposure = sum(abs(p.get("size_usd", 0)) for p in all_positions)
    if (gross_exposure + signal_size) / portfolio_value > GLOBAL_MAX_GROSS_EXPOSURE:
        return (
            False,
            f"Global gross exposure would exceed {GLOBAL_MAX_GROSS_EXPOSURE * 100:.0f}%",
        )

    # --- Per-pair exposure limit ---
    pair_exposure = sum(
        abs(p.get("size_usd", 0))
        for p in all_positions
        if p.get("pair") == signal_pair
    )
    if (pair_exposure + signal_size) / portfolio_value > GLOBAL_MAX_PAIR_EXPOSURE:
        return (
            False,
            f"Global {signal_pair} exposure would exceed {GLOBAL_MAX_PAIR_EXPOSURE * 100:.0f}% limit",
        )

    # --- Cross-agent conflict detection (flag, never block) ---
    _detect_cross_agent_conflicts(
        signal_payload, agent_id, all_positions, db_path
    )

    return True, "passed"


def _detect_cross_agent_conflicts(
    signal_payload: dict,
    agent_id: str,
    all_positions: list,
    db_path: str,
) -> None:
    """Log and message PM when agents hold opposing positions on the same pair."""
    signal_pair = signal_payload.get("pair", "")
    signal_action = signal_payload.get("action", "").lower()

    for pos in all_positions:
        if pos.get("pair") != signal_pair:
            continue
        if pos.get("agent_id") == agent_id:
            continue
        pos_action = pos.get("action", "").lower()
        # Opposing means one is buy and the other is sell
        if (signal_action == "buy" and pos_action == "sell") or (
            signal_action == "sell" and pos_action == "buy"
        ):
            logger.warning(
                "Cross-agent conflict on %s: agent %s (%s) vs agent %s (%s)",
                signal_pair,
                agent_id,
                signal_action,
                pos.get("agent_id"),
                pos_action,
            )
            _send_conflict_message(signal_pair, agent_id, pos, db_path)
            break  # one alert per signal is sufficient


def _send_conflict_message(
    pair: str, agent_id: str, conflicting_pos: dict, db_path: str
) -> None:
    """Insert an agent_message to portfolio_manager about the conflict."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = json.dumps(
        {
            "type": "cross_agent_conflict",
            "pair": pair,
            "agent_a": agent_id,
            "agent_b": conflicting_pos.get("agent_id"),
            "detail": f"Opposing directions on {pair}",
        }
    )
    try:
        conn = get_db(db_path)
        conn.execute(
            """
            INSERT INTO agent_messages
                (created_at, from_agent, to_agent, message_type,
                 priority, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "system",
                "portfolio_manager",
                "risk_alert",
                "high",
                payload,
                "pending",
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to send cross-agent conflict message")


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

def check_circuit_breaker(
    db_path: str, current_equity: float
) -> tuple[bool, str]:
    """Evaluate the circuit breaker against the high-water mark.

    When triggered the function:
      1. Sets ``circuit_breaker_status`` to ``"triggered"`` in system_state.
      2. Logs a ``circuit_breaker_triggered`` event.

    Callers are responsible for closing all positions and pausing agents
    when ``(True, "circuit_breaker_active")`` is returned.

    Returns:
        ``(True, "circuit_breaker_active")`` if the breaker has fired,
        ``(False, "normal")`` otherwise.
    """
    conn = get_db(db_path)

    # Already triggered?
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'circuit_breaker_status'"
    ).fetchone()
    if row:
        status_data = json.loads(row["value"])
        if status_data.get("status") == "triggered":
            conn.close()
            return True, "circuit_breaker_active"

    # Get high-water mark
    hwm_row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'high_water_mark'"
    ).fetchone()
    hwm = 0.0
    if hwm_row:
        hwm_data = json.loads(hwm_row["value"])
        hwm = hwm_data.get("amount", 0.0)

    # No HWM yet — system just started, no breaker possible
    if hwm <= 0:
        conn.close()
        return False, "normal"

    drawdown = (hwm - current_equity) / hwm
    if drawdown >= CIRCUIT_BREAKER_DRAWDOWN_PCT:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        logger.critical(
            "CIRCUIT BREAKER TRIGGERED: equity=%.2f hwm=%.2f drawdown=%.2f%%",
            current_equity,
            hwm,
            drawdown * 100,
        )

        # Update circuit breaker status
        conn.execute(
            "UPDATE system_state SET value = ?, updated_at = ? "
            "WHERE key = 'circuit_breaker_status'",
            (
                json.dumps({
                    "status": "triggered",
                    "triggered_at": now,
                    "equity": current_equity,
                    "hwm": hwm,
                }),
                now,
            ),
        )

        # Audit event
        conn.execute(
            """
            INSERT INTO events (timestamp, event_type, source, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                "circuit_breaker_triggered",
                "risk.portfolio",
                json.dumps({
                    "equity": current_equity,
                    "hwm": hwm,
                    "drawdown_pct": drawdown,
                }),
            ),
        )

        conn.commit()
        conn.close()
        return True, "circuit_breaker_active"

    conn.close()
    return False, "normal"


def update_high_water_mark(db_path: str, current_equity: float) -> None:
    """Update the stored high-water mark if *current_equity* exceeds it."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'high_water_mark'"
    ).fetchone()

    stored_hwm = 0.0
    if row:
        hwm_data = json.loads(row["value"])
        stored_hwm = hwm_data.get("amount", 0.0)

    if current_equity > stored_hwm:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "UPDATE system_state SET value = ?, updated_at = ? "
            "WHERE key = 'high_water_mark'",
            (json.dumps({"amount": current_equity}), now),
        )
        conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_and_approve(
    instruction_id: int,
    db_path: str,
    portfolio_value: float,
) -> str:
    """Load an instruction from the queue, run all risk checks, and update
    its status to ``"approved"`` or ``"rejected"``.

    Rejected instructions are recorded in the events table so they appear
    in the originating agent's next digest.

    Args:
        instruction_id: Primary key in ``instruction_queue``.
        db_path: Path to the SQLite database.
        portfolio_value: Current total portfolio equity in USD.

    Returns:
        ``"approved"`` or ``"rejected"``.
    """
    conn = get_db(db_path)

    # --- Load instruction ---
    row = conn.execute(
        "SELECT * FROM instruction_queue WHERE id = ?", (instruction_id,)
    ).fetchone()
    if row is None:
        conn.close()
        logger.error("Instruction %d not found", instruction_id)
        return "rejected"

    agent_id = row["agent_id"]
    payload = json.loads(row["payload"])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Non-signal instructions (promote / demote / kill) skip risk checks
    instruction_type = row["instruction_type"]
    if instruction_type != "signal":
        conn.execute(
            "UPDATE instruction_queue SET status = ?, risk_check_result = ? "
            "WHERE id = ?",
            (
                "approved",
                json.dumps({"reason": "non-signal instruction, no risk check required"}),
                instruction_id,
            ),
        )
        conn.commit()
        conn.close()
        return "approved"

    # --- Gather context ---
    agent_positions = _get_agent_positions(conn, agent_id)
    agent_capital = payload.get("agent_capital", portfolio_value)
    agent_config = payload.get("agent_config", {})
    all_positions = _get_all_positions(conn)

    # --- Per-agent limits ---
    ok, reason = check_agent_limits(
        payload, agent_id, agent_positions, agent_capital, agent_config
    )
    if not ok:
        _reject_instruction(conn, instruction_id, reason, agent_id, now)
        conn.close()
        return "rejected"

    # --- Global limits ---
    ok, reason = check_global_limits(
        payload, agent_id, all_positions, portfolio_value, db_path
    )
    if not ok:
        _reject_instruction(conn, instruction_id, reason, agent_id, now)
        conn.close()
        return "rejected"

    # --- Approved ---
    conn.execute(
        "UPDATE instruction_queue SET status = ?, risk_check_result = ? "
        "WHERE id = ?",
        (
            "approved",
            json.dumps({"reason": "all checks passed"}),
            instruction_id,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Instruction %d approved for agent %s", instruction_id, agent_id)
    return "approved"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _reject_instruction(
    conn,
    instruction_id: int,
    reason: str,
    agent_id: str,
    now: str,
) -> None:
    """Mark an instruction as rejected and write an audit event."""
    conn.execute(
        "UPDATE instruction_queue SET status = ?, risk_check_result = ? "
        "WHERE id = ?",
        ("rejected", json.dumps({"reason": reason}), instruction_id),
    )
    conn.execute(
        """
        INSERT INTO events (timestamp, event_type, agent_id, source, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            now,
            "instruction_rejected",
            agent_id,
            "risk.portfolio",
            json.dumps({"instruction_id": instruction_id, "reason": reason}),
        ),
    )
    conn.commit()
    logger.warning(
        "Instruction %d rejected for agent %s: %s",
        instruction_id,
        agent_id,
        reason,
    )


def _get_agent_positions(conn, agent_id: str) -> list[dict]:
    """Return open positions for a single agent as a list of dicts."""
    rows = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? AND status = 'open'",
        (agent_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_all_positions(conn) -> list[dict]:
    """Return all open positions across all agents."""
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'open'"
    ).fetchall()
    return [dict(r) for r in rows]
