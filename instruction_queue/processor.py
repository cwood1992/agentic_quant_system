"""Instruction queue processor for the agentic quant trading system.

Processes pending instructions from the instruction_queue table in FIFO order:
  1. Run each instruction through the portfolio risk gate.
  2. Route approved instructions to the correct executor (paper / live) based
     on the strategy's current stage.
  3. Handle non-signal actions (promote / demote / kill) by updating the
     strategy_registry directly.

Reference: BUILD.md — "Instruction Queue and Portfolio Risk Gate", Phase 3 task 24.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from database.schema import get_db
from logging_config import get_logger
from strategies.registry import StrategyRegistry
from risk.portfolio import check_and_approve
from strategies.base import Signal

logger = get_logger("instruction_queue.processor")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_pending_instructions(
    db_path: str,
    portfolio_value: float,
    exchange=None,
    config: Optional[dict] = None,
) -> list[dict]:
    """Process all pending instructions in FIFO order.

    For each pending instruction:
      1. Run ``check_and_approve`` from the risk gate.
      2. For approved signals, route to PaperExecutor or LiveExecutor based on
         the strategy's current stage in strategy_registry.
      3. For approved strategy actions (promote / demote / kill), update the
         strategy_registry accordingly.
      4. Update the instruction's status to ``"executed"`` or ``"failed"``
         with an ``execution_result``.

    Args:
        db_path: Path to the SQLite database.
        portfolio_value: Current total portfolio equity in USD (passed to the
            risk gate).
        exchange: Optional exchange connector instance forwarded to the live
            executor.
        config: Optional system configuration dict.

    Returns:
        List of result dicts, one per processed instruction, with keys
        ``id``, ``status``, and ``execution_result``.
    """
    conn = get_db(db_path)
    results: list[dict] = []

    try:
        rows = conn.execute(
            """
            SELECT id, created_at, cycle, agent_id, strategy_namespace,
                   instruction_type, payload, status
            FROM instruction_queue
            WHERE status = 'pending'
            ORDER BY created_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        instruction_id = row["id"]
        instruction_type = row["instruction_type"]
        payload = json.loads(row["payload"])
        agent_id = row["agent_id"]
        strategy_namespace = row["strategy_namespace"]

        # --- Step 1: risk gate ---
        decision = check_and_approve(instruction_id, db_path, portfolio_value)

        if decision == "rejected":
            results.append({
                "id": instruction_id,
                "status": "rejected",
                "execution_result": None,
            })
            continue

        # --- Step 2: route approved instructions ---
        now = datetime.now(timezone.utc).isoformat()
        try:
            if instruction_type == "signal":
                signal = extract_signal_from_payload(payload)
                if signal is None:
                    _mark_instruction(
                        db_path, instruction_id, "failed",
                        {"error": "Could not parse signal from payload"},
                        now,
                    )
                    results.append({
                        "id": instruction_id,
                        "status": "failed",
                        "execution_result": {"error": "Could not parse signal from payload"},
                    })
                    continue

                exec_result = _execute_signal(
                    db_path, signal, agent_id, strategy_namespace,
                    exchange, config,
                )
                final_status = "executed"

            elif instruction_type == "strategy_action":
                exec_result = _execute_strategy_action(
                    db_path, payload, agent_id, strategy_namespace,
                )
                final_status = "executed"

            else:
                # Unknown instruction type — mark executed with a note
                exec_result = {
                    "note": f"Unhandled instruction_type '{instruction_type}', approved but not executed",
                }
                final_status = "executed"

            _mark_instruction(db_path, instruction_id, final_status, exec_result, now)
            results.append({
                "id": instruction_id,
                "status": final_status,
                "execution_result": exec_result,
            })

        except Exception as exc:
            logger.exception(
                "Failed to execute instruction %d", instruction_id
            )
            error_result = {"error": str(exc)}
            _mark_instruction(db_path, instruction_id, "failed", error_result, now)
            results.append({
                "id": instruction_id,
                "status": "failed",
                "execution_result": error_result,
            })

    return results


def extract_signal_from_payload(payload: dict) -> Optional[Signal]:
    """Parse an instruction payload into a ``Signal`` object.

    Returns ``None`` when the payload does not represent a tradeable signal
    (e.g. promote / demote / kill actions).

    Expected payload keys (matching ``Signal`` dataclass fields):
        action, pair, size_pct, order_type, limit_price (optional),
        rationale (optional).
    """
    action = payload.get("action")
    pair = payload.get("pair")
    size_pct = payload.get("size_pct")
    order_type = payload.get("order_type")

    # All four core fields are required for a tradeable signal
    if not all([action, pair, size_pct is not None, order_type]):
        return None

    # Non-trade actions are not signals
    if action in ("promote", "demote", "kill"):
        return None

    return Signal(
        action=action,
        pair=pair,
        size_pct=float(size_pct),
        order_type=order_type,
        limit_price=payload.get("limit_price"),
        rationale=payload.get("rationale", ""),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _execute_signal(
    db_path: str,
    signal: Signal,
    agent_id: str,
    strategy_namespace: str,
    exchange=None,
    config: Optional[dict] = None,
) -> dict:
    """Route a signal to the correct executor based on strategy stage.

    Returns:
        Execution result dict from the executor.
    """
    stage = _get_strategy_stage(db_path, strategy_namespace, agent_id)

    if stage == "paper":
        return _execute_paper(signal, agent_id, strategy_namespace, db_path, config)
    elif stage == "live":
        return _execute_live(signal, agent_id, strategy_namespace, db_path, exchange, config)
    else:
        # Strategy is not in a tradeable stage (hypothesis, backtest, etc.)
        return {
            "skipped": True,
            "reason": f"Strategy stage is '{stage}', not paper or live",
        }


def _execute_paper(
    signal: Signal,
    agent_id: str,
    strategy_namespace: str,
    db_path: str,
    config: Optional[dict] = None,
) -> dict:
    """Execute a signal through the paper executor."""
    from executor.paper import PaperExecutor

    executor = PaperExecutor(db_path=db_path, config=config)
    result = executor.execute(
        signal=signal,
        agent_id=agent_id,
        strategy_id=strategy_namespace,
    )
    return result


def _execute_live(
    signal: Signal,
    agent_id: str,
    strategy_namespace: str,
    db_path: str,
    exchange=None,
    config: Optional[dict] = None,
) -> dict:
    """Execute a signal through the live executor."""
    from executor.live import LiveExecutor

    executor = LiveExecutor(
        db_path=db_path, exchange=exchange, config=config
    )
    result = executor.execute(
        signal=signal,
        agent_id=agent_id,
        strategy_id=strategy_namespace,
    )
    return result


def _execute_strategy_action(
    db_path: str,
    payload: dict,
    agent_id: str,
    strategy_namespace: str,
) -> dict:
    """Handle promote / demote / kill actions on a strategy.

    Updates the ``strategy_registry`` table accordingly.
    """
    action = payload.get("action", "").lower()
    target_stage = payload.get("target_stage")
    strategy_id = payload.get("strategy_id", strategy_namespace)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM strategy_registry WHERE strategy_id = ? AND agent_id = ?",
            (strategy_id, agent_id),
        ).fetchone()

        if row is None:
            return {"error": f"Strategy '{strategy_id}' not found for agent '{agent_id}'"}

        current_stage = row["stage"]

        registry = StrategyRegistry(db_path)

        if action == "promote":
            new_stage = target_stage or _next_stage(current_stage)
            try:
                registry.advance(strategy_id, new_stage)
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            result = {
                "action": "promote",
                "strategy_id": strategy_id,
                "from_stage": current_stage,
                "to_stage": new_stage,
            }

        elif action == "demote":
            new_stage = target_stage or _prev_stage(current_stage)
            if current_stage == "live":
                reason = payload.get("reason", "agent_demote")
                try:
                    registry.demote(strategy_id, reason)
                except (ValueError, KeyError) as exc:
                    return {"error": str(exc)}
            else:
                # Registry.demote() only supports live→paper; for other demotions
                # use direct SQL + manual file move
                conn.execute(
                    "UPDATE strategy_registry SET stage = ?, updated_at = ? "
                    "WHERE strategy_id = ? AND agent_id = ?",
                    (new_stage, now, strategy_id, agent_id),
                )
                conn.commit()
                registry._move_strategy_files(strategy_id, current_stage, new_stage)
            result = {
                "action": "demote",
                "strategy_id": strategy_id,
                "from_stage": current_stage,
                "to_stage": new_stage,
            }

        elif action == "kill":
            reason = payload.get("reason", "agent_kill")
            try:
                registry.kill(strategy_id, reason)
            except KeyError as exc:
                return {"error": str(exc)}
            result = {
                "action": "kill",
                "strategy_id": strategy_id,
                "from_stage": current_stage,
                "to_stage": "graveyard",
            }

        else:
            result = {"error": f"Unknown strategy action '{action}'"}

        # Log event
        conn.execute(
            """
            INSERT INTO events (timestamp, event_type, agent_id, source, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                "strategy_action",
                agent_id,
                "instruction_queue.processor",
                json.dumps(result),
            ),
        )
        conn.commit()
        return result

    finally:
        conn.close()


def _get_strategy_stage(
    db_path: str, strategy_namespace: str, agent_id: str
) -> str:
    """Look up the current stage of a strategy from strategy_registry.

    Returns the stage string, or ``"unknown"`` if not found.
    """
    conn = get_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT stage FROM strategy_registry
            WHERE (strategy_id = ? OR namespace = ?) AND agent_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (strategy_namespace, strategy_namespace, agent_id),
        ).fetchone()
        return row["stage"] if row else "unknown"
    finally:
        conn.close()


def _mark_instruction(
    db_path: str,
    instruction_id: int,
    status: str,
    execution_result: dict,
    now: str,
) -> None:
    """Update an instruction's status and execution_result in the queue."""
    conn = get_db(db_path)
    try:
        conn.execute(
            """
            UPDATE instruction_queue
            SET status = ?, executed_at = ?, execution_result = ?
            WHERE id = ?
            """,
            (status, now, json.dumps(execution_result), instruction_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stage progression helpers
# ---------------------------------------------------------------------------

_STAGE_ORDER = ["hypothesis", "backtest", "robustness", "paper", "live"]


def _next_stage(current: str) -> str:
    """Return the next stage in the lifecycle, or the current stage if at end."""
    try:
        idx = _STAGE_ORDER.index(current)
        return _STAGE_ORDER[min(idx + 1, len(_STAGE_ORDER) - 1)]
    except ValueError:
        return current


def _prev_stage(current: str) -> str:
    """Return the previous stage in the lifecycle, or the current stage if at start."""
    try:
        idx = _STAGE_ORDER.index(current)
        return _STAGE_ORDER[max(idx - 1, 0)]
    except ValueError:
        return current
