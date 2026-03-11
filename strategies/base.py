"""Base strategy interface and Signal dataclass.

Defines the abstract contract all strategies must implement, and the Signal
dataclass used to communicate trading intentions from strategy logic to the
executor layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
