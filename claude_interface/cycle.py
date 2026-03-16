"""End-to-end agent cycle orchestration.

Wires together digest building, agent calling, output parsing, instruction
dispatch, and error recovery into a single run_cycle() function.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

from billing.tracker import APIBudgetTracker
from claude_interface.caller import call_agent
from claude_interface.error_recovery import (
    check_auto_pause,
    log_failed_cycle,
)
from claude_interface.parser import (
    age_research_notes,
    dispatch_instructions,
    expire_old_research_notes,
    parse_agent_output,
)
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

    # ---- 0. Age and expire research notes ----
    try:
        aged = age_research_notes(db_path, agent_id)
        expired = expire_old_research_notes(db_path, agent_id)
        if aged or expired:
            logger.info("Research notes: aged %d, expired %d", aged, expired)
    except Exception as exc:
        logger.warning("Failed to age/expire research notes: %s", exc)

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
        memory_dir = str(_PROJECT_ROOT / "memory" / "data")
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

    # ---- 5b. Generate executive summary via Haiku ----
    try:
        _generate_executive_summary(
            parsed_output=parsed if isinstance(parsed, dict) else {},
            agent_id=agent_id,
            cycle_number=cycle_number,
            db_path=db_path,
        )
    except Exception as exc:
        logger.warning("Executive summary generation failed: %s", exc)

    # ---- 6. Log cycle_complete ----
    _log_event(db_path, agent_id, cycle_number, "cycle_complete", "cycle_runner", {
        "wake_reason": wake_reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    logger.info("Cycle %d completed successfully for agent %s", cycle_number, agent_id)
    return True


SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MAX_TOKENS = 300


def _generate_executive_summary(
    parsed_output: dict,
    agent_id: str,
    cycle_number: int,
    db_path: str,
) -> None:
    """Call Haiku to generate a 2-3 sentence executive summary of this cycle.

    Stores the result in system_state under key 'executive_summary'.
    Non-fatal: caller wraps this in try/except.
    """
    logger = get_logger("claude_interface.cycle", agent_id=agent_id)

    # Build context for Haiku from the parsed output
    cycle_notes = parsed_output.get("cycle_notes", "")
    if isinstance(cycle_notes, dict):
        cycle_notes = cycle_notes.get("cycle_notes", str(cycle_notes))

    instructions = parsed_output.get("instructions", [])
    instruction_summary = ", ".join(
        f"{i.get('type', 'unknown')}: {i.get('strategy_id', i.get('note_id', ''))}"
        for i in instructions[:5]
    ) if instructions else "none"

    research_notes = parsed_output.get("research_notes", [])
    research_summary = f"{len(research_notes)} notes" if research_notes else "none"

    user_msg = (
        f"Agent: {agent_id}, Cycle: {cycle_number}\n"
        f"Instructions issued: {instruction_summary}\n"
        f"Research notes: {research_summary}\n"
        f"Cycle notes: {cycle_notes[:1000]}"
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        system=(
            "You are a concise financial analyst. Summarize the trading system's "
            "current state in 2-3 sentences for the system owner. Focus on: what "
            "changed this cycle, current risk posture, and key pending actions. "
            "Do not use markdown formatting. Be direct and specific."
        ),
        messages=[{"role": "user", "content": user_msg}],
    )

    # Track API usage
    try:
        APIBudgetTracker(db_path).track_usage(
            agent_id="system",
            cycle=cycle_number,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=SUMMARY_MODEL,
        )
    except Exception:
        pass

    summary_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            summary_text += block.text

    # Store in system_state
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
        (
            "executive_summary",
            json.dumps({
                "summary": summary_text.strip(),
                "generated_at": now,
                "cycle": cycle_number,
                "agent_id": agent_id,
            }),
        ),
    )
    conn.commit()
    conn.close()

    logger.info("Executive summary generated for cycle %d (%d chars)", cycle_number, len(summary_text))


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
