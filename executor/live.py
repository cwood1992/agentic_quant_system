"""Live trade executor using ccxt / Kraken.

Places real orders on the exchange and logs results to the database.
Supports a dry_run mode that skips actual order placement.
"""

from __future__ import annotations

import datetime
import time

import ccxt

from database.schema import get_db
from logging_config import get_logger
from risk.limits import MINIMUM_ORDER_USD
from strategies.base import Signal

logger = get_logger("executor.live")


class LiveExecutor:
    """Executor that places real orders on a ccxt exchange.

    Args:
        exchange: An initialised ccxt exchange instance.
        db_path: Path to the SQLite database.
        config: Executor configuration dict.  Supports ``dry_run`` (bool).
    """

    def __init__(
        self, exchange: ccxt.Exchange, db_path: str, config: dict
    ) -> None:
        self.exchange = exchange
        self.db_path = db_path
        self.config = config

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

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
        """Place a live order for *signal*.

        In dry_run mode the order is not sent to the exchange; instead a
        summary of what *would* have been placed is returned.

        Args:
            signal: The trading signal to execute.
            agent_id: Owning agent identifier.
            strategy_id: Strategy that produced the signal.
            agent_capital: Capital allocated to the agent (USD).

        Returns:
            Dict with ``status`` and order details.
        """
        if signal.action == "hold":
            return {"status": "hold", "reason": "no_action"}

        size_usd = signal.size_pct * agent_capital
        if size_usd < MINIMUM_ORDER_USD:
            logger.warning(
                "Order rejected: size $%.2f below minimum $%.2f",
                size_usd,
                MINIMUM_ORDER_USD,
            )
            return {"status": "rejected", "reason": "below_minimum_order"}

        # Fetch current market price to compute amount
        ticker = self.exchange.fetch_ticker(signal.pair)
        market_price = ticker["last"]
        amount = size_usd / market_price

        side = "buy" if signal.action == "buy" else "sell"
        price = signal.limit_price if signal.order_type == "limit" else None

        # --- Dry-run mode ---
        if self.config.get("dry_run", False):
            logger.info(
                "Dry-run: would place %s %s %.8f %s @ %s",
                signal.order_type,
                side,
                amount,
                signal.pair,
                price or "market",
            )
            return {
                "status": "dry_run",
                "would_have_placed": {
                    "pair": signal.pair,
                    "side": side,
                    "order_type": signal.order_type,
                    "amount": amount,
                    "price": price,
                    "size_usd": size_usd,
                },
            }

        # --- Place real order ---
        now = self._now_iso()
        try:
            order = self.exchange.create_order(
                symbol=signal.pair,
                type=signal.order_type,
                side=side,
                amount=amount,
                price=price,
            )
        except ccxt.BaseError as exc:
            logger.error(
                "Order placement failed for %s %s %s: %s",
                side,
                signal.pair,
                signal.order_type,
                exc,
            )
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
                        market_price,
                        signal.order_type,
                        None,
                        None,
                        None,
                        None,
                        0,
                        signal.rationale,
                        "failed",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return {"status": "failed", "error": str(exc)}

        order_id = order.get("id")
        fill_price = order.get("average") or order.get("price")
        fees = order.get("fee", {}).get("cost", 0.0) if order.get("fee") else 0.0
        status = order.get("status", "open")

        # For limit orders that are not immediately filled, poll
        if status != "closed" and signal.order_type == "limit":
            poll_result = self.poll_order_status(order_id, signal.pair)
            status = poll_result.get("status", status)
            fill_price = poll_result.get("average", fill_price)
            fees = poll_result.get("fees", fees)

        fill_timestamp = self._now_iso()
        final_status = "filled" if status == "closed" else status

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
                    market_price,
                    signal.order_type,
                    fill_price,
                    fill_timestamp,
                    fees,
                    None,
                    0,  # paper = False
                    signal.rationale,
                    final_status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(
            "Live %s %s %.2f USD @ %s (status: %s, order_id: %s)",
            signal.action,
            signal.pair,
            size_usd,
            fill_price,
            final_status,
            order_id,
        )

        return {
            "status": final_status,
            "order_id": order_id,
            "fill_price": fill_price,
            "size_usd": size_usd,
            "pair": signal.pair,
            "action": signal.action,
            "fees": fees,
            "timestamp": fill_timestamp,
        }

    def poll_order_status(
        self, order_id: str, pair: str, timeout: int = 300
    ) -> dict:
        """Poll exchange until *order_id* is filled or cancelled, or timeout.

        Args:
            order_id: Exchange order identifier.
            pair: Trading pair symbol.
            timeout: Maximum seconds to wait (default 300 = 5 minutes).

        Returns:
            Dict with ``status``, ``average`` fill price, and ``fees``.
        """
        deadline = time.time() + timeout
        poll_interval = 2.0

        while time.time() < deadline:
            try:
                order = self.exchange.fetch_order(order_id, pair)
            except ccxt.BaseError as exc:
                logger.warning("Error polling order %s: %s", order_id, exc)
                time.sleep(poll_interval)
                continue

            status = order.get("status")
            if status in ("closed", "canceled", "cancelled", "expired"):
                fees = (
                    order.get("fee", {}).get("cost", 0.0)
                    if order.get("fee")
                    else 0.0
                )
                return {
                    "status": status,
                    "average": order.get("average"),
                    "fees": fees,
                }

            time.sleep(poll_interval)
            # Back off slightly
            poll_interval = min(poll_interval * 1.5, 15.0)

        logger.warning(
            "Order %s timed out after %ds", order_id, timeout
        )
        return {"status": "timeout", "average": None, "fees": 0.0}
