"""Base strategy interface and Signal dataclass.

Defines the abstract contract all strategies must implement, and the Signal
dataclass used to communicate trading intentions from strategy logic to the
executor layer.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

VALID_ACTIONS = {"buy", "sell", "close", "hold"}


@dataclass
class Signal:
    """A trading signal emitted by a strategy.

    Attributes:
        action: One of "buy", "sell", "close", "hold".
        pair: Trading pair symbol (e.g. "BTC/USD").
        size_pct: Fraction of allocated capital (0.0 - 1.0).
        order_type: Order type string, default "market".
        limit_price: Required when order_type is "limit".
        rationale: Human-readable explanation of the signal.
    """

    action: str
    pair: str
    size_pct: float
    order_type: str = "market"
    limit_price: Optional[float] = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise ValueError(
                f"Invalid action '{self.action}'. "
                f"Must be one of {VALID_ACTIONS}"
            )


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Subclasses must implement ``name``, ``required_feeds``, and ``on_data``.
    Optional hooks ``on_fill`` and ``on_cycle`` have default no-op
    implementations.
    """

    @abstractmethod
    def name(self) -> str:
        """Return the unique name / identifier for this strategy."""
        ...

    @abstractmethod
    def required_feeds(self) -> list[str]:
        """Return list of data feed names this strategy needs in its digest."""
        ...

    @abstractmethod
    def on_data(self, data: dict) -> list[Signal]:
        """Process a data digest and return zero or more Signals.

        Args:
            data: Dictionary of feed data keyed by feed name.

        Returns:
            List of Signal objects (may be empty if no action warranted).
        """
        ...

    def on_fill(self, fill: dict) -> None:
        """Called when an order fill is confirmed.

        Override to update internal state (e.g. position tracking).

        Args:
            fill: Fill details dict from the executor.
        """

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """Called at the end of each wake cycle.

        Override to perform bookkeeping or return diagnostics.

        Args:
            cycle_number: Current cycle count.
            portfolio_state: Snapshot of the portfolio at cycle end.

        Returns:
            Dictionary of strategy diagnostics / metrics (default empty).
        """
        return {}

    # ------------------------------------------------------------------
    # State persistence helpers
    # ------------------------------------------------------------------

    def save_state(self, db_path: str, strategy_id: str) -> None:
        """Persist strategy instance state to the database.

        Saves all instance attributes starting with ``_position`` or
        ``_state`` as key-value pairs in the ``strategy_state`` table.

        Args:
            db_path: Path to the SQLite database.
            strategy_id: Strategy identifier (used as the DB key).
        """
        from database.schema import get_db

        state_vars = {
            k: v for k, v in self.__dict__.items()
            if k.startswith("_position") or k.startswith("_state")
        }
        if not state_vars:
            return

        now = datetime.now(timezone.utc).isoformat()
        conn = get_db(db_path)
        try:
            for key, value in state_vars.items():
                conn.execute(
                    "INSERT OR REPLACE INTO strategy_state "
                    "(strategy_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                    (strategy_id, key, json.dumps(value), now),
                )
            conn.commit()
        finally:
            conn.close()

    def load_state(self, db_path: str, strategy_id: str) -> None:
        """Restore strategy instance state from the database.

        Reads all key-value pairs for *strategy_id* from ``strategy_state``
        and sets them as instance attributes.

        Args:
            db_path: Path to the SQLite database.
            strategy_id: Strategy identifier.
        """
        from database.schema import get_db

        conn = get_db(db_path)
        try:
            rows = conn.execute(
                "SELECT key, value FROM strategy_state WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchall()
            for row in rows:
                try:
                    setattr(self, row["key"], json.loads(row["value"]))
                except (json.JSONDecodeError, TypeError):
                    setattr(self, row["key"], row["value"])
        finally:
            conn.close()
