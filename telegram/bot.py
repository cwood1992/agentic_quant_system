"""Telegram bot with command handlers for system management.

Provides interactive commands for inspecting system state, resolving owner
requests, pausing/resuming agents, and managing the improvement pipeline.
Uses python-telegram-bot's Application (v20+) async interface.
"""

import json
from datetime import datetime, timezone

from logging_config import get_logger
from database.schema import get_db

logger = get_logger("telegram.bot")


class TelegramBot:
    """Interactive Telegram bot for the agentic quant trading system.

    Args:
        bot_token: Telegram Bot API token.
        chat_id: Authorised chat ID (commands from other chats are ignored).
        db_path: Path to the SQLite database.
        config: Full application configuration dict.
        wake_controller: Optional WakeController instance for /cycle and /pause.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        db_path: str,
        config: dict,
        wake_controller=None,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.db_path = db_path
        self.config = config
        self.wake_controller = wake_controller
        self._app = None

    def _authorised(self, update) -> bool:
        """Check that the message comes from the authorised chat."""
        return str(update.effective_chat.id) == self.chat_id

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_requests(self, update, context) -> None:
        """List pending owner requests."""
        if not self._authorised(update):
            return

        conn = get_db(self.db_path)
        rows = conn.execute(
            "SELECT id, request_id, agent_id, urgency, title, type, resolution_method "
            "FROM owner_requests WHERE status = 'pending' "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("No pending owner requests.")
            return

        lines = ["<b>Pending Owner Requests</b>\n"]
        for r in rows:
            blocking = " [BLOCKING]" if r["resolution_method"] == "blocking" else ""
            lines.append(
                f"<code>{r['id']}</code> | {r['urgency'].upper()}{blocking}\n"
                f"  {r['title']}\n"
                f"  Agent: {r['agent_id']} | Type: {r['type']}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_resolve(self, update, context) -> None:
        """Resolve an owner request: /resolve <id> [note]"""
        if not self._authorised(update):
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /resolve <id> [note]")
            return

        try:
            req_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid request ID.")
            return

        note = " ".join(args[1:]) if len(args) > 1 else "Resolved via Telegram"
        now = datetime.now(timezone.utc).isoformat()

        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT id FROM owner_requests WHERE id = ? AND status = 'pending'",
            (req_id,),
        ).fetchone()

        if not row:
            conn.close()
            await update.message.reply_text(f"Request {req_id} not found or already resolved.")
            return

        conn.execute(
            "UPDATE owner_requests SET status = 'resolved', resolved_at = ?, "
            "resolution_note = ? WHERE id = ?",
            (now, note, req_id),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(f"Request {req_id} resolved: {note}")

    async def _cmd_pause(self, update, context) -> None:
        """Pause a specific agent or all agents: /pause [agent_id]"""
        if not self._authorised(update):
            return

        args = context.args or []
        agent_id = args[0] if args else None
        now = datetime.now(timezone.utc).isoformat()

        conn = get_db(self.db_path)

        if agent_id:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (f"agent_paused_{agent_id}", json.dumps({"paused": True}), now),
            )
            conn.commit()
            conn.close()
            logger.info("Agent %s paused via Telegram", agent_id)
            await update.message.reply_text(f"Agent {agent_id} paused.")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                ("all_agents_paused", json.dumps({"paused": True}), now),
            )
            conn.commit()
            conn.close()
            logger.info("All agents paused via Telegram")
            await update.message.reply_text("All agents paused.")

    async def _cmd_resume(self, update, context) -> None:
        """Resume a specific agent or all agents, clear circuit breaker: /resume [agent_id]"""
        if not self._authorised(update):
            return

        args = context.args or []
        agent_id = args[0] if args else None
        now = datetime.now(timezone.utc).isoformat()

        conn = get_db(self.db_path)

        if agent_id:
            conn.execute(
                "DELETE FROM system_state WHERE key = ?",
                (f"agent_paused_{agent_id}",),
            )
            conn.commit()
            conn.close()
            logger.info("Agent %s resumed via Telegram", agent_id)
            await update.message.reply_text(f"Agent {agent_id} resumed.")
        else:
            # Clear all pause flags
            conn.execute(
                "DELETE FROM system_state WHERE key LIKE 'agent_paused_%'"
            )
            conn.execute(
                "DELETE FROM system_state WHERE key = 'all_agents_paused'"
            )
            # Clear circuit breaker
            conn.execute(
                "UPDATE system_state SET value = ?, updated_at = ? "
                "WHERE key = 'circuit_breaker_status'",
                (json.dumps({"status": "normal"}), now),
            )
            conn.commit()
            conn.close()
            logger.info("All agents resumed, circuit breaker cleared via Telegram")
            await update.message.reply_text(
                "All agents resumed. Circuit breaker cleared."
            )

    async def _cmd_status(self, update, context) -> None:
        """Show system state summary: /status"""
        if not self._authorised(update):
            return

        conn = get_db(self.db_path)

        # Circuit breaker
        cb_row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'circuit_breaker_status'"
        ).fetchone()
        cb_status = "normal"
        if cb_row:
            cb_data = json.loads(cb_row["value"])
            cb_status = cb_data.get("status", "normal")

        # High-water mark
        hwm_row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'high_water_mark'"
        ).fetchone()
        hwm = 0.0
        if hwm_row:
            hwm = json.loads(hwm_row["value"]).get("amount", 0.0)

        # Open positions
        pos_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status = 'open'"
        ).fetchone()["cnt"]

        # Active strategies
        strat_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM strategy_registry WHERE stage IN ('paper', 'live')"
        ).fetchone()["cnt"]

        # Pending requests
        req_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM owner_requests WHERE status = 'pending'"
        ).fetchone()["cnt"]

        # Pending instructions
        instr_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM instruction_queue WHERE status = 'pending'"
        ).fetchone()["cnt"]

        # Paused agents
        paused_rows = conn.execute(
            "SELECT key FROM system_state WHERE key LIKE 'agent_paused_%'"
        ).fetchall()
        paused_agents = [r["key"].replace("agent_paused_", "") for r in paused_rows]

        all_paused_row = conn.execute(
            "SELECT value FROM system_state WHERE key = 'all_agents_paused'"
        ).fetchone()
        all_paused = False
        if all_paused_row:
            all_paused = json.loads(all_paused_row["value"]).get("paused", False)

        conn.close()

        pause_text = "ALL PAUSED" if all_paused else (
            ", ".join(paused_agents) if paused_agents else "none"
        )

        text = (
            f"<b>System Status</b>\n\n"
            f"Circuit Breaker: <code>{cb_status}</code>\n"
            f"High-Water Mark: ${hwm:,.2f}\n"
            f"Open Positions: {pos_count}\n"
            f"Active Strategies: {strat_count}\n"
            f"Pending Requests: {req_count}\n"
            f"Pending Instructions: {instr_count}\n"
            f"Paused Agents: {pause_text}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_cycle(self, update, context) -> None:
        """Force an immediate wake cycle: /cycle <agent_id>"""
        if not self._authorised(update):
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /cycle <agent_id>")
            return

        agent_id = args[0]

        if self.wake_controller is None:
            await update.message.reply_text("Wake controller not available.")
            return

        try:
            self.wake_controller._run_agent_cycle(agent_id, wake_reason="manual_telegram")
            await update.message.reply_text(f"Cycle triggered for agent {agent_id}.")
        except Exception as e:
            await update.message.reply_text(f"Failed to trigger cycle: {e}")

    async def _cmd_agents(self, update, context) -> None:
        """List all configured agents: /agents"""
        if not self._authorised(update):
            return

        agents = self.config.get("agents", {})
        if not agents:
            await update.message.reply_text("No agents configured.")
            return

        conn = get_db(self.db_path)
        lines = ["<b>Configured Agents</b>\n"]

        for agent_id, cfg in agents.items():
            if not isinstance(cfg, dict):
                continue

            enabled = cfg.get("enabled", False)
            role = cfg.get("role", "unknown")
            cadence = cfg.get("base_cadence_hours", "?")
            cap_pct = cfg.get("capital_allocation_pct", 0) * 100

            # Check if paused
            paused_row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?",
                (f"agent_paused_{agent_id}",),
            ).fetchone()
            paused = bool(paused_row)

            status = "PAUSED" if paused else ("active" if enabled else "disabled")

            lines.append(
                f"<code>{agent_id}</code>\n"
                f"  Role: {role} | Cadence: {cadence}h | Capital: {cap_pct:.0f}%\n"
                f"  Status: {status}"
            )

        conn.close()
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_messages(self, update, context) -> None:
        """Show recent inter-agent messages: /messages"""
        if not self._authorised(update):
            return

        conn = get_db(self.db_path)
        rows = conn.execute(
            "SELECT created_at, from_agent, to_agent, message_type, priority, "
            "substr(payload, 1, 100) as payload_preview "
            "FROM agent_messages ORDER BY id DESC LIMIT 15"
        ).fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("No inter-agent messages.")
            return

        lines = ["<b>Recent Agent Messages</b>\n"]
        for r in rows:
            ts = r["created_at"][:16]  # trim to minute
            lines.append(
                f"{ts} | {r['from_agent']} -> {r['to_agent']}\n"
                f"  [{r['priority']}] {r['message_type']}: {r['payload_preview']}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_review(self, update, context) -> None:
        """Generate and send improvement review report: /review"""
        if not self._authorised(update):
            return

        conn = get_db(self.db_path)
        rows = conn.execute(
            "SELECT id, request_id, agent_id, title, category, priority, "
            "problem, impact, created_at "
            "FROM system_improvement_requests WHERE status = 'pending' "
            "ORDER BY priority DESC, category, id"
        ).fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("No pending improvement requests.")
            return

        # Group by priority then category
        grouped: dict[str, dict[str, list]] = {}
        for r in rows:
            pri = r["priority"]
            cat = r["category"]
            grouped.setdefault(pri, {}).setdefault(cat, []).append(r)

        lines = [f"<b>Improvement Review</b> ({len(rows)} pending)\n"]
        for priority in ["critical", "high", "normal", "low"]:
            if priority not in grouped:
                continue
            lines.append(f"\n<b>[{priority.upper()}]</b>")
            for category, items in sorted(grouped[priority].items()):
                lines.append(f"  <i>{category}</i>:")
                for item in items:
                    lines.append(
                        f"    #{item['id']} {item['title']}\n"
                        f"      Agent: {item['agent_id']} | {item['created_at'][:10]}"
                    )

        text = "\n".join(lines)
        # Split if too long for Telegram (4096 char limit)
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_improvements(self, update, context) -> None:
        """List pending improvements: /improvements"""
        if not self._authorised(update):
            return

        conn = get_db(self.db_path)
        rows = conn.execute(
            "SELECT id, request_id, title, category, priority, status "
            "FROM system_improvement_requests WHERE status = 'pending' "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("No pending improvements.")
            return

        lines = ["<b>Pending Improvements</b>\n"]
        for r in rows:
            lines.append(
                f"#{r['id']} [{r['priority']}] {r['category']}\n"
                f"  {r['title']}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_ship(self, update, context) -> None:
        """Mark an improvement as shipped: /ship <id>"""
        if not self._authorised(update):
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /ship <id>")
            return

        try:
            imp_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid improvement ID.")
            return

        now = datetime.now(timezone.utc).isoformat()
        conn = get_db(self.db_path)

        row = conn.execute(
            "SELECT id FROM system_improvement_requests WHERE id = ? AND status = 'pending'",
            (imp_id,),
        ).fetchone()
        if not row:
            conn.close()
            await update.message.reply_text(f"Improvement #{imp_id} not found or not pending.")
            return

        conn.execute(
            "UPDATE system_improvement_requests SET status = 'shipped', "
            "shipped_at = ?, status_note = 'Shipped via Telegram' WHERE id = ?",
            (now, imp_id),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(f"Improvement #{imp_id} marked as shipped.")

    async def _cmd_decline(self, update, context) -> None:
        """Decline an improvement: /decline <id> <note>"""
        if not self._authorised(update):
            return

        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /decline <id> <note>")
            return

        try:
            imp_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid improvement ID.")
            return

        note = " ".join(args[1:])
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db(self.db_path)

        row = conn.execute(
            "SELECT id FROM system_improvement_requests WHERE id = ? AND status = 'pending'",
            (imp_id,),
        ).fetchone()
        if not row:
            conn.close()
            await update.message.reply_text(f"Improvement #{imp_id} not found or not pending.")
            return

        conn.execute(
            "UPDATE system_improvement_requests SET status = 'declined', "
            "reviewed_at = ?, status_note = ? WHERE id = ?",
            (now, note, imp_id),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(f"Improvement #{imp_id} declined: {note}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Build and start the Telegram bot application (blocking)."""
        from telegram.ext import Application, CommandHandler

        self._app = Application.builder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("requests", self._cmd_requests))
        self._app.add_handler(CommandHandler("resolve", self._cmd_resolve))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("cycle", self._cmd_cycle))
        self._app.add_handler(CommandHandler("agents", self._cmd_agents))
        self._app.add_handler(CommandHandler("messages", self._cmd_messages))
        self._app.add_handler(CommandHandler("review", self._cmd_review))
        self._app.add_handler(CommandHandler("improvements", self._cmd_improvements))
        self._app.add_handler(CommandHandler("ship", self._cmd_ship))
        self._app.add_handler(CommandHandler("decline", self._cmd_decline))

        logger.info("Telegram bot starting (chat_id=%s)", self.chat_id)
        self._app.run_polling()

    async def start_async(self) -> None:
        """Start the bot in non-blocking mode (for integration with existing event loops)."""
        from telegram.ext import Application, CommandHandler

        self._app = Application.builder().token(self.bot_token).build()

        self._app.add_handler(CommandHandler("requests", self._cmd_requests))
        self._app.add_handler(CommandHandler("resolve", self._cmd_resolve))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("cycle", self._cmd_cycle))
        self._app.add_handler(CommandHandler("agents", self._cmd_agents))
        self._app.add_handler(CommandHandler("messages", self._cmd_messages))
        self._app.add_handler(CommandHandler("review", self._cmd_review))
        self._app.add_handler(CommandHandler("improvements", self._cmd_improvements))
        self._app.add_handler(CommandHandler("ship", self._cmd_ship))
        self._app.add_handler(CommandHandler("decline", self._cmd_decline))

        logger.info("Telegram bot starting async (chat_id=%s)", self.chat_id)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    def stop(self) -> None:
        """Stop the bot gracefully."""
        if self._app is not None:
            logger.info("Telegram bot stopping")
            try:
                self._app.stop_running()
            except Exception:
                logger.exception("Error stopping Telegram bot")

    async def stop_async(self) -> None:
        """Stop the bot from an async context."""
        if self._app is not None:
            logger.info("Telegram bot stopping (async)")
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("Error stopping Telegram bot (async)")
