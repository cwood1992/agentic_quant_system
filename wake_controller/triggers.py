"""Trigger evaluation and rate limiting for agent wake scheduling.

Provides built-in triggers (position loss, circuit breaker, consecutive
failures, wake requests) and agent-defined conditional triggers, plus
rate-limiting logic to enforce cooldown and max-fires-per-window constraints.
"""

import json
import time
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger
from risk.limits import (
    CIRCUIT_BREAKER_DRAWDOWN_PCT,
    MAX_TRIGGER_FIRES_PER_BASE_WINDOW,
    POSITION_LOSS_TRIGGER_PCT,
    TRIGGER_COOLDOWN_MINUTES,
)
from risk.portfolio import check_circuit_breaker

logger = get_logger("wake_controller.triggers")


class BuiltInTriggers:
    """Built-in trigger checks for the wake controller.

    Each method returns True when the trigger condition is met, meaning
    the agent should be woken.
    """

    @staticmethod
    def check_position_loss(agent_id: str, db_path: str) -> bool:
        """Check if any open position for *agent_id* has unrealized loss >= 25%.

        Compares the position's entry price (stored in ``price`` column) against
        the most recent fill price or entry price to estimate loss.  Positions
        with ``pnl`` already set are checked directly.

        Returns:
            True if any position has loss >= POSITION_LOSS_TRIGGER_PCT.
        """
        conn = get_db(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE agent_id = ? AND status = 'open'",
                (agent_id,),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            entry_price = row["price"]
            current_price = row["fill_price"] if row["fill_price"] else entry_price
            size_usd = abs(row["size_usd"])

            if entry_price <= 0 or size_usd <= 0:
                continue

            action = row["action"].lower()
            if action == "buy":
                # Long: loss when current < entry
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                # Short: loss when current > entry
                pnl_pct = (entry_price - current_price) / entry_price

            if pnl_pct <= -POSITION_LOSS_TRIGGER_PCT:
                logger.warning(
                    "Position loss trigger for agent %s: pair=%s loss=%.1f%%",
                    agent_id, row["pair"], pnl_pct * 100,
                )
                return True

        return False

    @staticmethod
    def check_circuit_breaker(db_path: str, current_equity: float) -> bool:
        """Check if the circuit breaker is active.

        Delegates to ``risk.portfolio.check_circuit_breaker``.

        Returns:
            True if the circuit breaker has been triggered.
        """
        triggered, _ = check_circuit_breaker(db_path, current_equity)
        return triggered

    @staticmethod
    def check_consecutive_failures(agent_id: str, db_path: str) -> bool:
        """Check if the agent has 3 or more consecutive failed cycles.

        Looks at the most recent 3 entries in ``failed_cycles`` and checks
        whether all of them are more recent than the last successful cycle.

        Returns:
            True if 3+ consecutive failures detected.
        """
        conn = get_db(db_path)
        try:
            # Get last 3 failed cycles
            failed_rows = conn.execute(
                "SELECT timestamp FROM failed_cycles "
                "WHERE agent_id = ? ORDER BY id DESC LIMIT 3",
                (agent_id,),
            ).fetchall()

            if len(failed_rows) < 3:
                return False

            # Get last successful cycle timestamp
            last_success = conn.execute(
                "SELECT timestamp FROM events "
                "WHERE agent_id = ? AND event_type = 'cycle_complete' "
                "ORDER BY id DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()

        if last_success is None:
            # No successful cycle ever — 3 failures means consecutive
            return True

        # All 3 failures must be after the last success
        oldest_failure_ts = failed_rows[-1]["timestamp"]
        return oldest_failure_ts > last_success["timestamp"]

    @staticmethod
    def check_fear_greed_reversal(db_path: str) -> bool:
        """Check if Fear & Greed Index reversed up after >=2 days at extreme fear (<=20).

        Returns True when the most recent F&G value is higher than the previous
        value, and the previous 2+ consecutive values were <= 20 (extreme fear).
        This signals a potential sentiment recovery entry. (sir_020)
        """
        conn = get_db(db_path)
        try:
            rows = conn.execute(
                "SELECT value, timestamp FROM supplementary_feeds "
                "WHERE feed_name = 'fear_greed_index' "
                "ORDER BY timestamp DESC LIMIT 7",
            ).fetchall()
        finally:
            conn.close()

        if len(rows) < 3:
            return False

        # rows[0] = most recent, rows[1] = previous, etc.
        current = rows[0]["value"]
        previous = rows[1]["value"]

        if current is None or previous is None:
            return False

        # Must be increasing (reversal)
        if current <= previous:
            return False

        # Previous 2+ consecutive values must have been <= 20
        consecutive_extreme = 0
        for row in rows[1:]:
            if row["value"] is not None and row["value"] <= 20:
                consecutive_extreme += 1
            else:
                break

        if consecutive_extreme >= 2:
            logger.info(
                "F&G reversal detected: current=%.0f, previous=%.0f, "
                "extreme_fear_days=%d",
                current, previous, consecutive_extreme,
            )
            return True

        return False

    @staticmethod
    def check_spread_zscore_cross(db_path: str, threshold: float = 1.5) -> bool:
        """Check if any active pair strategy's spread z-score crosses a threshold.

        Computes the current z-score for all non-graveyard strategies with
        exactly 2 target_pairs. Returns True if any |z| >= threshold. (sir_021)
        """
        import json as _json
        import numpy as _np

        conn = get_db(db_path)
        try:
            rows = conn.execute(
                "SELECT strategy_id, config FROM strategy_registry "
                "WHERE stage NOT IN ('graveyard') AND config IS NOT NULL",
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            try:
                config = _json.loads(row["config"] or "{}")
                target_pairs = config.get("target_pairs", [])
                if len(target_pairs) != 2:
                    continue

                # Check cache in strategy_state first
                conn2 = get_db(db_path)
                try:
                    cache_row = conn2.execute(
                        "SELECT value, updated_at FROM strategy_state "
                        "WHERE strategy_id = ? AND key = 'cached_coint_params'",
                        (row["strategy_id"],),
                    ).fetchone()

                    use_cache = False
                    if cache_row:
                        try:
                            cached = _json.loads(cache_row["value"])
                            cache_time = datetime.fromisoformat(cache_row["updated_at"])
                            age_seconds = (
                                datetime.now(timezone.utc) - cache_time
                            ).total_seconds()
                            if age_seconds < 3600:  # cache valid for 1 hour
                                use_cache = True
                        except (ValueError, KeyError):
                            pass

                    if use_cache:
                        hedge_ratio = cached["hedge_ratio"]
                        intercept = cached["intercept"]
                        residual_mean = cached["residual_mean"]
                        residual_std = cached["residual_std"]
                    else:
                        # Recompute cointegration
                        from data_collector.analysis import AnalysisEngine
                        engine = AnalysisEngine(db_path)
                        result = engine.cointegration(target_pairs, "4h", lookback_days=30)
                        if "error" in result or result.get("residual_std", 0) < 1e-8:
                            continue

                        hedge_ratio = result["hedge_ratio"]
                        intercept = result["intercept"]
                        residual_mean = result["residual_mean"]
                        residual_std = result["residual_std"]

                        # Update cache
                        now_iso = datetime.now(timezone.utc).isoformat()
                        conn2.execute(
                            "INSERT OR REPLACE INTO strategy_state "
                            "(strategy_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                            (
                                row["strategy_id"],
                                "cached_coint_params",
                                _json.dumps({
                                    "hedge_ratio": hedge_ratio,
                                    "intercept": intercept,
                                    "residual_mean": residual_mean,
                                    "residual_std": residual_std,
                                }),
                                now_iso,
                            ),
                        )
                        conn2.commit()

                    # Get latest prices
                    price_a = conn2.execute(
                        "SELECT close FROM ohlcv_cache WHERE pair = ? "
                        "ORDER BY timestamp DESC LIMIT 1",
                        (target_pairs[0],),
                    ).fetchone()
                    price_b = conn2.execute(
                        "SELECT close FROM ohlcv_cache WHERE pair = ? "
                        "ORDER BY timestamp DESC LIMIT 1",
                        (target_pairs[1],),
                    ).fetchone()
                finally:
                    conn2.close()

                if not price_a or not price_b:
                    continue

                spread = price_a["close"] - (hedge_ratio * price_b["close"] + intercept)
                z_score = (spread - residual_mean) / residual_std

                if abs(z_score) >= threshold:
                    logger.info(
                        "Spread z-score trigger: %s z=%.2f (threshold=%.1f)",
                        row["strategy_id"], z_score, threshold,
                    )
                    return True

            except Exception:
                logger.debug(
                    "Error checking z-score for %s", row["strategy_id"], exc_info=True
                )

        return False

    @staticmethod
    def check_agent_wake_requests(agent_id: str, db_path: str) -> bool:
        """Check for unread wake-priority messages addressed to this agent.

        Returns:
            True if there are pending high-priority messages for the agent.
        """
        conn = get_db(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM agent_messages "
                "WHERE to_agent = ? AND priority = 'wake' AND status = 'pending'",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()

        return row["cnt"] > 0 if row else False


def evaluate_agent_triggers(
    conditional_triggers: list[dict],
    conditions: dict,
) -> bool:
    """Evaluate agent-defined conditional triggers.

    Each trigger dict has the form::

        {"condition": "volatility_score > 80"}

    Uses the same simple comparison parsing as cadence modifiers.

    Args:
        conditional_triggers: List of trigger dicts with ``condition`` key.
        conditions: Current market/system conditions.

    Returns:
        True if any trigger condition is met.
    """
    import re
    import operator

    ops = {
        ">=": operator.ge,
        "<=": operator.le,
        ">": operator.gt,
        "<": operator.lt,
        "==": operator.eq,
    }
    pattern = re.compile(r"^\s*(\w+)\s*(>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?)\s*$")

    for trigger in conditional_triggers:
        condition_str = trigger.get("condition", "")
        match = pattern.match(condition_str)
        if match is None:
            continue

        var_name = match.group(1)
        op_str = match.group(2)
        threshold = float(match.group(3))

        current_value = conditions.get(var_name)
        if current_value is None:
            continue

        op_func = ops[op_str]
        if op_func(float(current_value), threshold):
            logger.info(
                "Agent-defined trigger fired: %s (value=%s)",
                condition_str, current_value,
            )
            return True

    return False


class TriggerRateLimiter:
    """Enforces rate limits on trigger-based agent wakes.

    Tracks last wake time per agent and enforces:
    - 30-minute cooldown between any two wakes for the same agent
    - Max 2 trigger wakes per base cadence window
    """

    def __init__(self):
        # agent_id -> list of wake timestamps (epoch seconds)
        self._wake_history: dict[str, list[float]] = {}

    def can_fire(
        self, agent_id: str, base_cadence_hours: float, now: float | None = None,
    ) -> bool:
        """Check if a trigger wake is allowed for this agent.

        Args:
            agent_id: The agent to check.
            base_cadence_hours: The agent's base cadence window in hours.
            now: Current time as epoch seconds. Defaults to time.time().

        Returns:
            True if the wake is allowed.
        """
        if now is None:
            now = time.time()

        history = self._wake_history.get(agent_id, [])

        # Cooldown check: last wake must be >= 30 minutes ago
        if history:
            last_wake = history[-1]
            cooldown_seconds = TRIGGER_COOLDOWN_MINUTES * 60
            if now - last_wake < cooldown_seconds:
                logger.debug(
                    "Agent %s trigger blocked by cooldown (%.0fs remaining)",
                    agent_id, cooldown_seconds - (now - last_wake),
                )
                return False

        # Window check: max fires within the base cadence window
        window_seconds = base_cadence_hours * 3600
        window_start = now - window_seconds
        fires_in_window = sum(1 for t in history if t >= window_start)

        if fires_in_window >= MAX_TRIGGER_FIRES_PER_BASE_WINDOW:
            logger.debug(
                "Agent %s trigger blocked: %d fires in window (max %d)",
                agent_id, fires_in_window, MAX_TRIGGER_FIRES_PER_BASE_WINDOW,
            )
            return False

        return True

    def record_fire(self, agent_id: str, now: float | None = None) -> None:
        """Record that a trigger wake occurred for this agent.

        Args:
            agent_id: The agent that was woken.
            now: Current time as epoch seconds. Defaults to time.time().
        """
        if now is None:
            now = time.time()

        if agent_id not in self._wake_history:
            self._wake_history[agent_id] = []

        self._wake_history[agent_id].append(now)

        # Prune old entries (older than 48h) to prevent unbounded growth
        cutoff = now - 48 * 3600
        self._wake_history[agent_id] = [
            t for t in self._wake_history[agent_id] if t >= cutoff
        ]
