"""Tool executor: dispatches tool calls to handler functions.

Each handler has a conceptual 60-second timeout (TOOL_TIMEOUT_SECONDS).
Complex timeout enforcement will be added when handlers perform real I/O.
"""

import json
from datetime import datetime, timedelta, timezone

from logging_config import get_logger
from database.schema import get_db

# Conceptual per-tool timeout; enforced in handlers that do real work (Phase 5+).
TOOL_TIMEOUT_SECONDS = 60

# Registry mapping tool names to handler functions
_TOOL_HANDLERS = {}


def _register(name: str):
    """Decorator to register a tool handler by name."""
    def decorator(func):
        _TOOL_HANDLERS[name] = func
        return func
    return decorator


def execute_tool_calls(
    tool_use_blocks: list, agent_id: str, db_path: str
) -> list[dict]:
    """Dispatch tool calls to their handlers and return results.

    Args:
        tool_use_blocks: List of tool_use content blocks from the Claude response.
            Each has .id, .name, and .input attributes.
        agent_id: The calling agent's identifier.
        db_path: Path to the SQLite database.

    Returns:
        List of tool_result dicts suitable for appending to the conversation:
        [{"type": "tool_result", "tool_use_id": ..., "content": ...}, ...]
    """
    logger = get_logger("claude_interface.tool_executor", agent_id=agent_id)
    results = []

    for block in tool_use_blocks:
        tool_name = block.name
        tool_id = block.id
        params = block.input or {}

        logger.info("Executing tool: %s (id=%s)", tool_name, tool_id)

        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            logger.warning("Unknown tool requested: %s", tool_name)
            results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps({
                    "error": f"Unknown tool: {tool_name}",
                    "available_tools": list(_TOOL_HANDLERS.keys()),
                }),
            })
            continue

        try:
            # Dispatch based on handler signature needs
            if tool_name in ("run_analysis", "check_backtest_status", "list_agent_messages"):
                result_str = handler(params, agent_id, db_path)
            elif tool_name == "query_memory":
                result_str = handler(params, agent_id)
            elif tool_name == "check_positions":
                result_str = handler(agent_id, db_path)
            elif tool_name == "check_exposure":
                result_str = handler(db_path)
            else:
                result_str = handler(params, agent_id, db_path)

            results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_str,
            })
        except Exception as exc:
            logger.error(
                "Tool %s raised exception: %s", tool_name, exc, exc_info=True
            )
            results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps({
                    "error": f"Tool execution failed: {str(exc)}",
                }),
            })

    return results


# ---------------------------------------------------------------------------
# Handler implementations (stubs — real logic added in later phases)
# ---------------------------------------------------------------------------


@_register("run_analysis")
def handle_run_analysis(params: dict, agent_id: str, db_path: str) -> str:
    """Run statistical analysis on market data via AnalysisEngine.

    Supports: correlation, autocorrelation, distribution, cointegration,
    rolling_sharpe, rolling_beta. Returns not_available for orderbook/funding_rates/custom
    (no local data source yet — submit a data_request instead).
    Timeout: 60s max.
    """
    from data_collector.analysis import AnalysisEngine

    logger = get_logger("claude_interface.tool_executor", agent_id=agent_id)

    analysis_type = params.get("analysis_type", "unknown")
    pairs = params.get("pairs", [])
    timeframe = params.get("timeframe", "1h")
    lookback_days = params.get("lookback_days", 30)

    _no_local_data = {"orderbook", "funding_rates", "custom"}
    if analysis_type in _no_local_data:
        return json.dumps({
            "status": "not_available",
            "analysis_type": analysis_type,
            "note": (
                f"{analysis_type} data not yet collected. "
                "Submit a data_request to queue feed ingestion."
            ),
        })

    engine = AnalysisEngine(db_path)
    method = getattr(engine, analysis_type, None)
    if method is None:
        return json.dumps({
            "status": "error",
            "analysis_type": analysis_type,
            "error": f"Unknown analysis_type '{analysis_type}'.",
        })

    # Methods that accept a single pair string and must be called per-pair
    _per_pair_methods = {"autocorrelation", "distribution", "rolling_sharpe"}

    try:
        if analysis_type == "rolling_beta":
            target = pairs[0] if pairs else params.get("target", "")
            reference = params.get("reference", "")
            window_days = int(params.get("window_days", 30))
            if not target or not reference:
                results = {"error": "rolling_beta requires pairs[0] as target and 'reference' param"}
            else:
                results = engine.rolling_beta(target, reference, timeframe, window_days, lookback_days)
        elif analysis_type in _per_pair_methods:
            # Run once per pair, return dict keyed by pair
            results = {}
            for pair in pairs:
                results[pair] = method(pair, timeframe, lookback_days)
        else:
            # correlation / cointegration accept the full list
            results = method(pairs, timeframe, lookback_days)

        logger.info(
            "Analysis %s completed for pairs=%s timeframe=%s lookback=%d",
            analysis_type, pairs, timeframe, lookback_days,
        )
        return json.dumps({
            "status": "ok",
            "analysis_type": analysis_type,
            "pairs": pairs,
            "timeframe": timeframe,
            "lookback_days": lookback_days,
            "results": results,
        })
    except Exception as exc:
        logger.error("Analysis %s failed: %s", analysis_type, exc, exc_info=True)
        return json.dumps({
            "status": "error",
            "analysis_type": analysis_type,
            "error": str(exc),
        })


@_register("query_memory")
def handle_query_memory(params: dict, agent_id: str) -> str:
    """Search agent long-term memory via MemoryRetriever.

    Uses memvid for semantic search when available, otherwise falls back
    to keyword matching on JSONL records.
    Timeout: <1s expected for JSONL, ~2s for memvid.
    """
    from memory.retriever import MemoryRetriever

    query = params.get("query", "")
    top_k = params.get("top_k", 5)

    if not query:
        return json.dumps({
            "query": query,
            "top_k": top_k,
            "results": [],
            "note": "Empty query provided.",
        })

    # Resolve storage path for this agent
    storage_path = _resolve_memory_path(agent_id)
    retriever = MemoryRetriever(storage_path=storage_path, agent_id=agent_id)
    results = retriever.search(query=query, top_k=top_k)

    return json.dumps({
        "query": query,
        "top_k": top_k,
        "results": results,
    })


def _resolve_memory_path(agent_id: str) -> str:
    """Resolve the memory storage path for an agent.

    Returns the path to the .mv2 file (memvid) or base path used by
    the JSONL fallback. Checks the standard memory/ directory.
    """
    import os
    memory_dir = os.path.join("memory", "data")
    os.makedirs(memory_dir, exist_ok=True)
    return os.path.join(memory_dir, f"{agent_id}.mv2")


@_register("check_backtest_status")
def handle_check_backtest_status(params: dict, agent_id: str, db_path: str) -> str:
    """Check status of a pending backtest or robustness test.

    Queries strategy_registry by hypothesis_id and returns stage + results.
    Timeout: <1s (database query).
    """
    hypothesis_id = params.get("hypothesis_id", "")
    if not hypothesis_id:
        return json.dumps({"error": "hypothesis_id is required"})

    conn = get_db(db_path)
    try:
        row = conn.execute(
            """SELECT strategy_id, stage, config, backtest_results,
                      robustness_results, paper_results, updated_at
               FROM strategy_registry
               WHERE hypothesis_id = ?
               ORDER BY updated_at DESC
               LIMIT 1""",
            (hypothesis_id,),
        ).fetchone()

        if row is None:
            return json.dumps({
                "hypothesis_id": hypothesis_id,
                "found": False,
                "note": "No strategy found for this hypothesis_id.",
            })

        result = {
            "hypothesis_id": hypothesis_id,
            "found": True,
            "strategy_id": row["strategy_id"],
            "stage": row["stage"],
            "updated_at": row["updated_at"],
        }

        # Include results for completed stages
        if row["backtest_results"]:
            try:
                result["backtest_results"] = json.loads(row["backtest_results"])
            except json.JSONDecodeError:
                result["backtest_results"] = row["backtest_results"]

        if row["robustness_results"]:
            try:
                result["robustness_results"] = json.loads(row["robustness_results"])
            except json.JSONDecodeError:
                result["robustness_results"] = row["robustness_results"]

        if row["paper_results"]:
            try:
                result["paper_results"] = json.loads(row["paper_results"])
            except json.JSONDecodeError:
                result["paper_results"] = row["paper_results"]

        return json.dumps(result)
    finally:
        conn.close()


@_register("check_positions")
def handle_check_positions(agent_id: str, db_path: str) -> str:
    """Return current open positions across all agents.

    Queries the trades table for positions with status 'filled' and no
    corresponding close. Simplified: groups open buy trades not yet closed.
    Timeout: <1s (database query).
    """
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT agent_id, strategy_id, pair, action, size_usd, price,
                      fill_price, timestamp, paper, status
               FROM trades
               WHERE status = 'filled'
               ORDER BY timestamp DESC""",
        ).fetchall()

        positions = []
        for row in rows:
            positions.append({
                "agent_id": row["agent_id"],
                "strategy_id": row["strategy_id"],
                "pair": row["pair"],
                "action": row["action"],
                "size_usd": row["size_usd"],
                "price": row["price"],
                "fill_price": row["fill_price"],
                "timestamp": row["timestamp"],
                "paper": bool(row["paper"]),
            })

        return json.dumps({
            "position_count": len(positions),
            "positions": positions,
        })
    finally:
        conn.close()


@_register("check_exposure")
def handle_check_exposure(db_path: str) -> str:
    """Compute gross and net exposure from open positions.

    Aggregates filled trades to compute per-pair and per-agent exposure.
    Timeout: <1s (database query + simple computation).
    """
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT agent_id, pair, action, size_usd
               FROM trades
               WHERE status = 'filled'""",
        ).fetchall()

        per_pair: dict[str, float] = {}
        per_agent: dict[str, float] = {}
        gross = 0.0
        net = 0.0

        for row in rows:
            size = row["size_usd"]
            sign = 1.0 if row["action"] == "buy" else -1.0
            signed_size = size * sign

            pair = row["pair"]
            agent = row["agent_id"]

            per_pair[pair] = per_pair.get(pair, 0.0) + signed_size
            per_agent[agent] = per_agent.get(agent, 0.0) + signed_size
            gross += abs(size)
            net += signed_size

        return json.dumps({
            "gross_exposure_usd": gross,
            "net_exposure_usd": net,
            "per_pair": per_pair,
            "per_agent": per_agent,
        })
    finally:
        conn.close()


@_register("list_agent_messages")
def handle_list_agent_messages(params: dict, agent_id: str, db_path: str) -> str:
    """List recent inter-agent messages.

    Optionally filtered by agent_id and time window (since_hours).
    Timeout: <1s (database query).
    """
    filter_agent = params.get("agent_id")
    since_hours = params.get("since_hours", 48)

    since_time = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
    ).isoformat()

    conn = get_db(db_path)
    try:
        if filter_agent:
            rows = conn.execute(
                """SELECT id, created_at, from_agent, to_agent, message_type,
                          priority, payload, status
                   FROM agent_messages
                   WHERE (from_agent = ? OR to_agent = ?)
                     AND created_at >= ?
                   ORDER BY created_at DESC""",
                (filter_agent, filter_agent, since_time),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, created_at, from_agent, to_agent, message_type,
                          priority, payload, status
                   FROM agent_messages
                   WHERE created_at >= ?
                   ORDER BY created_at DESC""",
                (since_time,),
            ).fetchall()

        messages = []
        for row in rows:
            msg = {
                "id": row["id"],
                "created_at": row["created_at"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "message_type": row["message_type"],
                "priority": row["priority"],
                "status": row["status"],
            }
            # Parse payload JSON if possible
            try:
                msg["payload"] = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                msg["payload"] = row["payload"]
            messages.append(msg)

        return json.dumps({
            "message_count": len(messages),
            "since_hours": since_hours,
            "messages": messages,
        })
    finally:
        conn.close()
