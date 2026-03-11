"""API budget tracking for the agentic quant trading system.

Records per-call token usage and cost, provides monthly spend summaries
and projections for budget alerting.
"""

import json
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("billing.tracker")

# Pricing per million tokens (as of 2025 Claude pricing)
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    # Fallback for unknown models
    "default": {"input": 3.00, "output": 15.00},
}


def _ensure_billing_table(db_path: str) -> None:
    """Create the api_usage table if it does not exist."""
    conn = get_db(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class APIBudgetTracker:
    """Tracks Claude API usage and cost against a monthly budget.

    Args:
        db_path: Path to the SQLite database.
        monthly_budget: Maximum monthly spend in USD.
    """

    def __init__(self, db_path: str, monthly_budget: float = 50.0):
        self.db_path = db_path
        self.monthly_budget = monthly_budget
        _ensure_billing_table(db_path)

    def _compute_cost(
        self, input_tokens: int, output_tokens: int, model: str
    ) -> float:
        """Calculate cost in USD for a given call."""
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    def track_usage(
        self,
        agent_id: str,
        cycle: int,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> float:
        """Record an API call and return its cost.

        Args:
            agent_id: Agent that made the call.
            cycle: Cycle number.
            input_tokens: Number of input tokens used.
            output_tokens: Number of output tokens used.
            model: Model identifier string.

        Returns:
            Cost in USD for this call.
        """
        cost = self._compute_cost(input_tokens, output_tokens, model)
        now = datetime.now(timezone.utc).isoformat()

        conn = get_db(self.db_path)
        conn.execute(
            "INSERT INTO api_usage (timestamp, agent_id, cycle, model, "
            "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, agent_id, cycle, model, input_tokens, output_tokens, cost),
        )
        conn.commit()
        conn.close()

        logger.debug(
            "API usage: agent=%s cycle=%d model=%s in=%d out=%d cost=$%.4f",
            agent_id, cycle, model, input_tokens, output_tokens, cost,
        )
        return cost

    def get_monthly_spend(self) -> float:
        """Return total API spend for the current calendar month in USD."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM api_usage "
            "WHERE timestamp >= ?",
            (month_start.isoformat(),),
        ).fetchone()
        conn.close()

        return float(row["total"])

    def get_projected_monthly(self) -> float:
        """Project total monthly spend based on current daily rate.

        Returns:
            Projected month-end spend in USD. Returns 0 if no data.
        """
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days_elapsed = max((now - month_start).total_seconds() / 86400, 0.1)

        # Days in current month
        if now.month == 12:
            next_month_start = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month_start = now.replace(month=now.month + 1, day=1)
        days_in_month = (next_month_start - month_start).days

        current_spend = self.get_monthly_spend()
        daily_rate = current_spend / days_elapsed
        return daily_rate * days_in_month

    def should_alert(self) -> bool:
        """Return True if projected monthly spend exceeds the budget."""
        return self.get_projected_monthly() > self.monthly_budget

    def get_budget_summary(self) -> dict:
        """Return a budget summary dict suitable for digest integration.

        Returns:
            Dict with keys: monthly_budget, current_spend, projected_spend,
            budget_remaining, utilization_pct, alert.
        """
        current = self.get_monthly_spend()
        projected = self.get_projected_monthly()
        remaining = max(self.monthly_budget - current, 0)
        utilization = (current / self.monthly_budget * 100) if self.monthly_budget > 0 else 0

        return {
            "monthly_budget": self.monthly_budget,
            "current_spend": round(current, 4),
            "projected_spend": round(projected, 4),
            "budget_remaining": round(remaining, 4),
            "utilization_pct": round(utilization, 2),
            "alert": self.should_alert(),
        }
