"""Tests for the output parser (claude_interface/parser.py)."""

import json
import os
from unittest.mock import patch

from claude_interface.parser import parse_agent_output


class TestParseAgentOutput:
    """Tests for parse_agent_output."""

    def test_valid_json_parsed(self, tmp_path):
        """Valid JSON string is parsed into a dict."""
        raw = json.dumps({
            "strategy_actions": [
                {"action": "buy", "pair": "BTC/USD", "size_usd": 100}
            ],
            "cycle_notes": "All good.",
        })
        result = parse_agent_output(
            raw_text=raw,
            agent_id="quant_primary",
            cycle=1,
            log_dir=str(tmp_path),
        )

        assert result is not None
        assert "strategy_actions" in result
        assert result["strategy_actions"][0]["pair"] == "BTC/USD"

    def test_malformed_json_returns_none(self, tmp_path):
        """Malformed JSON returns None."""
        raw = "{ this is not valid json ]["
        result = parse_agent_output(
            raw_text=raw,
            agent_id="quant_primary",
            cycle=2,
            log_dir=str(tmp_path),
        )

        assert result is None

    def test_markdown_fenced_json_parsed(self, tmp_path):
        """JSON wrapped in markdown code fences is parsed correctly."""
        inner = {"cycle_notes": "Fenced output", "strategy_actions": []}
        raw = f"```json\n{json.dumps(inner)}\n```"
        result = parse_agent_output(
            raw_text=raw,
            agent_id="quant_primary",
            cycle=3,
            log_dir=str(tmp_path),
        )

        assert result is not None
        assert result["cycle_notes"] == "Fenced output"

    def test_empty_string_returns_none(self, tmp_path):
        """Empty string returns None."""
        result = parse_agent_output(
            raw_text="",
            agent_id="quant_primary",
            cycle=4,
            log_dir=str(tmp_path),
        )

        assert result is None

    def test_response_always_logged(self, tmp_path):
        """Raw response is written to disk regardless of parse success."""
        raw_valid = json.dumps({"ok": True})
        raw_invalid = "not json at all"

        parse_agent_output(
            raw_text=raw_valid,
            agent_id="agent_a",
            cycle=10,
            log_dir=str(tmp_path),
        )
        parse_agent_output(
            raw_text=raw_invalid,
            agent_id="agent_b",
            cycle=11,
            log_dir=str(tmp_path),
        )

        # Both files should exist
        valid_log = tmp_path / "response_10_agent_a.json"
        invalid_log = tmp_path / "response_11_agent_b.json"

        assert valid_log.exists()
        assert invalid_log.exists()

        # Contents should match raw input
        assert valid_log.read_text(encoding="utf-8") == raw_valid
        assert invalid_log.read_text(encoding="utf-8") == raw_invalid
