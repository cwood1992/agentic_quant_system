"""End-to-end agent cycle orchestration.

Wires together digest building, agent calling, output parsing, instruction
dispatch, and error recovery into a single run_cycle() function.
"""

import json
import os
from datetime import datetime, timezone

from claude_interface.caller import call_agent
from claude_interface.error_recovery import (
    check_auto_pause,
    log_failed_cycle,
)
from claude_interface.parser import dispatch_instructions, parse_agent_output
from database.schema import get_db
from digest.builder import DigestBuilder
from logging_config import get_logger
from memory.encoder import MemoryEncoder


def run_cycle(
    agent_id: str,
    agent_config: dict,
    db_path: str,
    cycle_number: int,
    wake_reason: str,
    digest_log_dir: str = "data/digest_log",
    response_log_dir: str = "data/response_log",
) -> bool:
    """Execute a full agent cycle: digest -> call -> parse -> dispatch.

    Args:
        agent_id: Unique identifier for the agent.
        agent_config: Agent configuration dict (brief, role, namespace, etc.).
        db_path: Path to the SQLite database.
        cycle_number: Current cycle number.
        wake_reason: Why the agent was woken.
        digest_log_dir: Directory for digest logs.
        response_log_dir: Directory for response logs.

    Returns:
        True on success, False on failure.
    """
    logger = get_logger("claude_interface.cycle", agent_id=agent_id)
    now = datetime.now(timezone.utc).isoformat()

    # Log cycle_start event
    _log_event(db_path, agent_id, cycle_number, "cycle_start", "cycle_runner", {
        "wake_reason": wake_reason,
        "timestamp": now,
    })

    # ---- 1. Build digest ----
    try:
        capital = agent_config.get("capital_allocated", 0.0)
        builder = DigestBuilder(agent_id, agent_config, db_path)
        digest = builder.build_full_digest(
            agent_id=agent_id,
            cycle_number=cycle_number,
            wake_reason=wake_reason,
            capital_allocated=capital,
        )
    except Exception as exc:
        logger.error("Failed to build digest: %s", exc, exc_info=True)
        log_failed_cycle(
            db_path, agent_id, cycle_number, "", f"Digest build failed: {exc}",
            wake_reason, "",
        )
        check_auto_pause(db_path, agent_id)
        return False

    _log_event(db_path, agent_id, cycle_number, "digest_built", "cycle_runner", {
        "digest_length": len(digest),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Log digest to disk
    os.makedirs(digest_log_dir, exist_ok=True)
    digest_path = os.path.join(
        digest_log_dir, f"digest_{cycle_number}_{agent_id}.txt"
    )
    try:
        with open(digest_path, "w", encoding="utf-8") as f:
            f.write(digest)
    except OSError as exc:
        logger.warning("Failed to write digest log to %s: %s", digest_path, exc)

    # ---- 2. Call agent ----
    try:
        raw_response = call_agent(
            agent_id=agent_id,
            agent_config=agent_config,
            digest=digest,
            wake_reason=wake_reason,
            db_path=db_path,
            cycle_number=cycle_number,
        )
    except Exception as exc:
        logger.error("Agent call raised exception: %s", exc, exc_info=True)
        log_failed_cycle(
            db_path, agent_id, cycle_number, "", f"Agent call exception: {exc}",
            wake_reason, agent_config.get("default_model", ""),
        )
        check_auto_pause(db_path, agent_id)
        return False

    _log_event(db_path, agent_id, cycle_number, "api_call_made", "cycle_runner", {
        "response_received": raw_response is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # call_agent returns a parsed dict or None
    # If it returns None, the cycle failed (API error or parse failure)
    if raw_response is None:
        logger.warning("Agent returned None for cycle %d", cycle_number)
        log_failed_cycle(
            db_path, agent_id, cycle_number, "",
            "Agent call returned None (API error or parse failure)",
            wake_reason, agent_config.get("default_model", ""),
        )
        check_auto_pause(db_path, agent_id)
        return False

    # ---- 3. Parse output ----
    # call_agent already parses JSON, but we still want to log the raw text
    # and use our own parser for the response log. Since call_agent returns
    # a dict, we serialise it for logging purposes.
    raw_text = json.dumps(raw_response, indent=2)
    parsed = parse_agent_output(
        raw_text=raw_text,
        agent_id=agent_id,
        cycle=cycle_number,
        log_dir=response_log_dir,
    )

    if parsed is None:
        logger.error("Output parsing failed for cycle %d", cycle_number)
        log_failed_cycle(
            db_path, agent_id, cycle_number, raw_text,
            "Output parsing failed", wake_reason,
            agent_config.get("default_model", ""),
        )
        check_auto_pause(db_path, agent_id)
        return False

    _log_event(db_path, agent_id, cycle_number, "response_parsed", "cycle_runner", {
        "parsed_keys": list(parsed.keys()) if isinstance(parsed, dict) else [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # ---- 4. Dispatch instructions ----
    try:
        dispatch_instructions(
            parsed_output=parsed,
            agent_id=agent_id,
            cycle=cycle_number,
            db_path=db_path,
        )
    except Exception as exc:
        logger.error(
            "Instruction dispatch failed for cycle %d: %s",
            cycle_number, exc, exc_info=True,
        )
        log_failed_cycle(
            db_path, agent_id, cycle_number, raw_text,
            f"Dispatch failed: {exc}", wake_reason,
            agent_config.get("default_model", ""),
        )
        check_auto_pause(db_path, agent_id)
        return False

    _log_event(db_path, agent_id, cycle_number, "instructions_dispatched", "cycle_runner", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # ---- 5. Encode cycle to memory ----
    try:
        memory_dir = os.path.join("memory", "data")
        os.makedirs(memory_dir, exist_ok=True)
        mv2_path = os.path.join(memory_dir, f"{agent_id}.mv2")

        encoder = MemoryEncoder(agent_id=agent_id, mv2_path=mv2_path)
        cycle_data = {
            "cycle_number": cycle_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "parsed_output": parsed if isinstance(parsed, dict) else {},
            "wake_reason": wake_reason,
            "agent_id": agent_id,
        }
        encoder.encode_cycle(cycle_data)

        # Store memory_query_hints as an event if present
        if isinstance(parsed, dict) and parsed.get("memory_query_hints"):
            _log_event(
                db_path, agent_id, cycle_number,
                "memory_query_hints", "cycle_runner",
                {"hints": parsed["memory_query_hints"]},
            )

        _log_event(db_path, agent_id, cycle_number, "memory_encoded", "cycle_runner", {
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        # Memory encoding failure is non-fatal; log and continue
        logger.warning(
            "Memory encoding failed for cycle %d: %s", cycle_number, exc,
        )

    # ---- 6. Log cycle_complete ----
    _log_event(db_path, agent_id, cycle_number, "cycle_complete", "cycle_runner", {
        "wake_reason": wake_reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    logger.info("Cycle %d completed successfully for agent %s", cycle_number, agent_id)
    return True


def verify_cycle_events(
    db_path: str, cycle_number: int, agent_id: str
) -> dict:
    """Check that all expected event types were logged for a cycle.

    Expected events for a successful cycle:
        cycle_start, digest_built, api_call_made, response_parsed,
        instructions_dispatched, cycle_complete

    Args:
        db_path: Path to the SQLite database.
        cycle_number: The cycle number to verify.
        agent_id: The agent whose cycle to verify.

    Returns:
        Dict with keys:
            - ``found``: list of event_type strings present
            - ``missing``: list of event_type strings absent
            - ``complete``: bool, True if all expected events are present
    """
    expected = [
        "cycle_start",
        "digest_built",
        "api_call_made",
        "response_parsed",
        "instructions_dispatched",
        "cycle_complete",
    ]

    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT DISTINCT event_type FROM events "
        "WHERE agent_id = ? AND cycle = ?",
        (agent_id, cycle_number),
    ).fetchall()
    conn.close()

    found = [row["event_type"] for row in rows]
    found_in_expected = [e for e in expected if e in found]
    missing = [e for e in expected if e not in found]

    return {
        "found": found_in_expected,
        "missing": missing,
        "complete": len(missing) == 0,
    }


def _log_event(
    db_path: str,
    agent_id: str,
    cycle: int,
    event_type: str,
    source: str,
    payload: dict,
) -> None:
    """Insert an event row into the events table."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_db(db_path)
        conn.execute(
            """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (now, event_type, agent_id, cycle, source, json.dumps(payload)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Best-effort; don't mask the primary operation
