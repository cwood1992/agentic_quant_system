"""Agent caller: invokes Claude API with tool-use loop.

Reads the agent brief as a cached system message, passes the digest as
the user message, and iterates through tool calls up to MAX_TOOL_ITERATIONS.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from logging_config import get_logger
from risk.limits import DEFAULT_MODEL, TRIGGER_MODEL, MAX_OUTPUT_TOKENS
from database.schema import get_db
from claude_interface.tools import AGENT_TOOLS, COMMON_TOOLS
from claude_interface.tool_executor import execute_tool_calls

MAX_TOOL_ITERATIONS = 5
TOOL_TIMEOUT_SECONDS = 60


def select_model(
    wake_reason: str, prior_response: dict | None, agent_config: dict
) -> str:
    """Choose the Claude model based on wake reason and agent request.

    Args:
        wake_reason: Why the agent was woken (e.g. "scheduled", "trigger:drawdown").
        prior_response: Previous cycle response dict, may contain requested_model.
        agent_config: Agent configuration dict with default_model / escalation_model.

    Returns:
        Model identifier string.
    """
    # Trigger wake reasons get the escalation model
    if wake_reason.startswith("trigger:"):
        return agent_config.get("escalation_model", TRIGGER_MODEL)

    # Honor a model request from the prior cycle if it is a valid model
    requested = (prior_response or {}).get("requested_model")
    if requested in (TRIGGER_MODEL, DEFAULT_MODEL):
        return requested

    return agent_config.get("default_model", DEFAULT_MODEL)


def _log_failed_cycle(
    agent_id: str,
    db_path: str,
    raw_output: str,
    error: str,
    wake_reason: str,
    model_used: str,
) -> None:
    """Insert a row into the failed_cycles table."""
    try:
        conn = get_db(db_path)
        conn.execute(
            """INSERT INTO failed_cycles
               (agent_id, cycle, timestamp, raw_output, error, wake_reason, model_used)
               VALUES (?, 0, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                datetime.now(timezone.utc).isoformat(),
                raw_output,
                error,
                wake_reason,
                model_used,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Best-effort logging; avoid masking the original error


def _extract_text(response) -> str:
    """Pull the text content from a Claude API response."""
    parts = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _parse_json_response(text: str) -> dict | None:
    """Attempt to parse a JSON object from the agent's text output.

    The agent is expected to return a JSON object, possibly surrounded by
    markdown fences or extra whitespace.
    """
    cleaned = text.strip()
    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def call_agent(
    agent_id: str,
    agent_config: dict,
    digest: str,
    wake_reason: str,
    db_path: str,
    prior_response: dict | None = None,
) -> dict | None:
    """Invoke a Claude agent with digest and tool-use loop.

    Args:
        agent_id: Unique identifier for this agent.
        agent_config: Agent configuration dict (brief path, role, model prefs, etc.).
        digest: The formatted digest string to send as user message.
        wake_reason: Why the agent was woken.
        db_path: Path to the SQLite database.
        prior_response: Previous cycle's parsed response, if any.

    Returns:
        Parsed dict from the agent's final JSON response, or None on failure.
    """
    logger = get_logger("claude_interface.caller", agent_id=agent_id)

    # Load the agent brief
    brief_path = Path(agent_config["brief"])
    try:
        brief = brief_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Brief file not found: %s", brief_path)
        _log_failed_cycle(
            agent_id, db_path, "", f"Brief not found: {brief_path}", wake_reason, ""
        )
        return None

    model = select_model(wake_reason, prior_response, agent_config)
    tools = AGENT_TOOLS.get(agent_config.get("role"), COMMON_TOOLS)

    messages = [{"role": "user", "content": digest}]

    client = anthropic.Anthropic()

    logger.info(
        "Calling agent model=%s wake_reason=%s tools=%d",
        model,
        wake_reason,
        len(tools),
    )

    response = None
    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": brief,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages,
                tools=tools if tools else None,
            )
        except anthropic.APIError as exc:
            logger.error("Anthropic API error: %s", exc, exc_info=True)
            _log_failed_cycle(
                agent_id, db_path, "", str(exc), wake_reason, model
            )
            return None
        except Exception as exc:
            logger.error("Unexpected error calling Claude API: %s", exc, exc_info=True)
            _log_failed_cycle(
                agent_id, db_path, "", str(exc), wake_reason, model
            )
            return None

        if response.stop_reason == "end_turn":
            text = _extract_text(response)
            logger.info(
                "Agent finished after %d iteration(s), response length=%d",
                iteration + 1,
                len(text),
            )
            parsed = _parse_json_response(text)
            if parsed is None:
                logger.warning("Failed to parse JSON from agent response")
                _log_failed_cycle(
                    agent_id, db_path, text, "JSON parse failure", wake_reason, model
                )
            return parsed

        if response.stop_reason == "tool_use":
            # Extract tool_use blocks from response content
            tool_use_blocks = [
                block for block in response.content if block.type == "tool_use"
            ]
            logger.info(
                "Tool use iteration %d: %d tool call(s)",
                iteration + 1,
                len(tool_use_blocks),
            )

            tool_results = execute_tool_calls(tool_use_blocks, agent_id, db_path)

            # Append assistant response and tool results to conversation
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason — break out of loop
        logger.warning("Unexpected stop_reason: %s", response.stop_reason)
        break

    # Exhausted iterations or unexpected stop — return whatever text is available
    if response is not None:
        text = _extract_text(response)
        logger.warning(
            "Reached max tool iterations (%d). Forcing response extraction.",
            MAX_TOOL_ITERATIONS,
        )
        parsed = _parse_json_response(text)
        if parsed is None:
            _log_failed_cycle(
                agent_id,
                db_path,
                text,
                "Max tool iterations reached, JSON parse failure",
                wake_reason,
                model,
            )
        return parsed

    return None
