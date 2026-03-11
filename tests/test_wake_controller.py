"""Tests for wake controller cadence computation, triggers, and rate limiting.

Covers:
- Cadence clamping to [1, 24] hour range
- Cadence modifier application
- Trigger cooldown enforcement
- Trigger max fires per window
"""

import time
from unittest.mock import patch

import pytest

from wake_controller.cadence import compute_effective_cadence, evaluate_modifiers
from wake_controller.triggers import TriggerRateLimiter


# ---------------------------------------------------------------------------
# Cadence tests
# ---------------------------------------------------------------------------


class TestCadenceClamping:
    """Test that effective cadence is clamped to the allowed range."""

    def test_cadence_clamped_to_minimum(self):
        """A cadence below 1 hour should be clamped to 1 hour."""
        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=0.5,
            modifiers=[],
            current_conditions={},
        )
        assert result == 1.0

    def test_cadence_clamped_to_maximum(self):
        """A cadence above 24 hours should be clamped to 24 hours."""
        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=30.0,
            modifiers=[],
            current_conditions={},
        )
        assert result == 24.0

    def test_cadence_within_range_unchanged(self):
        """A cadence within [1, 24] should pass through unchanged."""
        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=6.0,
            modifiers=[],
            current_conditions={},
        )
        assert result == 6.0

    def test_cadence_at_minimum_boundary(self):
        """Exactly 1 hour should remain 1 hour."""
        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=1.0,
            modifiers=[],
            current_conditions={},
        )
        assert result == 1.0

    def test_cadence_at_maximum_boundary(self):
        """Exactly 24 hours should remain 24 hours."""
        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=24.0,
            modifiers=[],
            current_conditions={},
        )
        assert result == 24.0


class TestCadenceModifiers:
    """Test that cadence modifiers are correctly applied."""

    def test_cadence_modifiers_applied(self):
        """A 6h cadence with a 0.5x modifier should become 3h."""
        modifiers = [
            {"condition": "volatility_score > 70", "multiplier": 0.5},
        ]
        conditions = {"volatility_score": 85}

        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=6.0,
            modifiers=modifiers,
            current_conditions=conditions,
        )
        assert result == 3.0

    def test_modifier_not_matching(self):
        """When no modifier conditions match, cadence is unchanged."""
        modifiers = [
            {"condition": "volatility_score > 70", "multiplier": 0.5},
        ]
        conditions = {"volatility_score": 50}

        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=6.0,
            modifiers=modifiers,
            current_conditions=conditions,
        )
        assert result == 6.0

    def test_multiple_modifiers_compound(self):
        """Multiple matching modifiers should multiply together."""
        modifiers = [
            {"condition": "volatility_score > 70", "multiplier": 0.5},
            {"condition": "open_positions > 3", "multiplier": 0.8},
        ]
        conditions = {"volatility_score": 85, "open_positions": 5}

        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=10.0,
            modifiers=modifiers,
            current_conditions=conditions,
        )
        # 10 * 0.5 * 0.8 = 4.0
        assert result == 4.0

    def test_modifier_result_clamped_to_minimum(self):
        """Modifiers that reduce cadence below 1h should be clamped."""
        modifiers = [
            {"condition": "volatility_score > 70", "multiplier": 0.1},
        ]
        conditions = {"volatility_score": 90}

        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=2.0,
            modifiers=modifiers,
            current_conditions=conditions,
        )
        # 2.0 * 0.1 = 0.2 -> clamped to 1.0
        assert result == 1.0

    def test_modifier_with_missing_condition_variable(self):
        """Modifiers referencing absent variables should be skipped."""
        modifiers = [
            {"condition": "unknown_var > 50", "multiplier": 0.5},
        ]
        conditions = {"volatility_score": 85}

        result = compute_effective_cadence(
            agent_id="test_agent",
            base_cadence_hours=6.0,
            modifiers=modifiers,
            current_conditions=conditions,
        )
        assert result == 6.0

    def test_modifier_with_various_operators(self):
        """Test all supported comparison operators."""
        # >=
        assert evaluate_modifiers(
            [{"condition": "x >= 10", "multiplier": 2.0}],
            {"x": 10},
        ) == 2.0

        # <=
        assert evaluate_modifiers(
            [{"condition": "x <= 5", "multiplier": 0.5}],
            {"x": 5},
        ) == 0.5

        # ==
        assert evaluate_modifiers(
            [{"condition": "x == 42", "multiplier": 3.0}],
            {"x": 42},
        ) == 3.0

        # < (not matching)
        assert evaluate_modifiers(
            [{"condition": "x < 10", "multiplier": 0.5}],
            {"x": 10},
        ) == 1.0


# ---------------------------------------------------------------------------
# Trigger rate limiting tests
# ---------------------------------------------------------------------------


class TestTriggerRateLimiting:
    """Test trigger cooldown and max fires per window."""

    def test_trigger_cooldown_enforced(self):
        """A trigger within 30 minutes of the last wake should be blocked."""
        limiter = TriggerRateLimiter()
        base_time = 1000000.0
        base_cadence = 4.0  # hours

        # First fire is allowed
        assert limiter.can_fire("agent_a", base_cadence, now=base_time)
        limiter.record_fire("agent_a", now=base_time)

        # 10 minutes later — should be blocked (within 30-min cooldown)
        assert not limiter.can_fire(
            "agent_a", base_cadence, now=base_time + 600,
        )

        # 29 minutes later — still blocked
        assert not limiter.can_fire(
            "agent_a", base_cadence, now=base_time + 29 * 60,
        )

        # 30 minutes later — allowed
        assert limiter.can_fire(
            "agent_a", base_cadence, now=base_time + 30 * 60,
        )

    def test_trigger_max_per_window(self):
        """No more than 2 trigger wakes per base cadence window."""
        limiter = TriggerRateLimiter()
        base_time = 1000000.0
        base_cadence = 4.0  # 4-hour window

        # Fire 1 — allowed
        assert limiter.can_fire("agent_a", base_cadence, now=base_time)
        limiter.record_fire("agent_a", now=base_time)

        # Fire 2 — allowed (after cooldown)
        t2 = base_time + 31 * 60  # 31 minutes later
        assert limiter.can_fire("agent_a", base_cadence, now=t2)
        limiter.record_fire("agent_a", now=t2)

        # Fire 3 — blocked (max 2 per window, still within 4h window)
        t3 = t2 + 31 * 60  # 31 minutes after fire 2
        assert not limiter.can_fire("agent_a", base_cadence, now=t3)

        # After the window expires, should be allowed again
        t4 = base_time + 4 * 3600 + 1  # just past the 4h window from fire 1
        # But fire 2 is still in window, so we still have 1 fire in window
        # Need to wait until fire 2 is also outside window
        t5 = t2 + 4 * 3600 + 1  # past the 4h window from fire 2
        assert limiter.can_fire("agent_a", base_cadence, now=t5)

    def test_different_agents_independent(self):
        """Rate limits are per-agent; one agent's fires don't affect another."""
        limiter = TriggerRateLimiter()
        base_time = 1000000.0
        base_cadence = 4.0

        # Fire agent_a
        limiter.record_fire("agent_a", now=base_time)

        # agent_b should be unaffected
        assert limiter.can_fire("agent_b", base_cadence, now=base_time + 60)

    def test_first_fire_always_allowed(self):
        """An agent with no fire history should always be allowed."""
        limiter = TriggerRateLimiter()
        assert limiter.can_fire("new_agent", 4.0, now=1000000.0)
