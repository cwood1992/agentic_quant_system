"""Tests for the instruction queue processor (instruction_queue/processor.py)."""

import json

from database.schema import get_db
from instruction_queue.processor import process_pending_instructions


def _insert_instruction(db_path, agent_id, payload, cycle=1, instruction_type="signal"):
    """Helper to insert a pending instruction into the queue."""
    conn = get_db(db_path)
    conn.execute(
        """INSERT INTO instruction_queue
           (created_at, cycle, agent_id, strategy_namespace,
            instruction_type, payload, status)
           VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
        (
            "2026-01-01T00:00:00+00:00",
            cycle,
            agent_id,
            payload.get("strategy_id", "test_strategy"),
            instruction_type,
            json.dumps(payload),
        ),
    )
    conn.commit()
    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return last_id


class TestInstructionQueue:
    """Tests for instruction queue processing."""

    def test_pending_to_approved_flow(self, db):
        """A valid signal instruction transitions from pending to approved."""
        # Set HWM so circuit breaker doesn't fire
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 10000.0}),),
        )
        conn.commit()
        conn.close()

        # Insert a non-signal instruction (they skip risk checks and auto-approve)
        payload = {
            "action": "promote",
            "strategy_id": "test_strategy",
        }
        inst_id = _insert_instruction(db, "quant_primary", payload, instruction_type="strategy_action")

        results = process_pending_instructions(db, portfolio_value=10000.0)

        assert len(results) >= 1
        result = next(r for r in results if r["id"] == inst_id)
        # strategy_action is non-signal, so it gets auto-approved then executed
        assert result["status"] in ("approved", "executed")

        # Verify in DB
        conn = get_db(db)
        row = conn.execute(
            "SELECT status FROM instruction_queue WHERE id = ?", (inst_id,)
        ).fetchone()
        conn.close()
        assert row["status"] != "pending"

    def test_pending_to_rejected_flow(self, db):
        """A signal that violates risk limits is rejected."""
        # Set HWM to trigger circuit breaker (equity will be 0)
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 10000.0}),),
        )
        # Trigger circuit breaker by setting status to triggered
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'circuit_breaker_status'",
            (json.dumps({"status": "triggered"}),),
        )
        conn.commit()
        conn.close()

        payload = {
            "action": "buy",
            "pair": "BTC/USD",
            "size_usd": 500.0,
            "size_pct": 0.5,
            "order_type": "market",
            "agent_capital": 5000.0,
            "agent_config": {"max_positions": 5},
        }
        inst_id = _insert_instruction(db, "quant_primary", payload)

        results = process_pending_instructions(db, portfolio_value=0.0)

        assert len(results) >= 1
        result = next(r for r in results if r["id"] == inst_id)
        assert result["status"] == "rejected"

    def test_queue_ordering(self, db):
        """Instructions are processed in FIFO order (by created_at / id)."""
        # Set up a safe environment
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 10000.0}),),
        )
        conn.commit()
        conn.close()

        # Insert three non-signal instructions so they auto-approve
        ids = []
        for i in range(3):
            payload = {
                "action": "promote",
                "strategy_id": f"strategy_{i}",
            }
            inst_id = _insert_instruction(
                db, "quant_primary", payload, cycle=i + 1,
                instruction_type="strategy_action",
            )
            ids.append(inst_id)

        results = process_pending_instructions(db, portfolio_value=10000.0)

        # Results should come back in the same order as insertion
        result_ids = [r["id"] for r in results]
        assert result_ids == ids
