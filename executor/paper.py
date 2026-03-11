"""Paper (simulated) trade executor.

Executes signals against the database without touching a real exchange.
Applies configurable slippage and enforces minimum order size.
"""

from __future__ import annotations

import datetime
from typing import Optional

from database.schema import get_db
from logging_config import get_logger
from risk.limits import MINIMUM_ORDER_USD
from strategies.base import Signal

logger = get_logger("executor.paper")


class PaperExecutor:
    """Simulated executor that records paper trades in the database.

    Args:
        db_path: Path to the SQLite database.
        config: Executor configuration dict.
        slippage: Fractional slippage applied to fills (default 0.1%).
    """

    def __init__(
        self, db_path: str, config: dict, slippage: float = 0.001
    ) -> None:
        self.db_path = db_path
        self.config = config
        self.slippage = slippage

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _latest_price(self, pair: str) -> Optional[float]:
        """Fetch the most recent close price for *pair* from ohlcv_cache."""
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT close FROM ohlcv_cache "
                "WHERE pair = ? ORDER BY timestamp DESC LIMIT 1",
                (pair,),
            ).fetchone()
            return float(row["close"]) if row else None
        finally:
            conn.close()

    def _apply_slippage(self, price: float, side: str) -> float:
        if side == "buy":
            return price * (1.0 + self.slippage)
        return price * (1.0 - self.slippage)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_signal(
        self,
        signal: Signal,
        agent_id: str,
        strategy_id: str,
        agent_capital: float,
    ) -> dict:
        """Execute a paper trade for *signal*.

        Args:
            signal: The trading signal to execute.
            agent_id: Owning agent identifier.
            strategy_id: Strategy that produced the signal.
            agent_capital: Capital allocated to the agent (USD).

        Returns:
            Dict with at minimum ``status`` key.  On success includes
            ``fill_price``, ``size_usd``, ``pair``, ``action``.
        """
        if signal.action == "hold":
            return {"status": "hold", "reason": "no_action"}

        if signal.action == "close":
            return self.close_position(agent_id, signal.pair, strategy_id)

        size_usd = signal.size_pct * agent_capital
        if size_usd < MINIMUM_ORDER_USD:
            logger.warning(
                "Order rejected: size $%.2f below minimum $%.2f",
                size_usd,
                MINIMUM_ORDER_USD,
            )
            return {"status": "rejected", "reason": "below_minimum_order"}

        # Determine reference price
        price = signal.limit_price or self._latest_price(signal.pair)
        if price is None:
            logger.error("No price available for %s", signal.pair)
            return {"status": "rejected", "reason": "no_price_available"}

        fill_price = self._apply_slippage(price, signal.action)
        now = self._now_iso()

        conn = get_db(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO trades
                    (timestamp, agent_id, strategy_id, pair, action,
                     size_usd, price, order_type, fill_price,
                     fill_timestamp, fees, pnl, paper, rationale, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    agent_id,
                    strategy_id,
                    signal.pair,
                    signal.action,
                    size_usd,
                    price,
                    signal.order_type,
                    fill_price,
                    now,
                    0.0,  # fees (paper)
                    None,  # pnl computed on close
                    1,  # paper = True
                    signal.rationale,
                    "filled",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(
            "Paper %s %s %.2f USD @ %.6f (fill %.6f)",
            signal.action,
            signal.pair,
            size_usd,
            price,
            fill_price,
        )

        return {
            "status": "filled",
            "fill_price": fill_price,
            "size_usd": size_usd,
            "pair": signal.pair,
            "action": signal.action,
            "order_type": signal.order_type,
            "timestamp": now,
        }

    def get_positions(self, agent_id: str) -> list[dict]:
        """Return open positions for *agent_id*.

        An open position is a buy trade that has not been fully offset by a
        subsequent close/sell trade for the same pair and strategy.

        Returns:
            List of position dicts with keys: pair, strategy_id, entry_price,
            size_usd, current_price, unrealized_pnl.
        """
        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT t.pair, t.strategy_id, t.fill_price AS entry_price,
                       t.size_usd
                FROM trades t
                WHERE t.agent_id = ?
                  AND t.paper = 1
                  AND t.action = 'buy'
                  AND t.status = 'filled'
                  AND NOT EXISTS (
                      SELECT 1 FROM trades c
                      WHERE c.agent_id = t.agent_id
                        AND c.pair = t.pair
                        AND c.strategy_id = t.strategy_id
                        AND c.action IN ('sell', 'close')
                        AND c.status = 'filled'
                        AND c.paper = 1
                        AND c.timestamp > t.timestamp
                  )
                ORDER BY t.timestamp
                """,
                (agent_id,),
            ).fetchall()

            positions = []
            for row in rows:
                current_price = self._latest_price(row["pair"])
                entry_price = row["entry_price"]
                size_usd = row["size_usd"]

                if current_price and entry_price:
                    unrealized_pnl = (
                        (current_price - entry_price) / entry_price
                    ) * size_usd
                else:
                    unrealized_pnl = 0.0

                positions.append(
                    {
                        "pair": row["pair"],
                        "strategy_id": row["strategy_id"],
                        "entry_price": entry_price,
                        "size_usd": size_usd,
                        "current_price": current_price,
                        "unrealized_pnl": unrealized_pnl,
                    }
                )

            return positions
        finally:
            conn.close()

    def close_position(
        self, agent_id: str, pair: str, strategy_id: str
    ) -> dict:
        """Close an open paper position for *pair*.

        Finds the earliest unmatched buy, creates a closing sell trade, and
        computes realized PnL.

        Returns:
            Dict with fill details and realized_pnl, or rejection info.
        """
        conn = get_db(self.db_path)
        try:
            # Find open buy
            open_trade = conn.execute(
                """
                SELECT id, fill_price, size_usd FROM trades
                WHERE agent_id = ?
                  AND pair = ?
                  AND strategy_id = ?
                  AND action = 'buy'
                  AND status = 'filled'
                  AND paper = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM trades c
                      WHERE c.agent_id = trades.agent_id
                        AND c.pair = trades.pair
                        AND c.strategy_id = trades.strategy_id
                        AND c.action IN ('sell', 'close')
                        AND c.status = 'filled'
                        AND c.paper = 1
                        AND c.timestamp > trades.timestamp
                  )
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (agent_id, pair, strategy_id),
            ).fetchone()

            if open_trade is None:
                return {"status": "rejected", "reason": "no_open_position"}

            entry_price = open_trade["fill_price"]
            size_usd = open_trade["size_usd"]

            current_price = self._latest_price(pair)
            if current_price is None:
                return {"status": "rejected", "reason": "no_price_available"}

            fill_price = self._apply_slippage(current_price, "sell")
            realized_pnl = ((fill_price - entry_price) / entry_price) * size_usd
            now = self._now_iso()

            conn.execute(
                """
                INSERT INTO trades
                    (timestamp, agent_id, strategy_id, pair, action,
                     size_usd, price, order_type, fill_price,
                     fill_timestamp, fees, pnl, paper, rationale, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    agent_id,
                    strategy_id,
                    pair,
                    "close",
                    size_usd,
                    current_price,
                    "market",
                    fill_price,
                    now,
                    0.0,
                    realized_pnl,
                    1,
                    "position_close",
                    "filled",
                ),
            )
            conn.commit()

            logger.info(
                "Paper close %s %.2f USD @ %.6f, PnL: %.2f",
                pair,
                size_usd,
                fill_price,
                realized_pnl,
            )

            return {
                "status": "filled",
                "fill_price": fill_price,
                "size_usd": size_usd,
                "pair": pair,
                "action": "close",
                "realized_pnl": realized_pnl,
                "entry_price": entry_price,
                "timestamp": now,
            }
        finally:
            conn.close()
