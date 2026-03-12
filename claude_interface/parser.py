"""Output parser and instruction dispatcher for agent responses.

Parses the structured JSON output from Claude agents, logs raw responses,
and routes each output field to its correct destination (instruction_queue,
strategy_registry, research_notes, events, agent_messages, etc.).
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("claude_interface.parser")

# Maximum system_improvement_requests an agent can submit per cycle
MAX_IMPROVEMENT_REQUESTS_PER_CYCLE = 3


def parse_agent_output(
    raw_text: str,
    agent_id: str,
    cycle: int,
    log_dir: str = "data/response_log",
) -> dict | None:
    """Parse a JSON response from an agent, stripping markdown fences if present.

    Always logs the raw output to disk regardless of parse success.

    Args:
        raw_text: Raw text output from the agent.
        agent_id: Unique identifier for the agent.
        cycle: Current cycle number.
        log_dir: Directory to store raw response logs.

    Returns:
        Parsed dict on success, None on failure.
    """
    # Ensure log directory exists and write raw output
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"response_{cycle}_{agent_id}.json")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
    except OSError as exc:
        logger.warning(
            "Failed to write response log to %s: %s", log_path, exc
        )

    # Strip markdown fencing if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove opening fence (```json or ```) and closing fence (```)
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    # Attempt direct JSON parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to extract a JSON object from the text
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning(
        "Failed to parse JSON from agent %s cycle %d", agent_id, cycle
    )
    return None


def dispatch_instructions(
    parsed_output: dict,
    agent_id: str,
    cycle: int,
    db_path: str,
) -> None:
    """Route all fields from a parsed agent output to their correct destinations.

    Args:
        parsed_output: The parsed JSON dict from the agent.
        agent_id: Unique identifier for the agent.
        cycle: Current cycle number.
        db_path: Path to the SQLite database.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db(db_path)
    try:
        _dispatch_strategy_actions(conn, parsed_output, agent_id, cycle, now)
        _dispatch_new_hypotheses(conn, parsed_output, agent_id, cycle, now)
        _dispatch_research_notes(conn, parsed_output, agent_id, cycle, now)
        _dispatch_analysis_requests(conn, parsed_output, agent_id, cycle, now)
        _dispatch_data_requests(conn, parsed_output, agent_id, cycle, now)
        _dispatch_benchmark_actions(conn, parsed_output, agent_id, cycle, now)
        _dispatch_owner_requests(conn, parsed_output, agent_id, cycle, now)
        _dispatch_wake_schedule(conn, parsed_output, agent_id, cycle, now)
        _dispatch_requested_model(conn, parsed_output, agent_id, cycle, now)
        _dispatch_agent_messages(conn, parsed_output, agent_id, cycle, now)
        _dispatch_system_improvement_requests(conn, parsed_output, agent_id, cycle, now)
        _dispatch_cycle_notes(conn, parsed_output, agent_id, cycle, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal dispatch helpers
# ---------------------------------------------------------------------------


def _dispatch_strategy_actions(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert strategy_actions into instruction_queue."""
    actions = parsed_output.get("strategy_actions")
    if not actions:
        return
    for action in actions:
        conn.execute(
            """INSERT INTO instruction_queue
               (created_at, cycle, agent_id, strategy_namespace, instruction_type, payload, status)
               VALUES (?, ?, ?, ?, 'strategy_action', ?, 'pending')""",
            (
                now,
                cycle,
                agent_id,
                action.get("strategy_id", "unknown"),
                json.dumps(action),
            ),
        )
    logger.info(
        "Dispatched %d strategy_action(s) for agent %s cycle %d",
        len(actions), agent_id, cycle,
    )


def _dispatch_new_hypotheses(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Create strategy_registry entries at stage='hypothesis' and write placeholder modules."""
    hypotheses = parsed_output.get("new_hypotheses")
    if not hypotheses:
        return

    # Derive namespace from agent_id (e.g. "quant_primary" -> "quant_primary")
    namespace = agent_id

    for hyp in hypotheses:
        hypothesis_id = hyp.get("hypothesis_id") or hyp.get("id") or str(uuid.uuid4())[:8]
        if hypothesis_id.startswith(f"{namespace}_"):
            strategy_id = hypothesis_id
        else:
            strategy_id = f"{namespace}_{hypothesis_id}"

        # Extract code before storing config — keep DB slim
        code = hyp.get("code", "").strip()
        hyp_config = {k: v for k, v in hyp.items() if k != "code"}

        conn.execute(
            """INSERT INTO strategy_registry
               (strategy_id, agent_id, namespace, hypothesis_id, stage,
                created_at, updated_at, config)
               VALUES (?, ?, ?, ?, 'hypothesis', ?, ?, ?)""",
            (
                strategy_id,
                agent_id,
                namespace,
                hypothesis_id,
                now,
                now,
                json.dumps(hyp_config),
            ),
        )

        # Write strategy file — use agent-supplied code if present, else placeholder
        hyp_dir = Path("strategies") / "hypotheses"
        hyp_dir.mkdir(parents=True, exist_ok=True)
        hyp_file = hyp_dir / f"{strategy_id}.py"
        if not hyp_file.exists():
            header = (
                f'"""Hypothesis: {strategy_id}\n\n'
                f'Generated by agent {agent_id} at cycle {cycle}.\n'
                f'Config: {json.dumps(hyp_config, indent=2)}\n'
                f'"""\n\n'
            )
            if code:
                try:
                    compile(code, hyp_file.name, "exec")
                except SyntaxError as exc:
                    logger.warning(
                        "Strategy code for %s has syntax error: %s", strategy_id, exc
                    )
                file_content = header + code + "\n"
            else:
                logger.warning(
                    "No code provided for hypothesis %s — backtest will fail until code is added",
                    strategy_id,
                )
                file_content = header + "# TODO: Implement strategy module conforming to BaseStrategy interface.\n"
            hyp_file.write_text(file_content, encoding="utf-8")

    logger.info(
        "Dispatched %d new hypothesis(es) for agent %s cycle %d",
        len(hypotheses), agent_id, cycle,
    )


def _dispatch_research_notes(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert or update research_notes (update if note_id matches existing)."""
    notes = parsed_output.get("research_notes")
    if not notes:
        return
    for note in notes:
        note_id = note.get("note_id", str(uuid.uuid4())[:8])

        # Check for existing note with this note_id
        existing = conn.execute(
            "SELECT id FROM research_notes WHERE note_id = ? AND agent_id = ?",
            (note_id, agent_id),
        ).fetchone()

        if existing:
            # Update existing note
            conn.execute(
                """UPDATE research_notes
                   SET cycle = ?, observation = ?, potential_edge = ?,
                       questions = ?, requested_data = ?,
                       status = ?, age_cycles = age_cycles + 1
                   WHERE note_id = ? AND agent_id = ?""",
                (
                    cycle,
                    note.get("observation", ""),
                    note.get("potential_edge"),
                    json.dumps(note.get("questions_to_resolve", note.get("questions", []))),
                    json.dumps(note.get("requested_data", [])),
                    note.get("status", "active"),
                    note_id,
                    agent_id,
                ),
            )
        else:
            # Insert new note
            conn.execute(
                """INSERT INTO research_notes
                   (note_id, agent_id, cycle, created_at, observation,
                    potential_edge, questions, requested_data, status, age_cycles)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    note_id,
                    agent_id,
                    cycle,
                    now,
                    note.get("observation", ""),
                    note.get("potential_edge"),
                    json.dumps(note.get("questions_to_resolve", note.get("questions", []))),
                    json.dumps(note.get("requested_data", [])),
                    note.get("status", "active"),
                ),
            )
    logger.info(
        "Dispatched %d research note(s) for agent %s cycle %d",
        len(notes), agent_id, cycle,
    )


def _dispatch_analysis_requests(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Log analysis_requests to events table."""
    requests = parsed_output.get("analysis_requests")
    if not requests:
        return
    for req in requests:
        conn.execute(
            """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
               VALUES (?, 'analysis_request', ?, ?, 'agent_output', ?)""",
            (now, agent_id, cycle, json.dumps(req)),
        )
    logger.info(
        "Logged %d analysis_request(s) for agent %s cycle %d",
        len(requests), agent_id, cycle,
    )


def _dispatch_data_requests(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Log data_requests to events table."""
    requests = parsed_output.get("data_requests")
    if not requests:
        return
    for req in requests:
        conn.execute(
            """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
               VALUES (?, 'data_request', ?, ?, 'agent_output', ?)""",
            (now, agent_id, cycle, json.dumps(req)),
        )
    logger.info(
        "Logged %d data_request(s) for agent %s cycle %d",
        len(requests), agent_id, cycle,
    )


def _dispatch_benchmark_actions(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert benchmark_actions into instruction_queue."""
    actions = parsed_output.get("benchmark_actions")
    if not actions:
        return
    for action in actions:
        conn.execute(
            """INSERT INTO instruction_queue
               (created_at, cycle, agent_id, strategy_namespace, instruction_type, payload, status)
               VALUES (?, ?, ?, ?, 'benchmark_action', ?, 'pending')""",
            (
                now,
                cycle,
                agent_id,
                action.get("benchmark_id", "benchmark"),
                json.dumps(action),
            ),
        )
    logger.info(
        "Dispatched %d benchmark_action(s) for agent %s cycle %d",
        len(actions), agent_id, cycle,
    )


def _dispatch_owner_requests(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert owner_requests into owner_requests table."""
    requests = parsed_output.get("owner_requests")
    if not requests:
        return
    for req in requests:
        conn.execute(
            """INSERT INTO owner_requests
               (request_id, agent_id, cycle, created_at, type, urgency, title,
                description, blocked_work, suggested_action, resolution_method, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                req.get("request_id", str(uuid.uuid4())[:8]),
                agent_id,
                cycle,
                now,
                req.get("type", "judgment_call"),
                req.get("urgency", "normal"),
                req.get("title", ""),
                req.get("description", ""),
                json.dumps(req.get("blocked_work", [])),
                req.get("suggested_action"),
                req.get("resolution_method"),
            ),
        )
    logger.info(
        "Dispatched %d owner_request(s) for agent %s cycle %d",
        len(requests), agent_id, cycle,
    )


def _dispatch_wake_schedule(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Log wake_schedule to events table."""
    schedule = parsed_output.get("wake_schedule")
    if not schedule:
        return
    conn.execute(
        """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
           VALUES (?, 'wake_schedule_update', ?, ?, 'agent_output', ?)""",
        (now, agent_id, cycle, json.dumps(schedule)),
    )
    logger.info("Logged wake_schedule update for agent %s cycle %d", agent_id, cycle)


def _dispatch_requested_model(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Log requested_model to events table."""
    model = parsed_output.get("requested_model")
    if not model:
        return
    conn.execute(
        """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
           VALUES (?, 'model_request', ?, ?, 'agent_output', ?)""",
        (now, agent_id, cycle, json.dumps({"requested_model": model})),
    )
    logger.info("Logged model_request '%s' for agent %s cycle %d", model, agent_id, cycle)


def _dispatch_agent_messages(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert agent_messages into agent_messages table.

    Messages with priority "wake" also get an event logged for the wake
    controller to pick up.
    """
    messages = parsed_output.get("agent_messages")
    if not messages:
        return
    for msg in messages:
        to_agent = msg.get("to_agent", "all")
        priority = msg.get("priority", "normal")
        message_type = msg.get("message_type", "info_share")
        payload = json.dumps({
            "content": msg.get("content", ""),
            "context": msg.get("context", {}),
        })

        conn.execute(
            """INSERT INTO agent_messages
               (created_at, from_agent, to_agent, message_type, priority, payload, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (now, agent_id, to_agent, message_type, priority, payload),
        )

        # Wake-priority messages also generate an event for the wake controller
        if priority == "wake":
            conn.execute(
                """INSERT INTO events
                   (timestamp, event_type, agent_id, cycle, source, payload)
                   VALUES (?, 'wake_request', ?, ?, 'agent_message', ?)""",
                (
                    now,
                    to_agent,
                    cycle,
                    json.dumps({
                        "from_agent": agent_id,
                        "to_agent": to_agent,
                        "message_type": message_type,
                    }),
                ),
            )

    logger.info(
        "Dispatched %d agent_message(s) from agent %s cycle %d",
        len(messages), agent_id, cycle,
    )


def _word_overlap_ratio(title_a: str, title_b: str) -> float:
    """Return the fraction of words in title_a that appear in title_b.

    Used for simple de-duplication of system improvement requests.
    """
    words_a = set(re.findall(r"\w+", title_a.lower()))
    words_b = set(re.findall(r"\w+", title_b.lower()))
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)


def _dispatch_system_improvement_requests(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Insert system_improvement_requests with de-duplication and per-agent budget.

    De-duplication: if an existing pending request's title shares >50% of
    words with the new request title, merge by updating the impact field.

    Budget: max MAX_IMPROVEMENT_REQUESTS_PER_CYCLE new requests per agent per cycle.
    Excess requests are logged but not inserted.
    """
    requests = parsed_output.get("system_improvement_requests")
    if not requests:
        return

    inserted_count = 0

    # Fetch existing pending requests for de-dup comparison
    existing = conn.execute(
        """SELECT id, title, impact FROM system_improvement_requests
           WHERE status = 'pending'""",
    ).fetchall()

    for req in requests:
        title = req.get("title", "")
        impact = req.get("impact", "")

        # Check for near-duplicate among existing pending requests
        merged = False
        for ex in existing:
            if _word_overlap_ratio(title, ex["title"]) > 0.5:
                # Merge: update impact with new context
                updated_impact = ex["impact"] + f"\n[cycle {cycle}, {agent_id}] {impact}"
                # Upgrade priority if new request is higher
                conn.execute(
                    """UPDATE system_improvement_requests
                       SET impact = ?
                       WHERE id = ?""",
                    (updated_impact, ex["id"]),
                )
                logger.info(
                    "Merged improvement request '%s' into existing id=%d for agent %s",
                    title, ex["id"], agent_id,
                )
                merged = True
                break

        if merged:
            continue

        # Enforce per-agent budget
        if inserted_count >= MAX_IMPROVEMENT_REQUESTS_PER_CYCLE:
            logger.warning(
                "Agent %s exceeded improvement request budget (%d) at cycle %d; "
                "dropping request '%s'",
                agent_id, MAX_IMPROVEMENT_REQUESTS_PER_CYCLE, cycle, title,
            )
            continue

        request_id = req.get("request_id", str(uuid.uuid4())[:8])

        # Skip if this exact request_id already exists (agent re-submitted same SIR)
        existing_id = conn.execute(
            "SELECT id FROM system_improvement_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing_id is not None:
            logger.info(
                "Skipping duplicate request_id '%s' for agent %s cycle %d",
                request_id, agent_id, cycle,
            )
            continue

        conn.execute(
            """INSERT INTO system_improvement_requests
               (request_id, created_at, agent_id, cycle, title, problem,
                impact, category, priority, examples, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                request_id,
                now,
                agent_id,
                cycle,
                title,
                req.get("problem", ""),
                impact,
                req.get("category", "other"),
                req.get("priority", "normal"),
                json.dumps(req.get("examples", [])),
            ),
        )
        inserted_count += 1

    logger.info(
        "Dispatched %d system_improvement_request(s) for agent %s cycle %d "
        "(from %d submitted)",
        inserted_count, agent_id, cycle, len(requests),
    )


def _dispatch_cycle_notes(
    conn, parsed_output: dict, agent_id: str, cycle: int, now: str
) -> None:
    """Log cycle_notes to events table."""
    notes = parsed_output.get("cycle_notes")
    if not notes:
        return
    conn.execute(
        """INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload)
           VALUES (?, 'cycle_notes', ?, ?, 'agent_output', ?)""",
        (now, agent_id, cycle, json.dumps({"cycle_notes": notes})),
    )
    logger.info("Logged cycle_notes for agent %s cycle %d", agent_id, cycle)
