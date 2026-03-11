"""Telegram notification service for the agentic quant trading system.

Sends formatted alerts for cycle summaries, trades, strategy events,
trigger fires, owner requests, circuit breaker activations, and errors.
Uses python-telegram-bot (async) under the hood.
"""

import asyncio
from datetime import datetime, timezone

from logging_config import get_logger

logger = get_logger("telegram.notifier")


class TelegramNotifier:
    """Sends Telegram notifications for system events.

    Args:
        bot_token: Telegram Bot API token.
        chat_id: Target chat/channel ID for messages.
        enabled: If False, messages are logged but not sent.
        dry_run: If True, messages are prefixed with ``[DRY RUN]``.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True,
        dry_run: bool = False,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.dry_run = dry_run
        self._bot = None

    def _get_bot(self):
        """Lazy-initialise the telegram Bot instance."""
        if self._bot is None:
            from telegram import Bot

            self._bot = Bot(token=self.bot_token)
        return self._bot

    def _run_async(self, coro):
        """Run an async coroutine from synchronous code."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already inside an event loop -- schedule as a task
            future = asyncio.ensure_future(coro)
            return future
        else:
            return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram.

        Args:
            text: Message body (HTML or plain text).
            parse_mode: Telegram parse mode (``HTML`` or ``Markdown``).

        Returns:
            True if the message was sent (or logged in disabled mode).
        """
        if self.dry_run:
            text = f"[DRY RUN]\n{text}"

        if not self.enabled:
            logger.info("Telegram disabled; would send: %s", text[:200])
            return True

        try:
            bot = self._get_bot()
            self._run_async(
                bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                )
            )
            return True
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False

    # ------------------------------------------------------------------
    # Formatted senders
    # ------------------------------------------------------------------

    def send_cycle_summary(
        self,
        agent_id: str,
        cycle: int,
        regime: str,
        strategy_count: int,
        actions: list[dict],
    ) -> bool:
        """Send an agent cycle summary.

        Args:
            agent_id: Agent that completed the cycle.
            cycle: Cycle number.
            regime: Current market regime label.
            strategy_count: Number of active strategies.
            actions: List of action dicts emitted during the cycle.
        """
        action_lines = ""
        if actions:
            for a in actions:
                action_lines += (
                    f"\n  - {a.get('action', '?')} {a.get('pair', '?')} "
                    f"${a.get('size_usd', 0):.0f}"
                )
        else:
            action_lines = "\n  (no actions)"

        text = (
            f"<b>Cycle Summary</b>\n"
            f"Agent: <code>{agent_id}</code>\n"
            f"Cycle: {cycle}\n"
            f"Regime: {regime}\n"
            f"Strategies: {strategy_count}\n"
            f"Actions:{action_lines}"
        )
        return self.send_message(text)

    def send_trade(
        self,
        agent_id: str,
        pair: str,
        action: str,
        size: float,
        price: float,
        strategy_id: str,
        paper: bool = False,
    ) -> bool:
        """Send a trade execution notification.

        Args:
            agent_id: Agent that placed the trade.
            pair: Trading pair (e.g. ``BTC/USD``).
            action: ``buy`` or ``sell``.
            size: Trade size in USD.
            price: Execution price.
            strategy_id: Strategy that generated the signal.
            paper: Whether this is a paper trade.
        """
        mode = "PAPER" if paper else "LIVE"
        emoji_dir = "BUY" if action.lower() == "buy" else "SELL"

        text = (
            f"<b>{mode} Trade: {emoji_dir}</b>\n"
            f"Agent: <code>{agent_id}</code>\n"
            f"Pair: {pair}\n"
            f"Action: {action.upper()}\n"
            f"Size: ${size:,.2f}\n"
            f"Price: ${price:,.2f}\n"
            f"Strategy: <code>{strategy_id}</code>"
        )
        return self.send_message(text)

    def send_strategy_event(
        self,
        event_type: str,
        strategy_id: str,
        agent_id: str,
        metrics: dict | None = None,
    ) -> bool:
        """Send a strategy lifecycle event notification.

        Args:
            event_type: E.g. ``promoted``, ``demoted``, ``killed``.
            strategy_id: Strategy identifier.
            agent_id: Owning agent.
            metrics: Optional performance metrics dict.
        """
        metrics_lines = ""
        if metrics:
            for k, v in metrics.items():
                if isinstance(v, float):
                    metrics_lines += f"\n  {k}: {v:.4f}"
                else:
                    metrics_lines += f"\n  {k}: {v}"

        text = (
            f"<b>Strategy {event_type.upper()}</b>\n"
            f"Strategy: <code>{strategy_id}</code>\n"
            f"Agent: <code>{agent_id}</code>"
        )
        if metrics_lines:
            text += f"\nMetrics:{metrics_lines}"
        return self.send_message(text)

    def send_trigger_alert(
        self,
        trigger_type: str,
        agent_id: str,
        details: str,
    ) -> bool:
        """Send a trigger fire notification.

        Args:
            trigger_type: Type of trigger that fired.
            agent_id: Agent being triggered.
            details: Human-readable detail string.
        """
        text = (
            f"<b>Trigger Fired</b>\n"
            f"Type: {trigger_type}\n"
            f"Agent: <code>{agent_id}</code>\n"
            f"Details: {details}"
        )
        return self.send_message(text)

    def send_owner_request(self, request: dict) -> bool:
        """Send an owner request alert.

        Args:
            request: Dict with keys: request_id, agent_id, type, urgency,
                title, description, suggested_action, resolution_method.
        """
        blocking = request.get("resolution_method", "") == "blocking"
        urgency = request.get("urgency", "normal").upper()
        label = "BLOCKING REQUEST" if blocking else "Request"

        text = (
            f"<b>{label} [{urgency}]</b>\n"
            f"ID: <code>{request.get('request_id', '?')}</code>\n"
            f"Agent: <code>{request.get('agent_id', '?')}</code>\n"
            f"Type: {request.get('type', '?')}\n"
            f"Title: {request.get('title', '?')}\n"
            f"Description: {request.get('description', '?')}\n"
        )
        if request.get("suggested_action"):
            text += f"Suggested: {request['suggested_action']}\n"
        if blocking:
            text += "\nUse /resolve <id> [note] to unblock."
        return self.send_message(text)

    def send_circuit_breaker(
        self,
        equity: float,
        hwm: float,
        drawdown: float,
    ) -> bool:
        """Send a circuit breaker activation alert.

        Args:
            equity: Current portfolio equity.
            hwm: High-water mark at time of trigger.
            drawdown: Drawdown fraction (0.0-1.0).
        """
        text = (
            f"<b>CIRCUIT BREAKER TRIGGERED</b>\n\n"
            f"Equity: ${equity:,.2f}\n"
            f"High-Water Mark: ${hwm:,.2f}\n"
            f"Drawdown: {drawdown * 100:.1f}%\n\n"
            f"All positions will be closed.\n"
            f"All agents paused.\n\n"
            f"Use /resume to clear after investigation."
        )
        return self.send_message(text)

    def send_error(self, component: str, error: str) -> bool:
        """Send a system error alert.

        Args:
            component: System component that raised the error.
            error: Error description or traceback excerpt.
        """
        # Truncate long errors to stay within Telegram limits
        if len(error) > 3000:
            error = error[:3000] + "\n... (truncated)"

        text = (
            f"<b>System Error</b>\n"
            f"Component: <code>{component}</code>\n"
            f"Error:\n<pre>{error}</pre>"
        )
        return self.send_message(text)
