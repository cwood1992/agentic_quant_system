"""Tool schema definitions for Claude agent tool use.

Defines tool schemas in Anthropic API format, grouped by agent role.
Each tool has a name, description, and JSON Schema input_schema.
"""

COMMON_TOOLS = [
    {
        "name": "run_analysis",
        "description": (
            "Run statistical analysis on market data. Returns results in this cycle. "
            "Use for correlation, distribution, autocorrelation, cointegration, "
            "rolling_sharpe, rolling_beta. Only analyses completing in <60s."
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
                        "rolling_beta",
                        "ema",
                        "sma",
                        "orderbook",
                        "funding_rates",
                        "custom",
                    ],
                },
                "pairs": {"type": "array", "items": {"type": "string"}},
                "timeframe": {"type": "string"},
                "lookback_days": {"type": "integer"},
                "reference": {
                    "type": "string",
                    "description": "Reference pair for rolling_beta (e.g. 'BTC/USD')",
                },
                "window_days": {
                    "type": "integer",
                    "description": "Rolling window in days for rolling_beta (default: 30)",
                },
                "period": {
                    "type": "integer",
                    "description": "Period for ema/sma (default: 20). Common values: 9, 20, 50, 200.",
                },
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
        "name": "save_strategy_state",
        "description": (
            "Persist key-value state for a strategy between cycles. "
            "Use to track position side, entry prices, signal phase, etc. "
            "State survives restarts and is available in the next cycle's digest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy_id": {"type": "string"},
                "state": {
                    "type": "object",
                    "description": "Key-value pairs to persist (e.g. {\"position_side\": \"long_btc\", \"entry_price\": 74500})",
                },
            },
            "required": ["strategy_id", "state"],
        },
    },
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
    },
    {
        "name": "write_strategy_code",
        "description": (
            "Write a BaseStrategy subclass to a hypothesis file. Call this after "
            "emitting the hypothesis JSON (without inline code) to write the strategy "
            "implementation separately. The code is validated with compile() before "
            "writing. Use this instead of putting code in the hypothesis JSON to keep "
            "your response lean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "string",
                    "description": "The strategy_id matching the hypothesis (e.g. 'quant_primary_hyp_001_btc_momentum')",
                },
                "code": {
                    "type": "string",
                    "description": "Full Python source code for a BaseStrategy subclass. Must include imports.",
                },
            },
            "required": ["strategy_id", "code"],
        },
    },
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
