"""Generate STATE.md with current system and per-agent status.

Task 6.46: Produces a human-readable snapshot of system health,
circuit breaker state, and per-agent cycle/strategy information.
"""

import json
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("state_generator")


def generate_state_md(db_path: str, config: dict) -> str:
    """Generate STATE.md content summarising global and per-agent status.

    Args:
        db_path: Path to the SQLite database.
        config: Full application configuration dict.

    Returns:
        Markdown-formatted string with GLOBAL and per-agent sections.
    """
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    lines: list[str] = []
    lines.append("# System State")
    lines.append("")
    lines.append(f"*Generated: {now}*")
    lines.append("")

    # ---- GLOBAL section ----
    lines.append("## GLOBAL")
    lines.append("")

    # Total equity
    equity = _get_system_value(conn, "total_equity", 0.0)

    # High-water mark
    hwm_row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'high_water_mark'"
    ).fetchone()
    hwm = 0.0
    if hwm_row:
        hwm_data = json.loads(hwm_row["value"])
        hwm = hwm_data.get("amount", 0.0)

    # Drawdown
    drawdown_pct = 0.0
    if hwm > 0:
        drawdown_pct = max(0.0, (hwm - equity) / hwm * 100)

    # Circuit breaker
    cb_row = conn.execute(
        "SELECT value FROM system_state WHERE key = 'circuit_breaker_status'"
    ).fetchone()
    cb_status = "normal"
    if cb_row:
        cb_data = json.loads(cb_row["value"])
        cb_status = cb_data.get("status", "normal")

    # Active agents
    agents_cfg = config.get("agents", {})
    enabled_agents = [
        aid for aid, acfg in agents_cfg.items()
        if isinstance(acfg, dict) and acfg.get("enabled", False)
    ]

    # Mode
    mode = "live"
    if config.get("system", {}).get("dry_run", True):
        mode = "dry_run"

    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total Equity | ${equity:,.2f} |")
    lines.append(f"| High-Water Mark | ${hwm:,.2f} |")
    lines.append(f"| Current Drawdown | {drawdown_pct:.2f}% |")
    lines.append(f"| Circuit Breaker | {cb_status} |")
    lines.append(f"| Active Agents | {len(enabled_agents)} |")
    lines.append(f"| Mode | {mode} |")
    lines.append(f"| Last Updated | {now} |")
    lines.append("")

    # ---- PER AGENT sections ----
    for agent_id in enabled_agents:
        agent_cfg = agents_cfg[agent_id]
        lines.append(f"## Agent: {agent_id}")
        lines.append("")

        # Status — check if paused via system_state
        agent_status = _get_agent_status(conn, agent_id)

        # Current cycle number (highest recorded cycle number)
        cycle_row = conn.execute(
            "SELECT MAX(cycle) as max_cycle FROM events "
            "WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        cycle_count = cycle_row["max_cycle"] if cycle_row and cycle_row["max_cycle"] else 0

        # Capital allocated
        capital = agent_cfg.get("capital_allocated", 0.0)

        # Strategy counts by stage
        strategy_counts = _get_strategy_counts(conn, agent_id)

        # Consecutive failures
        consecutive_failures = _get_consecutive_failures(conn, agent_id)

        # Wake cadence
        cadence_hours = agent_cfg.get("cadence_hours", 4)

        # Next scheduled wake — not available without scheduler reference,
        # so we report "managed by wake controller"
        next_wake = "managed by wake controller"

        # Last cycle notes
        last_notes = _get_last_cycle_notes(conn, agent_id)

        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Status | {agent_status} |")
        lines.append(f"| Cycles Completed | {cycle_count} |")
        lines.append(f"| Capital Allocated | ${capital:,.2f} |")

        for stage in ("live", "paper", "backtest", "hypothesis", "graveyard"):
            count = strategy_counts.get(stage, 0)
            lines.append(f"| Strategies ({stage}) | {count} |")

        lines.append(f"| Consecutive Failures | {consecutive_failures} |")
        lines.append(f"| Wake Cadence | {cadence_hours}h |")
        lines.append(f"| Next Wake | {next_wake} |")
        lines.append(f"| Last Cycle Notes | {last_notes} |")
        lines.append("")

        # Research notes (pre-hypothesis observations)
        research_notes = _get_research_notes(conn, agent_id)
        if research_notes:
            lines.append(f"### Research Notes ({len(research_notes)} active)")
            lines.append("")
            lines.append("| ID | Age | Status | Summary |")
            lines.append("|----|-----|--------|---------|")
            for note in research_notes:
                summary = note["summary"]
                if len(summary) > 80:
                    summary = summary[:80] + "…"
                lines.append(f"| {note['note_id']} | {note['age_cycles']}c | {note['status']} | {summary} |")
            lines.append("")

    conn.close()
    return "\n".join(lines)


def write_state_md(
    db_path: str, config: dict, output_path: str = "STATE.md"
) -> None:
    """Generate STATE.md and write it to disk.

    Args:
        db_path: Path to the SQLite database.
        config: Full application configuration dict.
        output_path: File path for the output (default ``STATE.md``).
    """
    try:
        content = generate_state_md(db_path, config)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("STATE.md written to %s", output_path)
    except Exception:
        logger.exception("Failed to write STATE.md to %s", output_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_system_value(conn, key: str, default=None):
    """Read a numeric value from system_state by key."""
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    if row:
        try:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                return data.get("amount", data.get("value", default))
            return data
        except (json.JSONDecodeError, TypeError):
            pass
    return default


def _get_agent_status(conn, agent_id: str) -> str:
    """Determine if an agent is active or paused."""
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?",
        (f"agent_status_{agent_id}",),
    ).fetchone()
    if row:
        try:
            data = json.loads(row["value"])
            return data.get("status", "active")
        except (json.JSONDecodeError, TypeError):
            pass
    return "active"


def _get_strategy_counts(conn, agent_id: str) -> dict[str, int]:
    """Return {stage: count} for strategies belonging to an agent."""
    rows = conn.execute(
        "SELECT stage, COUNT(*) as cnt FROM strategy_registry "
        "WHERE agent_id = ? GROUP BY stage",
        (agent_id,),
    ).fetchall()
    return {row["stage"]: row["cnt"] for row in rows}


def _get_consecutive_failures(conn, agent_id: str) -> int:
    """Count consecutive recent failures (failed_cycles with no success after).

    Looks at the most recent events to find how many cycle failures occurred
    in a row without an intervening cycle_complete.
    """
    # Get the timestamp of the last successful cycle
    last_success = conn.execute(
        "SELECT timestamp FROM events "
        "WHERE agent_id = ? AND event_type = 'cycle_complete' "
        "ORDER BY timestamp DESC LIMIT 1",
        (agent_id,),
    ).fetchone()

    if last_success:
        # Count failures after the last success
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM failed_cycles "
            "WHERE agent_id = ? AND timestamp > ?",
            (agent_id, last_success["timestamp"]),
        ).fetchone()
        return row["cnt"] if row else 0
    else:
        # No successes ever — count all failures
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM failed_cycles WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def _get_research_notes(conn, agent_id: str) -> list[dict]:
    """Return active research notes for an agent."""
    rows = conn.execute(
        "SELECT note_id, status, observation, age_cycles FROM research_notes "
        "WHERE agent_id = ? AND status NOT IN ('promoted', 'abandoned') "
        "ORDER BY created_at ASC",
        (agent_id,),
    ).fetchall()
    notes = []
    for row in rows:
        notes.append({
            "note_id": row["note_id"],
            "status": row["status"],
            "age_cycles": row["age_cycles"],
            "summary": row["observation"] or "",
        })
    return notes


def _get_last_cycle_notes(conn, agent_id: str) -> str:
    """Retrieve the most recent cycle_notes event payload for an agent."""
    row = conn.execute(
        "SELECT payload FROM events "
        "WHERE agent_id = ? AND event_type = 'cycle_notes' "
        "ORDER BY timestamp DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row:
        try:
            data = json.loads(row["payload"])
            return data.get("notes", str(data))
        except (json.JSONDecodeError, TypeError):
            return row["payload"]
    return "none"
