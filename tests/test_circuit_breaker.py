"""Tests for the circuit breaker logic in risk/portfolio.py."""

import json

from database.schema import get_db
from risk.portfolio import check_circuit_breaker, update_high_water_mark


class TestCircuitBreaker:
    """Tests for circuit breaker triggering and HWM tracking."""

    def test_triggers_at_30pct_drawdown(self, db):
        """Circuit breaker triggers when equity drops 30% from HWM."""
        # Set HWM to 1000
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 1000.0}),),
        )
        conn.commit()
        conn.close()

        # Equity at 700 = 30% drawdown -> should trigger
        triggered, status = check_circuit_breaker(db_path=db, current_equity=700.0)

        assert triggered is True
        assert status == "circuit_breaker_active"

    def test_does_not_trigger_below_threshold(self, db):
        """Circuit breaker does NOT trigger when drawdown is below 30%."""
        # Set HWM to 1000
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 1000.0}),),
        )
        conn.commit()
        conn.close()

        # Equity at 710 = 29% drawdown -> should not trigger
        triggered, status = check_circuit_breaker(db_path=db, current_equity=710.0)

        assert triggered is False
        assert status == "normal"

    def test_hwm_tracking(self, db):
        """HWM updates when current equity exceeds it."""
        # Set HWM to 1000
        conn = get_db(db)
        conn.execute(
            "UPDATE system_state SET value = ? WHERE key = 'high_water_mark'",
            (json.dumps({"amount": 1000.0}),),
        )
        conn.commit()
        conn.close()

        # Equity at 1200 -> HWM should update to 1200
        update_high_water_mark(db_path=db, current_equity=1200.0)

        # Now check circuit breaker with equity still at 1200 (no drawdown)
        triggered, status = check_circuit_breaker(db_path=db, current_equity=1200.0)

        assert triggered is False
        assert status == "normal"

        # Verify HWM persisted in DB
        conn = get_db(db)
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'high_water_mark'"
        ).fetchone()
        conn.close()
        stored_hwm = json.loads(row["value"])["amount"]
        assert stored_hwm == 1200.0
