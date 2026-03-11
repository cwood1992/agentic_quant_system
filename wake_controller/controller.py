"""Wake controller for the agentic quant trading system.

Manages agent wake scheduling using APScheduler 3.x. Each enabled agent gets
an IntervalTrigger job based on its effective cadence. A separate polling job
checks triggers every 5 minutes and fires agents whose triggers are met and
rate limits allow.
"""

import json
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from claude_interface.cycle import run_cycle
from database.schema import get_db
from logging_config import get_logger
from wake_controller.cadence import compute_effective_cadence
from wake_controller.triggers import (
    BuiltInTriggers,
    TriggerRateLimiter,
    evaluate_agent_triggers,
)

logger = get_logger("wake_controller.controller")

TRIGGER_POLL_INTERVAL_SECONDS = 300  # 5 minutes


class WakeController:
    """Orchestrates agent wake scheduling and trigger-based wakes.

    Uses APScheduler's BackgroundScheduler to run interval jobs for each
    enabled agent, plus a trigger polling job that checks all triggers
    every 5 minutes.

    Args:
        config: Full application configuration dict.
        db_path: Path to the SQLite database.
        exchange: A ccxt exchange instance for market data queries.
    """

    def __init__(self, config: dict, db_path: str, exchange):
        self.config = config
        self.db_path = db_path
        self.exchange = exchange
        self._shutdown_event = threading.Event()
        self._rate_limiter = TriggerRateLimiter()
        self._cycle_counts: dict[str, int] = {}  # agent_id -> cycle number
        self._agent_schedules: dict[str, dict] = {}  # agent_id -> schedule info

        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    def start(self) -> None:
        """Start the scheduler and register all agent jobs."""
        agents = self.config.get("agents", {})

        for agent_id, agent_cfg in agents.items():
            if not isinstance(agent_cfg, dict):
                continue
            if not agent_cfg.get("enabled", False):
                continue

            self._schedule_agent(agent_id, agent_cfg)

        # Register trigger polling job
        self._scheduler.add_job(
            self._poll_triggers,
            IntervalTrigger(seconds=TRIGGER_POLL_INTERVAL_SECONDS),
            id="trigger_poll",
            name="Trigger Polling",
        )

        self._scheduler.start()
        logger.info("WakeController started with %d agent jobs", len(self._agent_schedules))

    def stop(self) -> None:
        """Stop the scheduler gracefully."""
        self._shutdown_event.set()
        try:
            self._scheduler.shutdown(wait=True)
        except Exception:
            logger.exception("Error shutting down scheduler")
        logger.info("WakeController stopped")

    def _schedule_agent(self, agent_id: str, agent_cfg: dict) -> None:
        """Create or update the interval job for an agent."""
        base_cadence = agent_cfg.get("cadence_hours", 4)
        modifiers = agent_cfg.get("cadence_modifiers", [])
        conditions = self._get_current_conditions()

        effective_cadence = compute_effective_cadence(
            agent_id, base_cadence, modifiers, conditions,
        )

        # Bootstrap cadence: wake faster when agent has no hypotheses yet.
        # Degrades back to effective_cadence as hypotheses accumulate.
        bootstrap_cap = self._bootstrap_cadence_cap(agent_id)
        if bootstrap_cap is not None and effective_cadence > bootstrap_cap:
            logger.info(
                "Agent %s bootstrap cap applied: %.1fh -> %.1fh (hypothesis_count=%d)",
                agent_id, effective_cadence, bootstrap_cap,
                self._count_hypotheses(agent_id),
            )
            effective_cadence = bootstrap_cap

        job_id = f"agent_wake_{agent_id}"

        # Remove existing job if present
        existing = self._scheduler.get_job(job_id)
        if existing:
            self._scheduler.remove_job(job_id)

        self._scheduler.add_job(
            self._run_agent_cycle,
            IntervalTrigger(hours=effective_cadence),
            id=job_id,
            name=f"Wake {agent_id}",
            args=[agent_id],
        )

        self._agent_schedules[agent_id] = {
            "base_cadence_hours": base_cadence,
            "effective_cadence_hours": effective_cadence,
            "modifiers": modifiers,
            "conditional_triggers": agent_cfg.get("conditional_triggers", []),
            "agent_config": agent_cfg,
        }

        if agent_id not in self._cycle_counts:
            self._cycle_counts[agent_id] = 0

        logger.info(
            "Scheduled agent %s: base=%.1fh effective=%.1fh",
            agent_id, base_cadence, effective_cadence,
        )

    def update_agent_schedule(self, agent_id: str, wake_schedule: dict) -> None:
        """Update an agent's cadence, modifiers, and triggers, then reschedule.

        Args:
            agent_id: The agent to update.
            wake_schedule: Dict with optional keys: ``cadence_hours``,
                ``cadence_modifiers``, ``conditional_triggers``.
        """
        schedule = self._agent_schedules.get(agent_id)
        if schedule is None:
            logger.warning("Cannot update schedule for unknown agent %s", agent_id)
            return

        agent_cfg = dict(schedule["agent_config"])

        if "cadence_hours" in wake_schedule:
            agent_cfg["cadence_hours"] = wake_schedule["cadence_hours"]
        if "cadence_modifiers" in wake_schedule:
            agent_cfg["cadence_modifiers"] = wake_schedule["cadence_modifiers"]
        if "conditional_triggers" in wake_schedule:
            agent_cfg["conditional_triggers"] = wake_schedule["conditional_triggers"]

        self._schedule_agent(agent_id, agent_cfg)
        logger.info("Updated schedule for agent %s", agent_id)

    def _run_agent_cycle(self, agent_id: str, wake_reason: str = "scheduled") -> None:
        """Execute a single agent cycle. Called by the scheduler."""
        if self._shutdown_event.is_set():
            return

        schedule = self._agent_schedules.get(agent_id)
        if schedule is None:
            return

        agent_cfg = schedule["agent_config"]
        self._cycle_counts[agent_id] = self._cycle_counts.get(agent_id, 0) + 1
        cycle_number = self._cycle_counts[agent_id]

        logger.info(
            "Waking agent %s (cycle %d, reason: %s)",
            agent_id, cycle_number, wake_reason,
        )

        try:
            run_cycle(
                agent_id=agent_id,
                agent_config=agent_cfg,
                db_path=self.db_path,
                cycle_number=cycle_number,
                wake_reason=wake_reason,
            )
        except Exception:
            logger.exception(
                "Agent %s cycle %d failed with unhandled exception",
                agent_id, cycle_number,
            )

        # Apply any wake schedule update the agent emitted this cycle
        self._apply_wake_schedule_update(agent_id, cycle_number)

        # Re-evaluate bootstrap cadence cap after each cycle (hypothesis count may change)
        self._schedule_agent(agent_id, self._agent_schedules[agent_id]["agent_config"])

    def _poll_triggers(self) -> None:
        """Check all triggers for all agents and fire if rate limits allow."""
        if self._shutdown_event.is_set():
            return

        now = time.time()

        for agent_id, schedule in self._agent_schedules.items():
            if self._shutdown_event.is_set():
                return

            base_cadence = schedule["base_cadence_hours"]
            should_fire = False
            trigger_reason = ""

            # Built-in triggers
            try:
                if BuiltInTriggers.check_position_loss(agent_id, self.db_path):
                    should_fire = True
                    trigger_reason = "position_loss"
            except Exception:
                logger.exception("Error checking position loss trigger for %s", agent_id)

            if not should_fire:
                try:
                    if BuiltInTriggers.check_consecutive_failures(agent_id, self.db_path):
                        should_fire = True
                        trigger_reason = "consecutive_failures"
                except Exception:
                    logger.exception("Error checking failure trigger for %s", agent_id)

            if not should_fire:
                try:
                    if BuiltInTriggers.check_agent_wake_requests(agent_id, self.db_path):
                        should_fire = True
                        trigger_reason = "wake_request"
                except Exception:
                    logger.exception("Error checking wake request trigger for %s", agent_id)

            # Agent-defined conditional triggers
            if not should_fire:
                conditional = schedule.get("conditional_triggers", [])
                if conditional:
                    conditions = self._get_current_conditions()
                    try:
                        if evaluate_agent_triggers(conditional, conditions):
                            should_fire = True
                            trigger_reason = "conditional_trigger"
                    except Exception:
                        logger.exception(
                            "Error evaluating conditional triggers for %s", agent_id
                        )

            if should_fire:
                if self._rate_limiter.can_fire(agent_id, base_cadence, now):
                    self._rate_limiter.record_fire(agent_id, now)
                    logger.info(
                        "Trigger firing for agent %s: %s", agent_id, trigger_reason,
                    )
                    self._run_agent_cycle(
                        agent_id, wake_reason=f"trigger:{trigger_reason}",
                    )
                else:
                    logger.debug(
                        "Trigger %s for agent %s blocked by rate limiter",
                        trigger_reason, agent_id,
                    )

    def _count_hypotheses(self, agent_id: str) -> int:
        """Count strategy_registry rows for this agent at any stage."""
        try:
            conn = get_db(self.db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM strategy_registry WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()
        except Exception:
            return 0

    def _bootstrap_cadence_cap(self, agent_id: str) -> float | None:
        """Return a cadence cap (hours) for agents with few hypotheses.

        Provides faster wakes early in an agent's life when it needs to
        gather analysis quickly, degrading to None (no cap) once the
        agent has accumulated enough hypotheses.

        Returns:
            Cap in hours, or None if no cap should be applied.
        """
        n = self._count_hypotheses(agent_id)
        if n == 0:
            return 1.0   # New agent: wake hourly
        if n <= 2:
            return 2.0   # Early research: every 2 hours
        return None      # 3+ hypotheses: use full effective cadence

    def _apply_wake_schedule_update(self, agent_id: str, cycle_number: int) -> None:
        """Apply any wake_schedule_update event emitted by the agent this cycle."""
        try:
            conn = get_db(self.db_path)
            try:
                row = conn.execute(
                    """SELECT payload FROM events
                       WHERE event_type = 'wake_schedule_update'
                         AND agent_id = ?
                         AND cycle = ?
                       ORDER BY timestamp DESC
                       LIMIT 1""",
                    (agent_id, cycle_number),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            logger.exception("Error reading wake_schedule_update for %s", agent_id)
            return

        if row is None:
            return

        try:
            wake_schedule = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Could not parse wake_schedule_update payload for %s", agent_id)
            return

        # Map agent JSON keys to internal keys
        mapped: dict = {}
        if "base_cadence_hours" in wake_schedule:
            mapped["cadence_hours"] = float(wake_schedule["base_cadence_hours"])
        if "cadence_modifiers" in wake_schedule:
            mapped["cadence_modifiers"] = wake_schedule["cadence_modifiers"]
        if "conditional_triggers" in wake_schedule:
            mapped["conditional_triggers"] = wake_schedule["conditional_triggers"]

        if mapped:
            self.update_agent_schedule(agent_id, mapped)
            logger.info(
                "Applied wake_schedule_update for agent %s cycle %d: %s",
                agent_id, cycle_number, mapped,
            )

    def _get_current_conditions(self) -> dict:
        """Gather current market/system conditions for modifier evaluation.

        Returns a dict of condition variable names to numeric values.
        Currently provides volatility_score for configured pairs.
        """
        conditions = {}

        try:
            from data_collector.collector import compute_volatility_score

            # Use first agent's pairs or a default
            agents = self.config.get("agents", {})
            pairs = set()
            for agent_cfg in agents.values():
                if isinstance(agent_cfg, dict):
                    for pair in agent_cfg.get("pairs", []):
                        pairs.add(pair)

            if pairs:
                # Use average volatility across pairs
                scores = []
                for pair in pairs:
                    try:
                        score = compute_volatility_score(self.db_path, pair)
                        scores.append(score)
                    except Exception:
                        pass
                if scores:
                    conditions["volatility_score"] = sum(scores) / len(scores)
        except Exception:
            logger.debug("Could not compute volatility conditions", exc_info=True)

        return conditions
