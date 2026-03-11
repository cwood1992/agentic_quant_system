"""Tests for the risk gate (risk/portfolio.py)."""

import json

from database.schema import get_db
from risk.limits import (
    DEFAULT_MAX_POSITIONS_PER_AGENT,
    GLOBAL_MAX_GROSS_EXPOSURE,
    GLOBAL_MAX_PAIR_EXPOSURE,
)
from risk.portfolio import (
    check_agent_limits,
    check_and_approve,
    check_global_limits,
)


class TestAgentLimits:
    """Tests for per-agent limit checks."""

    def test_agent_limits_rejects_exceeding_capital(self):
        """An instruction exceeding the agent's capital allocation is rejected."""
        signal_payload = {
            "action": "buy",
            "pair": "BTC/USD",
            "size_usd": 600.0,
        }
        agent_id = "quant_primary"
        # Agent already has $400 exposure, requesting $600 more.
        # Agent capital is $500 -> 400 + 600 = 1000 > 500
        agent_positions = [{"size_usd": 400.0}]
        agent_capital = 500.0
        agent_config = {"max_positions": 5}

        ok, reason = check_agent_limits(
            signal_payload, agent_id, agent_positions, agent_capital, agent_config
        )

        assert ok is False
        assert "capital" in reason.lower()

    def test_agent_limits_rejects_max_positions(self):
        """An instruction is rejected when the agent is at max positions."""
        signal_payload = {
            "action": "buy",
            "pair": "SOL/USD",
            "size_usd": 50.0,
        }
        agent_id = "quant_primary"
        agent_positions = [
            {"size_usd": 100.0},
            {"size_usd": 100.0},
            {"size_usd": 100.0},
        ]
        agent_capital = 10000.0
        agent_config = {"max_positions": 3}

        ok, reason = check_agent_limits(
            signal_payload, agent_id, agent_positions, agent_capital, agent_config
        )

        assert ok is False
        assert "position" in reason.lower()

    def test_agent_limits_allows_close_at_max_positions(self):
        """A close action is allowed even when at max positions."""
        signal_payload = {
            "action": "close",
            "pair": "BTC/USD",
            "size_usd": 100.0,
        }
        agent_id = "quant_primary"
        agent_positions = [
            {"size_usd": 100.0},
            {"size_usd": 100.0},
            {"size_usd": 100.0},
        ]
        agent_capital = 300.0
        agent_config = {"max_positions": 3}

        ok, reason = check_agent_limits(
            signal_payload, agent_id, agent_positions, agent_capital, agent_config
        )

        assert ok is True


class TestGlobalLimits:
    """Tests for global exposure limit checks."""

    def test_global_gross_exposure_rejection(self, db):
        """Instruction rejected when it would push gross exposure above 80%."""
        portfolio_value = 10000.0
        # Existing positions use 75% ($7500), adding $600 would push to 81%
        all_positions = [{"size_usd": 7500.0, "pair": "BTC/USD", "agent_id": "other"}]
        signal_payload = {
            "action": "buy",
            "pair": "ETH/USD",
            "size_usd": 600.0,
        }

        ok, reason = check_global_limits(
            signal_payload, "quant_primary", all_positions, portfolio_value, db
        )

        assert ok is False
        assert "gross exposure" in reason.lower() or "80" in reason

    def test_global_pair_exposure_rejection(self, db):
        """Instruction rejected when a single pair would exceed 50% exposure."""
        portfolio_value = 10000.0
        # Existing BTC position at 45%, trying to add another 6%
        all_positions = [{"size_usd": 4500.0, "pair": "BTC/USD", "agent_id": "other"}]
        signal_payload = {
            "action": "buy",
            "pair": "BTC/USD",
            "size_usd": 600.0,
        }

        ok, reason = check_global_limits(
            signal_payload, "quant_primary", all_positions, portfolio_value, db
        )

        assert ok is False
        assert "BTC/USD" in reason or "50" in reason


class TestCrossAgentConflicts:
    """Tests for cross-agent conflict detection.

    In the current implementation, cross-agent conflicts are flagged via
    agent_messages but do NOT block the signal.  We verify the message is
    written to the database.
    """

    def test_cross_agent_conflict_detection(self, db):
        """Conflicting buy/sell from different agents inserts a conflict message."""
        signal_payload = {
            "action": "buy",
            "pair": "BTC/USD",
            "size_usd": 100.0,
        }
        # Existing opposing position from another agent
        all_positions = [
            {
                "action": "sell",
                "pair": "BTC/USD",
                "agent_id": "quant_secondary",
                "size_usd": 100.0,
            }
        ]

        # This should pass (conflicts don't block) but log a message
        ok, reason = check_global_limits(
            signal_payload, "quant_primary", all_positions, 10000.0, db
        )

        assert ok is True

        # Verify a conflict message was inserted into agent_messages
        conn = get_db(db)
        row = conn.execute(
            "SELECT * FROM agent_messages WHERE message_type = 'risk_alert'"
        ).fetchone()
        conn.close()

        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["type"] == "cross_agent_conflict"
        assert payload["pair"] == "BTC/USD"


class TestFullApproval:
    """Test the combined check_and_approve function."""

    def test_approved_signal_passes_all_checks(self, db):
        """A well-sized signal instruction passes all checks."""
        # Set HWM so circuit breaker doesn't fire
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 10000.0}),),
        )

        # Insert a signal instruction into the queue
        conn.execute(
            """INSERT INTO instruction_queue
               (created_at, cycle, agent_id, strategy_namespace,
                instruction_type, payload, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "2026-01-01T00:00:00+00:00",
                1,
                "quant_primary",
                "quant_primary_momentum",
                "signal",
                json.dumps({
                    "action": "buy",
                    "pair": "BTC/USD",
                    "size_usd": 200.0,
                    "agent_capital": 5000.0,
                    "agent_config": {"max_positions": 5},
                }),
                "pending",
            ),
        )
        conn.commit()
        instruction_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        conn.close()

        result = check_and_approve(
            instruction_id=instruction_id,
            db_path=db,
            portfolio_value=10000.0,
        )

        assert result == "approved"

        # Verify status updated in DB
        conn = get_db(db)
        row = conn.execute(
            "SELECT status FROM instruction_queue WHERE id = ?",
            (instruction_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "approved"
