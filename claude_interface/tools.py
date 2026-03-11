"""Tool schema definitions for Claude agent tool use.

Defines tool schemas in Anthropic API format, grouped by agent role.
Each tool has a name, description, and JSON Schema input_schema.
"""

COMMON_TOOLS = [
    {
        "name": "run_analysis",
        "description": (
            "Run statistical analysis on market data. Returns results in this cycle. "
            "Use for correlation, distribution, autocorrelation, cointegration. "
            "Only analyses completing in <60s."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "analysis_type": {
                    "type": "string",
                    "enum": [
                        "correlation",
                        "rolling_sharpe",
                        "autocorrelation",
                        "distribution",
                        "cointegration",
                        "orderbook",
                        "funding_rates",
                        "custom",
                    ],
                },
                "pairs": {"type": "array", "items": {"type": "string"}},
                "timeframe": {"type": "string"},
                "lookback_days": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["analysis_type", "description"],
        },
    },
    {
        "name": "query_memory",
        "description": (
            "Search your long-term memory for relevant prior cycles. "
            "Use to check if you've tried this before, what happened in similar "
            "regimes, what you learned from similar strategies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
]

QUANT_TOOLS = COMMON_TOOLS + [
    {
        "name": "check_backtest_status",
        "description": "Check status of a pending backtest or robustness test.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hypothesis_id": {"type": "string"},
            },
            "required": ["hypothesis_id"],
        },
    }
]

RISK_TOOLS = COMMON_TOOLS + [
    {
        "name": "check_positions",
        "description": "Current position details across all agents.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_exposure",
        "description": (
            "Portfolio exposure breakdown: per-agent, per-pair, gross/net, correlation."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

PM_TOOLS = COMMON_TOOLS + [
    {
        "name": "list_agent_messages",
        "description": "Recent inter-agent messages and task status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "since_hours": {"type": "integer", "default": 48},
            },
        },
    },
    {
        "name": "check_positions",
        "description": "Current position details across all agents.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_exposure",
        "description": "Portfolio exposure: per-agent, per-pair, gross/net.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

AGENT_TOOLS = {
    "quant": QUANT_TOOLS,
    "risk_monitor": RISK_TOOLS,
    "portfolio_manager": PM_TOOLS,
}
