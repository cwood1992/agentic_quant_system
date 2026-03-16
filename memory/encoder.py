"""Memory encoder: converts cycle output into persistent memory records.

Uses memvid-sdk for local .mv2 file storage with lexical search,
with a fallback to JSONL-based storage if memvid-sdk is unavailable.
"""

import json
import os
from datetime import datetime, timezone

from logging_config import get_logger

try:
    import memvid_sdk
    MEMVID_AVAILABLE = True
except ImportError:
    MEMVID_AVAILABLE = False


class MemoryEncoder:
    """Encodes agent cycle data into long-term memory storage.

    Args:
        agent_id: Unique identifier for the agent.
        mv2_path: Path to the .mv2 memory file.
    """

    def __init__(self, agent_id: str, mv2_path: str):
        self.agent_id = agent_id
        self.mv2_path = mv2_path
        self.jsonl_path = os.path.splitext(mv2_path)[0] + ".jsonl"
        self.logger = get_logger("memory.encoder", agent_id=agent_id)

    def encode_cycle(self, cycle_data: dict) -> bool:
        """Create a memory record from cycle output and persist it.

        Args:
            cycle_data: Dict containing cycle output. Expected keys:
                - cycle_number (int)
                - timestamp (str, ISO format)
                - parsed_output (dict): the parsed agent response
                - wake_reason (str)
                - agent_id (str)

        Returns:
            True if encoding succeeded, False otherwise.
        """
        try:
            record = self._build_record(cycle_data)
            text = self._record_to_text(record)

            if MEMVID_AVAILABLE:
                return self._encode_memvid(text, record)
            else:
                return self._encode_jsonl(record)

        except Exception as exc:
            self.logger.error(
                "Failed to encode cycle %s to memory: %s",
                cycle_data.get("cycle_number", "?"), exc, exc_info=True,
            )
            return False

    def _build_record(self, cycle_data: dict) -> dict:
        """Extract structured fields from cycle data into a memory record."""
        parsed = cycle_data.get("parsed_output", {})
        timestamp = cycle_data.get(
            "timestamp", datetime.now(timezone.utc).isoformat()
        )

        instructions = parsed.get("instructions", [])
        active_strategies = []
        killed_strategies = []

        for instr in instructions:
            instr_type = instr.get("type", "")
            if instr_type == "kill_strategy":
                killed_strategies.append(instr.get("strategy_id", "unknown"))
            elif instr_type in ("submit_hypothesis", "promote_strategy"):
                strategy_id = instr.get("strategy_id", instr.get("hypothesis_id", "unknown"))
                stage = instr.get("to_stage", instr.get("type", "unknown"))
                active_strategies.append({"name": strategy_id, "stage": stage})

        regime = parsed.get("regime_classification", "")
        market_assessment = parsed.get("market_assessment", "")
        cycle_notes = parsed.get("cycle_notes", "")
        if isinstance(cycle_notes, dict):
            cycle_notes = cycle_notes.get("cycle_notes", str(cycle_notes))
        memory_query_hints = parsed.get("memory_query_hints", [])

        key_events = []
        for instr in instructions:
            instr_type = instr.get("type", "")
            if instr_type in ("place_order", "close_position", "kill_strategy",
                              "submit_hypothesis", "promote_strategy"):
                summary = f"{instr_type}"
                if "pair" in instr:
                    summary += f" {instr['pair']}"
                if "strategy_id" in instr:
                    summary += f" ({instr['strategy_id']})"
                key_events.append(summary)

        tool_calls_made = parsed.get("tool_calls_made", [])
        messages = parsed.get("messages", [])

        return {
            "agent_id": self.agent_id,
            "cycle_number": cycle_data.get("cycle_number", 0),
            "timestamp": timestamp,
            "regime_classification": regime,
            "market_assessment": market_assessment,
            "active_strategies": active_strategies,
            "killed_strategies": killed_strategies,
            "cycle_notes": cycle_notes,
            "key_events": key_events,
            "tool_calls_made": tool_calls_made,
            "messages_sent_received": messages,
            "memory_query_hints": memory_query_hints,
            "wake_reason": cycle_data.get("wake_reason", ""),
        }

    def _record_to_text(self, record: dict) -> str:
        """Convert a memory record to a searchable text representation."""
        parts = [
            f"Cycle {record['cycle_number']} at {record['timestamp']}",
            f"Wake reason: {record['wake_reason']}",
        ]

        if record["regime_classification"]:
            parts.append(f"Regime: {record['regime_classification']}")
        if record["market_assessment"]:
            parts.append(f"Market assessment: {record['market_assessment']}")
        if record["active_strategies"]:
            strats = ", ".join(
                f"{s['name']}({s['stage']})" for s in record["active_strategies"]
            )
            parts.append(f"Active strategies: {strats}")
        if record["killed_strategies"]:
            parts.append(f"Killed strategies: {', '.join(record['killed_strategies'])}")
        if record["key_events"]:
            parts.append(f"Key events: {'; '.join(record['key_events'])}")
        if record["cycle_notes"]:
            notes = record["cycle_notes"]
            if isinstance(notes, dict):
                notes = notes.get("cycle_notes", str(notes))
            parts.append(f"Notes: {notes}")

        return "\n".join(parts)

    def _encode_memvid(self, text: str, record: dict) -> bool:
        """Encode using memvid-sdk: put text into .mv2 file."""
        try:
            os.makedirs(os.path.dirname(self.mv2_path) or ".", exist_ok=True)

            if os.path.exists(self.mv2_path):
                m = memvid_sdk.use("basic", self.mv2_path)
            else:
                m = memvid_sdk.create(self.mv2_path)

            m.put(text=text)
            m.close()

            self.logger.info(
                "Encoded cycle %d to memvid at %s",
                record["cycle_number"], self.mv2_path,
            )
            # Also write JSONL for get_recent() support
            self._encode_jsonl(record)
            return True

        except Exception as exc:
            self.logger.warning(
                "memvid encoding failed, falling back to JSONL: %s", exc,
            )
            return self._encode_jsonl(record)

    def _encode_jsonl(self, record: dict) -> bool:
        """Fallback: append JSON record to a .jsonl file."""
        os.makedirs(os.path.dirname(self.jsonl_path) or ".", exist_ok=True)

        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

        self.logger.info(
            "Encoded cycle %d to JSONL at %s",
            record["cycle_number"], self.jsonl_path,
        )
        return True
