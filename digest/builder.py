"""Digest builder for the agentic quant trading system.

Assembles a structured text digest scoped to a specific agent, following
the format defined in BRIEF.md. Each section method is self-contained
and queries SQLite directly.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_collector.collector import compute_volatility_score
from database.schema import get_db
from logging_config import get_logger
from memory.retriever import MemoryRetriever

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DigestBuilder:
    """Builds per-agent digests from database state.

    Args:
        agent_id: Unique identifier for the agent receiving the digest.
        agent_config: Dict with keys such as role, namespace,
            capital_allocation_pct, strategy_namespace, etc.
        db_path: Path to the SQLite database file.
    """

    def __init__(self, agent_id: str, agent_config: dict, db_path: str):
        self.agent_id = agent_id
        self.agent_config = agent_config
        self.db_path = db_path
        self.logger = get_logger("digest.builder", agent_id=agent_id)

    # ------------------------------------------------------------------
    # Helper: empty-section collapsing
    # ------------------------------------------------------------------

    @staticmethod
    def _collapse_if_empty(section_name: str, content: str) -> str:
        """Return a collapsed single-line header if content is empty."""
        if not content.strip():
            return f"--- {section_name} --- (empty)"
        return f"--- {section_name} ---\n{content}"

    # ------------------------------------------------------------------
    # Helper: get last cycle timestamp for this agent
    # ------------------------------------------------------------------

    def _get_last_cycle_timestamp(self, conn: sqlite3.Connection) -> str | None:
        """Return the ISO timestamp of the most recent cycle event for this agent."""
        row = conn.execute(
            """
            SELECT timestamp FROM events
            WHERE agent_id = ? AND event_type = 'cycle_complete'
            ORDER BY timestamp DESC LIMIT 1
            """,
            (self.agent_id,),
        ).fetchone()
        return row["timestamp"] if row else None

    # ------------------------------------------------------------------
    # 1. Portfolio section
    # ------------------------------------------------------------------

    def build_portfolio_section(self, agent_id: str) -> str:
        """Show positions and allocation.

        Quant agents see only their own positions. PM role sees all positions.

        Args:
            agent_id: The agent whose portfolio to display.

        Returns:
            Formatted portfolio section string.
        """
        conn = get_db(self.db_path)
        try:
            role = self.agent_config.get("role", "quant")

            # Read cached total equity for the summary line
            total_equity = 0.0
            eq_row = conn.execute(
                "SELECT value FROM system_state WHERE key = 'portfolio_value_usd'"
            ).fetchone()
            if eq_row:
                try:
                    total_equity = float(json.loads(eq_row["value"]))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            capital_allocated = self.agent_config.get("capital_allocated", 0.0)

            if role == "portfolio_manager":
                # PM sees all positions
                rows = conn.execute(
                    """
                    SELECT t.agent_id, t.strategy_id, t.pair, t.action, t.size_usd,
                           t.price, t.fill_price, t.pnl, t.paper, t.timestamp
                    FROM trades t
                    WHERE t.status = 'filled'
                      AND t.action IN ('buy', 'sell')
                      AND NOT EXISTS (
                          SELECT 1 FROM trades t2
                          WHERE t2.strategy_id = t.strategy_id
                            AND t2.pair = t.pair
                            AND t2.action = 'close'
                            AND t2.status = 'filled'
                            AND t2.timestamp > t.timestamp
                      )
                    ORDER BY t.agent_id, t.pair
                    """
                ).fetchall()
            else:
                # Quant/risk agents see only own positions
                rows = conn.execute(
                    """
                    SELECT t.agent_id, t.strategy_id, t.pair, t.action, t.size_usd,
                           t.price, t.fill_price, t.pnl, t.paper, t.timestamp
                    FROM trades t
                    WHERE t.agent_id = ?
                      AND t.status = 'filled'
                      AND t.action IN ('buy', 'sell')
                      AND NOT EXISTS (
                          SELECT 1 FROM trades t2
                          WHERE t2.strategy_id = t.strategy_id
                            AND t2.pair = t.pair
                            AND t2.action = 'close'
                            AND t2.status = 'filled'
                            AND t2.timestamp > t.timestamp
                      )
                    ORDER BY t.pair
                    """,
                    (agent_id,),
                ).fetchall()

            equity_line = (
                f"Total equity: ${total_equity:,.2f} | "
                f"Agent capital: ${capital_allocated:,.2f}"
            )

            if not rows:
                content = f"{equity_line}\nOpen positions: 0 (all cash)"
                return self._collapse_if_empty("PORTFOLIO STATE", content)

            lines = []
            total_exposure = 0.0
            total_pnl = 0.0
            for r in rows:
                mode = "paper" if r["paper"] else "live"
                pnl = r["pnl"] or 0.0
                total_exposure += r["size_usd"]
                total_pnl += pnl
                agent_prefix = f"[{r['agent_id']}] " if role == "portfolio_manager" else ""
                lines.append(
                    f"  {agent_prefix}{r['pair']} | {r['action']} | "
                    f"${r['size_usd']:.2f} | entry ${r['price']:.2f} | "
                    f"PnL ${pnl:.2f} | {r['strategy_id']} | {mode}"
                )

            summary = f"Open positions: {len(rows)} | Exposure: ${total_exposure:.2f} | Unrealized PnL: ${total_pnl:.2f}"
            content = equity_line + "\n" + summary + "\n" + "\n".join(lines)
            return self._collapse_if_empty("PORTFOLIO STATE", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 2. Benchmark section
    # ------------------------------------------------------------------

    def build_benchmark_section(self) -> str:
        """Render all benchmarks with current value and 24h/7d/30d performance.

        Reads benchmark_* keys from system_state (written by BenchmarkTracker)
        and formats performance data from each benchmark's history.

        Returns:
            Formatted benchmark section string.
        """
        from benchmarks.tracker import BenchmarkTracker

        tracker = BenchmarkTracker(self.db_path)

        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT key FROM system_state WHERE key LIKE 'benchmark_%'"
            ).fetchall()
        finally:
            conn.close()

        def _fmt_pct(v: float | None) -> str:
            return f"{v * 100:+.1f}%" if v is not None else "n/a"

        lines = [
            f"  {'Benchmark':<25} {'Current':>10} {'Total':>8} {'24h':>8} {'7d':>8} {'30d':>8}",
            f"  {'-' * 25} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}",
        ]

        for row in rows:
            bench_id = row["key"][len("benchmark_"):]  # strip "benchmark_" prefix
            perf = tracker.get_benchmark_performance(bench_id)
            if perf is None:
                continue
            lines.append(
                f"  {bench_id:<25} "
                f"${perf['current_value']:>9.2f} "
                f"{_fmt_pct(perf['total_return']):>8} "
                f"{_fmt_pct(perf['return_24h']):>8} "
                f"{_fmt_pct(perf['return_7d']):>8} "
                f"{_fmt_pct(perf['return_30d']):>8}"
            )

        if len(lines) == 2:
            lines.append("  No benchmark data yet — seeding in progress")

        content = "\n".join(lines)
        return self._collapse_if_empty("BENCHMARK PERFORMANCE", content)

    # ------------------------------------------------------------------
    # 3. Strategy sections
    # ------------------------------------------------------------------

    def build_strategy_sections(self, agent_id: str, namespace: str) -> str:
        """Query strategy_registry filtered by namespace.

        Shows: live strategies, paper strategies, backtest queue
        (with status), hypothesis queue (research notes).

        Args:
            agent_id: Agent whose strategies to show.
            namespace: Strategy namespace filter.

        Returns:
            Combined string of all strategy sub-sections.
        """
        conn = get_db(self.db_path)
        try:
            role = self.agent_config.get("role", "quant")

            # Fetch strategies -- PM sees all, others see own namespace
            if role == "portfolio_manager":
                strategies = conn.execute(
                    """
                    SELECT strategy_id, namespace, stage, created_at, updated_at,
                           config, backtest_results, robustness_results, paper_results
                    FROM strategy_registry
                    ORDER BY namespace, stage, updated_at DESC
                    """
                ).fetchall()
            else:
                strategies = conn.execute(
                    """
                    SELECT strategy_id, namespace, stage, created_at, updated_at,
                           config, backtest_results, robustness_results, paper_results
                    FROM strategy_registry
                    WHERE namespace = ?
                    ORDER BY stage, updated_at DESC
                    """,
                    (namespace,),
                ).fetchall()

            # Group by stage
            live = [s for s in strategies if s["stage"] == "live"]
            paper = [s for s in strategies if s["stage"] == "paper"]
            backtest = [s for s in strategies if s["stage"] in ("backtest", "robustness")]
            hypothesis = [s for s in strategies if s["stage"] == "hypothesis"]
            graveyard = [s for s in strategies if s["stage"] == "graveyard"]

            sections = []

            # Live strategies
            live_lines = []
            for s in live:
                config_data = json.loads(s["config"]) if s["config"] else {}
                paper_res = json.loads(s["paper_results"]) if s["paper_results"] else {}
                live_lines.append(
                    f"  {s['strategy_id']} | ns:{s['namespace']} | "
                    f"since {s['updated_at']} | "
                    f"config: {json.dumps(config_data, separators=(',', ':'))}"
                )
            sections.append(self._collapse_if_empty("LIVE STRATEGIES", "\n".join(live_lines)))

            # Paper strategies
            paper_lines = []
            for s in paper:
                paper_res = json.loads(s["paper_results"]) if s["paper_results"] else {}
                paper_lines.append(
                    f"  {s['strategy_id']} | ns:{s['namespace']} | "
                    f"since {s['updated_at']} | "
                    f"results: {json.dumps(paper_res, separators=(',', ':'))}"
                )
            sections.append(self._collapse_if_empty("PAPER STRATEGIES", "\n".join(paper_lines)))

            # Backtest queue
            bt_lines = []
            for s in backtest:
                bt_res = json.loads(s["backtest_results"]) if s["backtest_results"] else {}
                rob_res = json.loads(s["robustness_results"]) if s["robustness_results"] else {}
                status_detail = f"stage:{s['stage']}"
                if rob_res:
                    status_detail += f" | robustness: {json.dumps(rob_res, separators=(',', ':'))}"
                elif bt_res:
                    status_detail += f" | backtest: {json.dumps(bt_res, separators=(',', ':'))}"
                bt_lines.append(
                    f"  {s['strategy_id']} | {status_detail} | submitted {s['created_at']}"
                )
            sections.append(self._collapse_if_empty("BACKTEST QUEUE", "\n".join(bt_lines)))

            # Hypothesis queue (from research_notes)
            if role == "portfolio_manager":
                notes = conn.execute(
                    """
                    SELECT note_id, agent_id, observation, potential_edge,
                           status, age_cycles, created_at
                    FROM research_notes
                    WHERE status IN ('active', 'research')
                    ORDER BY age_cycles DESC
                    """
                ).fetchall()
            else:
                notes = conn.execute(
                    """
                    SELECT note_id, agent_id, observation, potential_edge,
                           status, age_cycles, created_at
                    FROM research_notes
                    WHERE agent_id = ? AND status IN ('active', 'research')
                    ORDER BY age_cycles DESC
                    """,
                    (agent_id,),
                ).fetchall()

            from claude_interface.parser import MAX_RESEARCH_NOTES

            hyp_lines = []
            if len(notes) >= MAX_RESEARCH_NOTES:
                hyp_lines.append(
                    f"  ** WARNING: Research note cap reached ({len(notes)}/{MAX_RESEARCH_NOTES}). "
                    f"Abandon or promote notes before adding new ones. **"
                )
            elif len(notes) >= MAX_RESEARCH_NOTES - 2:
                hyp_lines.append(
                    f"  ** Note: {len(notes)}/{MAX_RESEARCH_NOTES} research notes active. "
                    f"Consider consolidating. **"
                )
            for n in notes:
                expiry_warning = ""
                if n["age_cycles"] >= 8:
                    expiry_warning = " ** EXPIRING SOON (age >= 8 cycles) **"
                agent_prefix = f"[{n['agent_id']}] " if role == "portfolio_manager" else ""
                hyp_lines.append(
                    f"  {agent_prefix}{n['note_id']} | age: {n['age_cycles']} cycles | "
                    f"{n['observation'][:120]}{expiry_warning}"
                )
            sections.append(self._collapse_if_empty("HYPOTHESIS QUEUE", "\n".join(hyp_lines)))

            # Graveyard summary — flag strategies killed during the $0 capital period
            capital_fix_ts = None
            cap_row = conn.execute(
                "SELECT MIN(updated_at) as ts FROM system_state "
                "WHERE key = 'portfolio_value_usd'"
            ).fetchone()
            if cap_row and cap_row["ts"]:
                capital_fix_ts = cap_row["ts"]

            grave_lines = []
            for s in graveyard:
                config_data = json.loads(s["config"]) if s["config"] else {}
                reason = config_data.get("kill_reason", "unknown")
                zero_cap_flag = ""
                if capital_fix_ts and s["updated_at"] < capital_fix_ts:
                    zero_cap_flag = " ** KILLED DURING $0 CAPITAL PERIOD — RE-EVALUATE **"
                grave_lines.append(
                    f"  {s['strategy_id']} | killed {s['updated_at']} | reason: {reason}{zero_cap_flag}"
                )
            sections.append(self._collapse_if_empty("GRAVEYARD SUMMARY", "\n".join(grave_lines)))

            return "\n\n".join(sections)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 4. Market conditions
    # ------------------------------------------------------------------

    def build_market_conditions(self, pairs: list[str]) -> str:
        """Build market conditions for each monitored pair.

        For each pair: gets latest OHLCV data, computes volatility score,
        and includes latest supplementary feed values with source/freshness.

        Args:
            pairs: List of trading pairs to report on.

        Returns:
            Formatted market conditions section string.
        """
        if not pairs:
            return self._collapse_if_empty("MARKET CONDITIONS", "")

        def _close_at(conn, pair: str, before_ts: str) -> float | None:
            """Return the most recent 1h close price at or before before_ts."""
            row = conn.execute(
                "SELECT close FROM ohlcv_cache WHERE pair=? AND timeframe='1h' "
                "AND timestamp<=? ORDER BY timestamp DESC LIMIT 1",
                (pair, before_ts),
            ).fetchone()
            return float(row["close"]) if row else None

        conn = get_db(self.db_path)
        try:
            now_dt = datetime.now(timezone.utc)
            ts_24h = (now_dt - timedelta(hours=24)).isoformat()
            ts_7d  = (now_dt - timedelta(days=7)).isoformat()
            ts_30d = (now_dt - timedelta(days=30)).isoformat()

            lines = []
            for pair in pairs:
                # Latest OHLCV candle (1h)
                candle = conn.execute(
                    """
                    SELECT timestamp, open, high, low, close, volume
                    FROM ohlcv_cache
                    WHERE pair = ? AND timeframe = '1h'
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (pair,),
                ).fetchone()

                vol_score = compute_volatility_score(self.db_path, pair)

                if candle:
                    lines.append(
                        f"  {pair}:"
                    )
                    lines.append(
                        f"    Latest 1h: O={candle['open']:.2f} H={candle['high']:.2f} "
                        f"L={candle['low']:.2f} C={candle['close']:.2f} V={candle['volume']:.2f}"
                    )
                    lines.append(f"    As of: {candle['timestamp']}")

                    # Price changes vs historical closes
                    cn = float(candle["close"])
                    c24 = _close_at(conn, pair, ts_24h)
                    c7d = _close_at(conn, pair, ts_7d)
                    c30 = _close_at(conn, pair, ts_30d)

                    def _pct(now: float, prev: float | None) -> str:
                        return f"{(now / prev - 1) * 100:+.1f}%" if prev else "n/a"

                    lines.append(
                        f"    Change: 24h={_pct(cn, c24)}  7d={_pct(cn, c7d)}  30d={_pct(cn, c30)}"
                    )
                else:
                    lines.append(f"  {pair}:")
                    lines.append("    Latest 1h: no data")

                lines.append(f"    Volatility score: {vol_score:.1f}/100  (annualised vol ×100, cap=100)")

            # Supplementary feeds — split into standard and prediction market feeds
            _PREDICTION_MARKET_FEEDS = {"polymarket", "kalshi"}

            feed_rows = conn.execute(
                """
                SELECT sf.feed_name, sf.value, sf.metadata, sf.source, sf.timestamp
                FROM supplementary_feeds sf
                INNER JOIN (
                    SELECT feed_name, MAX(timestamp) as max_ts
                    FROM supplementary_feeds
                    GROUP BY feed_name
                ) latest ON sf.feed_name = latest.feed_name
                    AND sf.timestamp = latest.max_ts
                GROUP BY sf.feed_name
                ORDER BY sf.feed_name
                """
            ).fetchall()

            standard_feeds = [f for f in feed_rows if f["feed_name"] not in _PREDICTION_MARKET_FEEDS]
            pm_feed_names = list(dict.fromkeys(
                f["feed_name"] for f in feed_rows if f["feed_name"] in _PREDICTION_MARKET_FEEDS
            ))

            if standard_feeds:
                lines.append("")
                lines.append("  Supplementary feeds:")
                for f in standard_feeds:
                    try:
                        feed_ts = datetime.fromisoformat(f["timestamp"])
                        age_hours = (now_dt - feed_ts).total_seconds() / 3600
                        freshness = f"{age_hours:.1f}h ago"
                    except (ValueError, TypeError):
                        freshness = "unknown"

                    metadata_str = ""
                    if f["metadata"]:
                        try:
                            meta = json.loads(f["metadata"])
                            if isinstance(meta, dict):
                                metadata_str = f" | {json.dumps(meta, separators=(',', ':'))}"
                        except (json.JSONDecodeError, TypeError):
                            pass

                    lines.append(
                        f"    {f['feed_name']}: {f['value']} "
                        f"(source: {f['source']}, {freshness}){metadata_str}"
                    )

            # Prediction market feeds — one row per market in metadata
            for pm_feed_name in pm_feed_names:
                pm_rows = conn.execute(
                    """
                    SELECT value, metadata, timestamp
                    FROM supplementary_feeds
                    WHERE feed_name = ?
                    ORDER BY timestamp DESC
                    LIMIT 200
                    """,
                    (pm_feed_name,),
                ).fetchall()

                # Deduplicate: keep latest record per market_id
                seen: dict[str, dict] = {}
                for row in pm_rows:
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        continue
                    mid = meta.get("market_id", "")
                    if mid and mid not in seen:
                        seen[mid] = {"value": row["value"], "meta": meta, "ts": row["timestamp"]}

                if not seen:
                    continue

                lines.append("")
                lines.append(f"  Prediction Markets ({pm_feed_name}):")

                for mid, item in seen.items():
                    meta = item["meta"]
                    prob_pct = round(float(item["value"]) * 100, 1)
                    title = meta.get("market_title", mid)[:60]
                    delta_24h = meta.get("delta_24h")
                    delta_7d = meta.get("delta_7d")

                    # Build delta string
                    parts = []
                    if delta_24h is not None:
                        arrow = "↑" if delta_24h >= 0 else "↓"
                        parts.append(f"{arrow}{abs(delta_24h):.0f}pp/24h")
                    if delta_7d is not None:
                        arrow = "↑" if delta_7d >= 0 else "↓"
                        parts.append(f"{arrow}{abs(delta_7d):.0f}pp/7d")

                    delta_str = f" ({', '.join(parts)})" if parts else ""
                    res_date = meta.get("resolution_date", "")
                    res_str = f"  [resolves {res_date}]" if res_date else ""

                    lines.append(f"    {title}: {prob_pct}%{delta_str}{res_str}")

            content = "\n".join(lines)
            return self._collapse_if_empty("MARKET CONDITIONS", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 5. Agent messages section
    # ------------------------------------------------------------------

    def build_agent_messages_section(self, agent_id: str) -> str:
        """Show unread messages for this agent.

        Queries agent_messages WHERE to_agent = agent_id OR to_agent = 'all',
        status != 'read'.

        Args:
            agent_id: The receiving agent.

        Returns:
            Formatted agent messages section string.
        """
        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT from_agent, created_at, message_type, priority, payload
                FROM agent_messages
                WHERE (to_agent = ? OR to_agent = 'all')
                  AND status != 'read'
                ORDER BY
                    CASE priority
                        WHEN 'wake' THEN 0
                        WHEN 'high' THEN 1
                        ELSE 2
                    END,
                    created_at ASC
                """,
                (agent_id,),
            ).fetchall()

            if not rows:
                return self._collapse_if_empty("AGENT MESSAGES", "")

            lines = [f"[{len(rows)} unread message(s)]", ""]
            now = datetime.now(timezone.utc)
            for r in rows:
                try:
                    msg_ts = datetime.fromisoformat(r["created_at"])
                    age_hours = (now - msg_ts).total_seconds() / 3600
                    if age_hours < 1:
                        age_str = f"{int(age_hours * 60)}m ago"
                    else:
                        age_str = f"{age_hours:.1f}h ago"
                except (ValueError, TypeError):
                    age_str = "unknown"

                priority_str = f", priority: {r['priority']}" if r["priority"] != "normal" else ""
                lines.append(f"FROM: {r['from_agent']} ({age_str}{priority_str})")
                lines.append(f"Type: {r['message_type']}")

                # Parse payload
                try:
                    payload = json.loads(r["payload"])
                    if isinstance(payload, dict) and "content" in payload:
                        lines.append(f'"{payload["content"]}"')
                    else:
                        lines.append(f"{json.dumps(payload, separators=(',', ':'))}")
                except (json.JSONDecodeError, TypeError):
                    lines.append(f'"{r["payload"]}"')

                lines.append("")

            content = "\n".join(lines).rstrip()
            return self._collapse_if_empty("AGENT MESSAGES", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 6. System updates section
    # ------------------------------------------------------------------

    def build_system_updates_section(self, agent_id: str) -> str:
        """Show recent events since last cycle, owner request resolutions,
        and shipped improvements.

        Args:
            agent_id: The agent receiving the digest.

        Returns:
            Formatted system updates section string.
        """
        conn = get_db(self.db_path)
        try:
            last_cycle_ts = self._get_last_cycle_timestamp(conn)

            lines = []

            # Recent system events since last cycle
            if last_cycle_ts:
                events = conn.execute(
                    """
                    SELECT timestamp, event_type, source, payload
                    FROM events
                    WHERE timestamp > ?
                      AND event_type IN (
                          'owner_intervention', 'config_change', 'circuit_breaker',
                          'agent_enabled', 'agent_disabled', 'capital_reallocation',
                          'feed_activated', 'system_pause', 'system_resume'
                      )
                    ORDER BY timestamp ASC
                    """,
                    (last_cycle_ts,),
                ).fetchall()
            else:
                # First cycle -- show recent events from last 24h
                events = conn.execute(
                    """
                    SELECT timestamp, event_type, source, payload
                    FROM events
                    WHERE event_type IN (
                        'owner_intervention', 'config_change', 'circuit_breaker',
                        'agent_enabled', 'agent_disabled', 'capital_reallocation',
                        'feed_activated', 'system_pause', 'system_resume'
                    )
                    ORDER BY timestamp DESC LIMIT 20
                    """
                ).fetchall()

            if events:
                lines.append("Recent events:")
                for e in events:
                    try:
                        payload = json.loads(e["payload"])
                        detail = json.dumps(payload, separators=(",", ":"))
                    except (json.JSONDecodeError, TypeError):
                        detail = e["payload"]
                    lines.append(
                        f"  [{e['timestamp']}] {e['event_type']} | "
                        f"source: {e['source']} | {detail}"
                    )

            # Resolved owner requests for this agent
            resolved = conn.execute(
                """
                SELECT request_id, title, status, resolution_note, resolved_at
                FROM owner_requests
                WHERE agent_id = ? AND status IN ('resolved', 'declined')
                ORDER BY resolved_at DESC LIMIT 10
                """,
                (agent_id,),
            ).fetchall()

            if resolved:
                if lines:
                    lines.append("")
                lines.append("Owner request resolutions:")
                for r in resolved:
                    note = r["resolution_note"] or ""
                    lines.append(
                        f"  {r['request_id']}: {r['title']} -> {r['status']}"
                        f"{' | ' + note if note else ''}"
                    )

            # Shipped improvements for this agent
            shipped = conn.execute(
                """
                SELECT request_id, title, status_note, shipped_at
                FROM system_improvement_requests
                WHERE agent_id = ? AND status = 'shipped'
                ORDER BY shipped_at DESC LIMIT 10
                """,
                (agent_id,),
            ).fetchall()

            if shipped:
                if lines:
                    lines.append("")
                lines.append("Shipped improvements:")
                for s in shipped:
                    note = s["status_note"] or ""
                    lines.append(
                        f"  {s['request_id']}: {s['title']}"
                        f"{' | ' + note if note else ''}"
                    )

            # Pending improvement requests for this agent
            pending = conn.execute(
                """
                SELECT request_id, title, status, priority
                FROM system_improvement_requests
                WHERE agent_id = ? AND status IN ('pending', 'in_progress')
                ORDER BY
                    CASE priority
                        WHEN 'high' THEN 0
                        WHEN 'normal' THEN 1
                        ELSE 2
                    END
                """,
                (agent_id,),
            ).fetchall()

            if pending:
                if lines:
                    lines.append("")
                lines.append("Pending improvement requests:")
                for p in pending:
                    lines.append(
                        f"  {p['request_id']}: {p['title']} [{p['status']}] "
                        f"(priority: {p['priority']})"
                    )

            content = "\n".join(lines)
            return self._collapse_if_empty("SYSTEM UPDATES", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 7. Risk gate log section
    # ------------------------------------------------------------------

    def build_risk_gate_log_section(self, agent_id: str) -> str:
        """Show rejected instructions since last cycle with reasons.

        Args:
            agent_id: The agent whose rejections to display.

        Returns:
            Formatted risk gate log section string.
        """
        conn = get_db(self.db_path)
        try:
            last_cycle_ts = self._get_last_cycle_timestamp(conn)

            if last_cycle_ts:
                rows = conn.execute(
                    """
                    SELECT created_at, instruction_type, payload, risk_check_result
                    FROM instruction_queue
                    WHERE agent_id = ?
                      AND status = 'rejected'
                      AND created_at > ?
                    ORDER BY created_at ASC
                    """,
                    (agent_id, last_cycle_ts),
                ).fetchall()
            else:
                # First cycle -- show recent rejections
                rows = conn.execute(
                    """
                    SELECT created_at, instruction_type, payload, risk_check_result
                    FROM instruction_queue
                    WHERE agent_id = ?
                      AND status = 'rejected'
                    ORDER BY created_at DESC LIMIT 20
                    """,
                    (agent_id,),
                ).fetchall()

            if not rows:
                return self._collapse_if_empty("RISK GATE LOG", "")

            lines = []
            for r in rows:
                try:
                    payload = json.loads(r["payload"])
                    payload_summary = json.dumps(payload, separators=(",", ":"))
                except (json.JSONDecodeError, TypeError):
                    payload_summary = r["payload"]

                try:
                    result = json.loads(r["risk_check_result"])
                    reason = result.get("reason", str(result))
                except (json.JSONDecodeError, TypeError):
                    reason = r["risk_check_result"] or "unknown"

                lines.append(
                    f"  [{r['created_at']}] {r['instruction_type']} | "
                    f"REJECTED: {reason}"
                )
                lines.append(f"    payload: {payload_summary}")

            content = "\n".join(lines)
            return self._collapse_if_empty("RISK GATE LOG", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 8. Recent trades section
    # ------------------------------------------------------------------

    def _build_recent_trades_section(self, agent_id: str) -> str:
        """Show recent trades for this agent (live and paper).

        Args:
            agent_id: The agent whose trades to display.

        Returns:
            Formatted recent trades section string.
        """
        conn = get_db(self.db_path)
        try:
            last_cycle_ts = self._get_last_cycle_timestamp(conn)

            if last_cycle_ts:
                rows = conn.execute(
                    """
                    SELECT timestamp, pair, action, size_usd, price, fill_price,
                           pnl, paper, strategy_id, rationale, status
                    FROM trades
                    WHERE agent_id = ? AND timestamp > ?
                    ORDER BY timestamp DESC
                    """,
                    (agent_id, last_cycle_ts),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT timestamp, pair, action, size_usd, price, fill_price,
                           pnl, paper, strategy_id, rationale, status
                    FROM trades
                    WHERE agent_id = ?
                    ORDER BY timestamp DESC LIMIT 20
                    """,
                    (agent_id,),
                ).fetchall()

            if not rows:
                return self._collapse_if_empty("RECENT TRADES", "")

            lines = []
            for r in rows:
                mode = "paper" if r["paper"] else "live"
                pnl_str = f" PnL=${r['pnl']:.2f}" if r["pnl"] is not None else ""
                fill_str = f" fill=${r['fill_price']:.2f}" if r["fill_price"] is not None else ""
                lines.append(
                    f"  [{r['timestamp']}] {r['pair']} {r['action']} "
                    f"${r['size_usd']:.2f} @ ${r['price']:.2f}{fill_str}{pnl_str} | "
                    f"{r['strategy_id']} | {mode} | {r['status']}"
                )

            content = "\n".join(lines)
            return self._collapse_if_empty("RECENT TRADES", content)

        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 9. Relevant history section (Phase 8 — memory retrieval)
    # ------------------------------------------------------------------

    def build_relevant_history_section(
        self, agent_id: str, current_conditions: dict
    ) -> str:
        """Build the RELEVANT HISTORY section from long-term memory.

        Constructs a context query from current market conditions, active
        strategies, and recent events, then retrieves the top 5 most
        relevant historical records via MemoryRetriever.

        Args:
            agent_id: The agent whose memory to query.
            current_conditions: Dict with optional keys:
                - regime (str): current market regime
                - active_strategies (list[str]): names of active strategies
                - recent_events (list[str]): recent notable events
                - pairs (list[str]): monitored trading pairs
                - memory_query_hints (list[str]): hints from prior cycle output

        Returns:
            Formatted relevant history section string.
        """
        # Resolve memory storage path (absolute to avoid CWD issues)
        memory_dir = str(_PROJECT_ROOT / "memory" / "data")
        storage_path = os.path.join(memory_dir, f"{agent_id}.mv2")

        retriever = MemoryRetriever(storage_path=storage_path, agent_id=agent_id)

        # Build context query from current conditions
        query_parts = []

        regime = current_conditions.get("regime", "")
        if regime:
            query_parts.append(f"regime {regime}")

        strategies = current_conditions.get("active_strategies", [])
        if strategies:
            query_parts.append(f"strategies {' '.join(strategies)}")

        events = current_conditions.get("recent_events", [])
        if events:
            query_parts.append(" ".join(events[:3]))

        pairs = current_conditions.get("pairs", [])
        if pairs:
            query_parts.append(" ".join(pairs[:5]))

        # Use memory_query_hints from previous cycle output if available
        hints = current_conditions.get("memory_query_hints", [])
        if hints:
            query_parts.extend(hints[:3])

        if not query_parts:
            query_parts.append("recent market activity and strategy performance")

        query = " ".join(query_parts)
        self.logger.debug("Memory query for relevant history: %s", query)

        results = retriever.search(query=query, top_k=5)

        if not results:
            self.logger.debug("Search returned empty, trying get_recent()")
            results = retriever.get_recent(n=3)

        if not results:
            return self._collapse_if_empty("RELEVANT HISTORY", "")

        lines = []
        for r in results:
            cycle_num = r.get("cycle_number", "?")
            timestamp = r.get("timestamp", "unknown")
            summary = r.get("summary", "No summary")
            score = r.get("relevance_score", 0.0)

            lines.append(f"  [Cycle {cycle_num} | {timestamp} | relevance: {score:.2f}]")
            # Wrap summary lines with indentation
            summary_lines = summary.split(" | ")
            for sl in summary_lines:
                lines.append(f"    {sl.strip()}")
            lines.append("")

        content = "\n".join(lines).rstrip()
        return self._collapse_if_empty("RELEVANT HISTORY", content)

    # ------------------------------------------------------------------
    # 10. Prior cycle notes section
    # ------------------------------------------------------------------

    def build_prior_cycle_notes_section(self, agent_id: str) -> str:
        """Retrieve the most recent cycle_notes from the events table."""
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT payload FROM events "
                "WHERE agent_id = ? AND event_type = 'cycle_notes' "
                "ORDER BY timestamp DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            if not row:
                return ""
            try:
                data = json.loads(row["payload"])
                return data.get("cycle_notes", "")
            except (json.JSONDecodeError, TypeError):
                return row["payload"] if row["payload"] else ""
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 11. Requested analysis section (pre-computed z-scores)
    # ------------------------------------------------------------------

    def build_requested_analysis_section(self, agent_id: str) -> str:
        """Pre-compute cointegration spread z-scores for active pair strategies.

        Checks strategy_registry for non-graveyard strategies with exactly 2
        target_pairs, runs a lightweight cointegration analysis, and returns
        the current z-score so the agent doesn't burn a tool call each cycle.
        """
        try:
            from data_collector.analysis import AnalysisEngine
        except ImportError:
            return self._collapse_if_empty("REQUESTED ANALYSIS", "")

        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT strategy_id, config FROM strategy_registry "
                "WHERE agent_id = ? AND stage NOT IN ('graveyard') "
                "AND config IS NOT NULL",
                (agent_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return self._collapse_if_empty("REQUESTED ANALYSIS", "")

        engine = AnalysisEngine(self.db_path)
        lines = []

        for row in rows:
            try:
                config = json.loads(row["config"] or "{}")
                target_pairs = config.get("target_pairs", [])
                if len(target_pairs) != 2:
                    continue

                result = engine.cointegration(target_pairs, "4h", lookback_days=30)
                if "error" in result:
                    continue

                hedge_ratio = result["hedge_ratio"]
                intercept = result["intercept"]
                residual_mean = result["residual_mean"]
                residual_std = result["residual_std"]

                if residual_std < 1e-8:
                    continue

                # Get latest close prices for both pairs
                conn2 = get_db(self.db_path)
                try:
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

                pa = price_a["close"]
                pb = price_b["close"]
                current_spread = pa - (hedge_ratio * pb + intercept)
                z_score = (current_spread - residual_mean) / residual_std

                lines.append(
                    f"  {target_pairs[0]} vs {target_pairs[1]} "
                    f"({row['strategy_id']}): "
                    f"z = {z_score:+.2f} | spread = {current_spread:.1f} "
                    f"| mean = {residual_mean:.1f} | std = {residual_std:.1f} "
                    f"| hedge = {hedge_ratio:.4f}"
                )
            except Exception as exc:
                self.logger.debug(
                    "Skipping z-score for %s: %s", row["strategy_id"], exc
                )

        if lines:
            content = "Cointegration spread z-scores (pre-computed):\n" + "\n".join(lines)
        else:
            content = ""
        return self._collapse_if_empty("REQUESTED ANALYSIS", content)

    # ------------------------------------------------------------------
    # 11. Full digest assembly
    # ------------------------------------------------------------------

    def build_full_digest(
        self,
        agent_id: str,
        cycle_number: int,
        wake_reason: str,
        capital_allocated: float,
    ) -> str:
        """Assemble the complete digest with all sections.

        Args:
            agent_id: The agent receiving the digest.
            cycle_number: Current cycle number.
            wake_reason: Reason for this wake (scheduled, triggered, etc.).
            capital_allocated: Dollar amount of capital allocated to this agent.

        Returns:
            Full digest string following the format from BRIEF.md.
        """
        now = datetime.now(timezone.utc).isoformat()
        cap_pct = self.agent_config.get("capital_allocation_pct", 1.0)
        total_capital = capital_allocated / cap_pct if cap_pct > 0 else capital_allocated
        namespace = self.agent_config.get("strategy_namespace", agent_id)

        # Determine pairs to monitor -- from config or default
        pairs = self.agent_config.get("monitored_pairs", [])
        if not pairs:
            # Fall back to querying pairs with OHLCV data
            conn = get_db(self.db_path)
            try:
                pair_rows = conn.execute(
                    "SELECT DISTINCT pair FROM ohlcv_cache ORDER BY pair"
                ).fetchall()
                pairs = [r["pair"] for r in pair_rows]
            finally:
                conn.close()

        # Build header with restart awareness
        restart_info = ""
        try:
            conn_restart = get_db(self.db_path)
            # Get last cycle_complete timestamp for this agent
            last_complete = conn_restart.execute(
                "SELECT timestamp FROM events WHERE agent_id = ? "
                "AND event_type = 'cycle_complete' ORDER BY timestamp DESC LIMIT 1",
                (agent_id,),
            ).fetchone()

            if last_complete:
                last_ts = last_complete["timestamp"]
                # Check for system_start events since last cycle
                restarts = conn_restart.execute(
                    "SELECT timestamp FROM events WHERE event_type = 'system_start' "
                    "AND timestamp > ? ORDER BY timestamp DESC",
                    (last_ts,),
                ).fetchall()
                if restarts:
                    last_dt = datetime.fromisoformat(last_ts)
                    gap = datetime.now(timezone.utc) - last_dt
                    gap_hours = gap.total_seconds() / 3600
                    restart_info = (
                        f"\nSystem restarted since last cycle "
                        f"({len(restarts)} restart(s)). "
                        f"Time since last cycle: {gap_hours:.1f}h."
                    )
            conn_restart.close()
        except Exception:
            pass

        header = (
            f"=== AGENTIC QUANT DIGEST ===\n"
            f"Agent: {agent_id} | Cycle: {cycle_number} | {now}\n"
            f"Capital allocated: ${capital_allocated:.2f} "
            f"({cap_pct * 100:.0f}% of ${total_capital:.2f})\n"
            f"Wake reason: {wake_reason}"
            f"{restart_info}"
        )

        # Build current conditions context for memory retrieval
        current_conditions = {
            "pairs": pairs,
            "active_strategies": [],
            "recent_events": [],
            "memory_query_hints": [],
        }

        # Pull memory_query_hints from recent events if available
        conn_hints = get_db(self.db_path)
        try:
            hint_rows = conn_hints.execute(
                """SELECT payload FROM events
                   WHERE agent_id = ? AND event_type = 'memory_query_hints'
                   ORDER BY timestamp DESC LIMIT 1""",
                (agent_id,),
            ).fetchall()
            for row in hint_rows:
                try:
                    hints_data = json.loads(row["payload"])
                    if isinstance(hints_data, dict):
                        current_conditions["memory_query_hints"] = hints_data.get("hints", [])
                    elif isinstance(hints_data, list):
                        current_conditions["memory_query_hints"] = hints_data
                except (json.JSONDecodeError, TypeError):
                    pass
        finally:
            conn_hints.close()

        # Build relevant history section
        try:
            relevant_history = self.build_relevant_history_section(
                agent_id, current_conditions
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to build relevant history section: %s", exc
            )
            relevant_history = self._collapse_if_empty("RELEVANT HISTORY", "")

        # Assemble sections
        sections = [
            header,
            "",
            self.build_portfolio_section(agent_id),
            "",
            self.build_benchmark_section(),
            "",
            self.build_strategy_sections(agent_id, namespace),
            "",
            self._build_recent_trades_section(agent_id),
            "",
            self.build_market_conditions(pairs),
            "",
            self.build_requested_analysis_section(agent_id),
            "",
            relevant_history,
            "",
            self.build_agent_messages_section(agent_id),
            "",
            self._collapse_if_empty("PRIOR CYCLE NOTES", self.build_prior_cycle_notes_section(agent_id)),
            "",
            self._collapse_if_empty("PENDING OWNER REQUESTS", ""),
            "",
            self._collapse_if_empty("SYSTEM HEALTH", ""),
            "",
            self.build_system_updates_section(agent_id),
            "",
            self.build_risk_gate_log_section(agent_id),
        ]

        return "\n".join(sections)
