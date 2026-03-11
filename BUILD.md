# Agentic Quant System — Build Specification

> This document is for the builder (you + Claude Code). It is NOT included in any agent's runtime system prompt.
> Agent briefs live in `/workspace/briefs/`. This document tells you how to construct the system that calls them.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          PERSISTENT RUNTIME                             │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐   │
│  │ Data         │  │ Executor     │  │ Benchmark    │  │ Robustness │   │
│  │ Collector    │  │ Engine       │  │ Tracker      │  │ Tester     │   │
│  │ + Analysis   │  │ live/paper   │  │              │  │            │   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘   │
│         │                 │                 │                │          │
│         └─────────────────┴─────────┬───────┴────────────────┘          │
│                                     │                                   │
│                        ┌────────────▼────────────┐                      │
│                        │    Instruction Queue    │                      │
│                        │  + Portfolio Risk Gate  │                      │
│                        └────────────┬────────────┘                      │
│                                     │                                   │
│              ┌──────────────────────▼──────────────────────┐            │
│              │              Digest Builder                 │            │
│              │  (per-agent: scoped data + agent messages)  │            │
│              └──────────────────────┬──────────────────────┘            │
│                                     │                                   │
│              ┌──────────────────────▼──────────────────────┐            │
│              │            Wake Controller                  │            │
│              │  (per-agent schedules + inter-agent wakes)  │            │
│              └──────────────────────┬──────────────────────┘            │
│                                     │                                   │
└─────────────────────────────────────┼───────────────────────────────────┘
                                      │  (API call when wake condition met)
                           ┌──────────▼──────────┐
                           │    AGENT CALLER     │
                           │  (agentic tool loop │
                           │   per wake cycle)   │
                           └──────────┬──────────┘
                                      │  (structured JSON output)
                           ┌──────────▼──────────┐
                           │  Instruction Parser  │
                           │  + Strategy Manager  │
                           │  + Message Router    │
                           └──────────────────────┘
```

---

## Agent Hierarchy

The system supports multiple agent roles with different briefs, cadences, capital allocations, and tool access. Agents communicate asynchronously through a shared message bus (SQLite table). Any agent can wake any other agent by inserting a wake trigger.

```
                    ┌─────────────────────┐
                    │  PORTFOLIO MANAGER  │
                    │  (slow cadence)     │
                    │  allocates capital, │
                    │  resolves conflicts,│
                    │  sets risk budgets  │
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼────┐  ┌─────▼──────┐  ┌───▼──────────┐
    │ QUANT AGENT  │  │ QUANT AGENT│  │ RISK MONITOR │
    │ (micro)      │  │ (barbell)  │  │ (fast cadence│
    │              │  │            │  │  independent)│
    └──────────────┘  └────────────┘  └──────────────┘
```

### Agent Roles

| Role | Brief | Default Cadence | Capital | Wake Authority | Tool Access |
|------|-------|----------------|---------|----------------|-------------|
| `portfolio_manager` | `briefs/BRIEF_PM.md` | 24h (or on-demand) | Controls allocation | Can wake any agent | memory, analysis |
| `quant` (per-instance) | `briefs/BRIEF_QUANT_*.md` | 4–8h (self-managed) | Per allocation | Can wake PM (escalation) | memory, analysis, backtest_status |
| `risk_monitor` | `briefs/BRIEF_RISK.md` | 30min polling | None (read-only) | Can wake any agent | positions, exposure, memory |

### Agent Configuration

```yaml
# config.yaml
agents:
  portfolio_manager:
    role: portfolio_manager
    brief: briefs/BRIEF_PM.md
    memory: memory/portfolio_manager.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 24
    tools: [run_analysis, query_memory, list_agent_messages]
    enabled: false   # enable when ready for multi-agent

  quant_primary:
    role: quant
    brief: briefs/BRIEF_QUANT.md
    memory: memory/quant_primary.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 6
    capital_allocation_pct: 1.0   # 100% until other quant agents added
    strategy_namespace: "primary"
    tools: [run_analysis, query_memory, check_backtest_status]
    enabled: true

  # Example future agents (disabled):
  quant_micro:
    role: quant
    brief: briefs/BRIEF_QUANT_MICRO.md
    memory: memory/quant_micro.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 4
    capital_allocation_pct: 0.50
    strategy_namespace: "micro"
    tools: [run_analysis, query_memory, check_backtest_status]
    enabled: false

  quant_barbell:
    role: quant
    brief: briefs/BRIEF_QUANT_BARBELL.md
    memory: memory/quant_barbell.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 8
    capital_allocation_pct: 0.50
    strategy_namespace: "barbell"
    tools: [run_analysis, query_memory, check_backtest_status]
    enabled: false

  risk_monitor:
    role: risk_monitor
    brief: briefs/BRIEF_RISK.md
    memory: memory/risk_monitor.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 0.5  # 30 min — lightweight digest
    tools: [query_memory, check_positions, check_exposure]
    enabled: false   # enable after Phase 7
```

Capital allocations across all enabled quant agents must sum to <= 1.0. The system validates this on startup and refuses to run if violated.

---

## Inter-Agent Communication

### Agent Message Bus

Agents communicate asynchronously through a shared SQLite table. Messages are delivered in the recipient's next digest under an `AGENT MESSAGES` section.

```sql
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,          -- ISO timestamp
    from_agent TEXT NOT NULL,          -- sender agent_id
    to_agent TEXT NOT NULL,            -- recipient agent_id, or "all" for broadcast
    message_type TEXT NOT NULL,        -- see types below
    priority TEXT DEFAULT 'normal',    -- 'normal' | 'high' | 'wake'
    payload TEXT NOT NULL,             -- JSON content
    read_by_cycle INTEGER,            -- cycle when recipient read it (NULL = unread)
    expires_at TEXT,                   -- optional expiry
    status TEXT DEFAULT 'pending'      -- 'pending' | 'read' | 'responded' | 'expired'
);
```

### Message Types

| Type | Direction | Purpose |
|------|-----------|---------|
| `task_request` | PM → Quant | "Investigate X", "Reduce exposure to Y", "Kill strategy Z" |
| `task_response` | Quant → PM | Response to a task with results or status |
| `risk_alert` | Risk → Any | "Exposure limit approaching", "Correlation spike detected" |
| `escalation` | Quant → PM | "Need capital reallocation", "Cross-agent conflict detected" |
| `capital_update` | PM → Quant | "Your allocation changed to X%" |
| `info_share` | Any → Any | "FYI: I noticed X in the data" |
| `regime_broadcast` | PM → All | "Regime change: entering high_vol. Adjust accordingly." |
| `wake_request` | Any → Any | "Wake up and look at this" (triggers immediate wake) |

### Wake Authority

Any agent can wake any other agent by sending a message with `priority: 'wake'`. The wake controller checks for wake-priority messages during its polling loop (every 5 minutes) and fires an out-of-cycle wake for the recipient.

Wake authority is subject to the same rate limiting as all triggers — 30-minute cooldown, max fires per base window.

### How Messages Appear in the Digest

```
--- AGENT MESSAGES ---
[2 unread messages]

FROM: risk_monitor (2h ago, priority: high)
Type: risk_alert
"BTC correlation with ETH has spiked to 0.96 over the last 24h. Your long positions
on both pairs represent effectively doubled exposure. Consider reducing one leg."

FROM: portfolio_manager (18h ago, priority: normal)
Type: task_request | task_id: pm_task_042
"Your barbell strategy has been in paper for 3 weeks with flat performance. Provide
an assessment: continue, modify parameters, or kill? Respond via task_response."
```

### Agent Message Output Schema

Add to every agent's output JSON:

```json
{
  "agent_messages": [
    {
      "to_agent": "portfolio_manager",
      "message_type": "escalation",
      "priority": "high",
      "content": "Micro and barbell agents both want to go long BTC. Combined exposure would be 70%. Requesting conflict resolution.",
      "context": {}
    }
  ]
}
```

### Task Lifecycle

1. PM sends `task_request` with a `task_id`
2. Quant receives it in digest, works on it
3. Quant sends `task_response` referencing the same `task_id`
4. PM receives response in its next digest

Tasks don't block. If ignored for 3 cycles, surfaced as stale in PM digest. PM can re-send with higher priority.

---

## Instruction Queue and Portfolio Risk Gate

### Instruction Queue

Every signal from every agent flows through a shared queue before execution.

```sql
CREATE TABLE instruction_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    strategy_namespace TEXT NOT NULL,
    instruction_type TEXT NOT NULL,    -- 'signal' | 'strategy_action' | 'benchmark_action'
    payload TEXT NOT NULL,             -- JSON
    status TEXT DEFAULT 'pending',     -- 'pending' | 'approved' | 'rejected' | 'executed' | 'failed'
    risk_check_result TEXT,            -- JSON
    executed_at TEXT,
    execution_result TEXT              -- JSON
);
```

### Portfolio Risk Gate

```python
# risk/portfolio.py

def check_agent_limits(signal, agent_id, agent_positions, agent_capital):
    """Per-agent limits scoped to this agent's capital allocation."""
    if position_value_after(signal, agent_positions) > agent_capital:
        return False, "Would exceed agent capital allocation"
    if len(agent_positions) >= agent_config[agent_id].max_positions and signal.action == "buy":
        return False, "Agent at max concurrent positions"
    return True, "passed"

def check_global_limits(signal, agent_id, all_positions, portfolio_state):
    """Global limits across all agents. Override per-agent approvals."""
    gross = sum(abs(p.size_usd) for p in all_positions)
    if (gross + signal_size_usd(signal)) / portfolio_state.total_equity > GLOBAL_MAX_GROSS_EXPOSURE:
        return False, f"Global gross exposure would exceed {GLOBAL_MAX_GROSS_EXPOSURE*100}%"

    pair_exposure = sum(p.size_usd for p in all_positions if p.pair == signal.pair)
    if (pair_exposure + signal_size_usd(signal)) / portfolio_state.total_equity > GLOBAL_MAX_PAIR_EXPOSURE:
        return False, f"Global {signal.pair} exposure would exceed limit"

    # Cross-agent conflict detection (flag, don't block)
    conflicting = [p for p in all_positions
                   if p.pair == signal.pair
                   and p.agent_id != agent_id
                   and opposing_direction(p, signal)]
    if conflicting:
        log_conflict(signal, agent_id, conflicting)
        send_agent_message(from_agent="system", to_agent="portfolio_manager",
            message_type="risk_alert", priority="high",
            content=f"Cross-agent conflict on {signal.pair}")

    return True, "passed"

def check_and_approve(instruction_id):
    """Main entry point. Run all checks, update queue status."""
    instruction = get_instruction(instruction_id)
    signal = parse_signal(instruction.payload)
    agent_id = instruction.agent_id

    ok, reason = check_agent_limits(signal, agent_id, ...)
    if not ok:
        update_status(instruction_id, "rejected", reason)
        return

    ok, reason = check_global_limits(signal, agent_id, ...)
    if not ok:
        update_status(instruction_id, "rejected", reason)
        return

    update_status(instruction_id, "approved", "all checks passed")
```

Rejected instructions logged in events table and surfaced in originating agent's next digest.

---

## System Improvement Pipeline

Agents can request improvements to the system itself — new tools, analysis capabilities, data pipelines, bug fixes. These requests accumulate and are reviewed on a weekly cycle where the owner + Claude Code implement approved changes.

### Schema

```sql
CREATE TABLE system_improvement_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE NOT NULL,       -- agent-generated unique ID
    created_at TEXT NOT NULL,
    agent_id TEXT NOT NULL,                -- requesting agent
    cycle INTEGER NOT NULL,               -- cycle when requested
    title TEXT NOT NULL,
    problem TEXT NOT NULL,                 -- what the agent is trying to do
    impact TEXT NOT NULL,                  -- what this unblocks (references to hypotheses, etc.)
    category TEXT NOT NULL,               -- new_tool | tool_enhancement | data_pipeline | etc.
    priority TEXT DEFAULT 'normal',       -- high | normal | low
    examples TEXT,                        -- JSON array of usage examples
    status TEXT DEFAULT 'pending',        -- pending | approved | in_progress | shipped | declined
    status_note TEXT,                     -- reason for decline, or description of what shipped
    reviewed_at TEXT,                     -- when owner reviewed
    shipped_at TEXT,                      -- when implementation was deployed
    review_cycle INTEGER                  -- which review cycle handled this
);
```

### Review Cycle (Claude Code Scheduled Task)

Set up a scheduled Claude Code invocation (weekly, or on-demand via Telegram `/review`). The review cycle:

1. **Gather** — Query all `pending` improvement requests, grouped by category and priority
2. **Present** — Generate a summary for the owner: what's requested, by which agent, impact assessment, estimated effort
3. **Triage** — Owner approves, declines (with reason), or defers each request
4. **Implement** — Claude Code implements approved requests in priority order
5. **Notify** — Mark shipped requests in the table. The digest builder surfaces these in `SYSTEM UPDATES` for requesting agents

#### Claude Code Review Session Script

```bash
# /workspace/scripts/review_improvements.sh
# Called by cron or Claude Code scheduler, e.g. every Sunday at 10:00 AM

# 1. Generate review report
python /workspace/scripts/generate_review_report.py > /tmp/review_report.md

# 2. Claude Code opens an interactive session with the report
#    Owner reviews, approves/declines requests
#    Claude Code implements approved changes
#    (This is the human-in-the-loop step)

# 3. After implementation, mark shipped
python /workspace/scripts/mark_shipped.py --requests <approved_ids>
```

#### Review Report Format (generated for the owner)

```
=== SYSTEM IMPROVEMENT REVIEW ===
Week of: [date]
Pending requests: [count] ([high_count] high priority)

--- HIGH PRIORITY ---
[SIR-042] from quant_primary (cycle 38, 4 days ago)
  "Rolling beta computation"
  Problem: Can't assess factor exposure for momentum hypotheses.
  Impact: 3 research notes stalled waiting for this capability.
  Category: analysis_capability
  → APPROVE / DECLINE / DEFER

--- NORMAL PRIORITY ---
[SIR-039] from quant_primary (cycle 31, 7 days ago)
  "Orderbook imbalance signal in digest"
  Problem: Want to see bid/ask imbalance ratio alongside price data.
  Impact: Would inform a market-microstructure hypothesis.
  Category: digest_improvement
  → APPROVE / DECLINE / DEFER

--- PREVIOUSLY DEFERRED ---
[SIR-028] from quant_primary (cycle 20, deferred 1 week ago)
  "Deribit options flow integration"
  ...
```

### How Updates Appear in Agent Digest

```
--- SYSTEM UPDATES ---
[2 improvements shipped since your last cycle]

SHIPPED: Rolling beta computation (SIR-042)
  You now have a "rolling_beta" option in run_analysis.
  Usage: run_analysis(analysis_type="rolling_beta", pairs=["BTC/USD", "ETH/USD"],
         timeframe="4h", lookback_days=30, description="beta of ETH vs BTC")
  Returns: rolling beta series + current beta value

SHIPPED: Orderbook imbalance signal (SIR-039)
  MARKET CONDITIONS now includes bid/ask imbalance ratio for monitored pairs.
  Values > 1.5 indicate strong buying pressure; < 0.67 strong selling.

PENDING: [1 request in review queue]
  SIR-045: "Sentiment feed from Reddit/Twitter" — submitted cycle 41, priority: normal
```

### Telegram Integration

```
/review              — trigger an immediate review cycle (doesn't wait for schedule)
/improvements        — list all pending improvement requests with status
/ship <request_id>   — manually mark a request as shipped (if implemented outside review cycle)
/decline <id> <note> — decline a request with reason
```

### De-duplication

Before inserting a new request, the parser checks for existing requests with similar titles and overlapping problem descriptions. If a near-duplicate is found, the new request is merged — the impact field is updated to include the new context, and the priority is upgraded if the new request has higher priority. This prevents agents from submitting the same request every cycle when a capability gap persists.

### Agent Request Budgeting

To prevent agents from flooding the review queue, each agent is limited to 3 new improvement requests per cycle. This is a soft limit enforced by the parser — excess requests are logged but held for the next cycle. The limit is configurable in `config.yaml`:

```yaml
system_improvements:
  review_cadence: "weekly"          # or "biweekly" or "on_demand"
  review_day: "sunday"
  max_requests_per_agent_per_cycle: 3
  auto_decline_after_weeks: 8       # requests deferred for 8+ weeks auto-declined
```

---

## Agentic Tool Use Within Cycles

### Tool Definitions

```python
# claude_interface/tools.py

COMMON_TOOLS = [
    {
        "name": "run_analysis",
        "description": "Run statistical analysis on market data. Returns results in this cycle. Use for correlation, distribution, autocorrelation, cointegration. Only analyses completing in <60s.",
        "input_schema": {
            "type": "object",
            "properties": {
                "analysis_type": {"type": "string", "enum": ["correlation", "rolling_sharpe", "autocorrelation", "distribution", "cointegration", "orderbook", "funding_rates", "custom"]},
                "pairs": {"type": "array", "items": {"type": "string"}},
                "timeframe": {"type": "string"},
                "lookback_days": {"type": "integer"},
                "description": {"type": "string"}
            },
            "required": ["analysis_type", "description"]
        }
    },
    {
        "name": "query_memory",
        "description": "Search your long-term memory for relevant prior cycles. Use to check if you've tried this before, what happened in similar regimes, what you learned from similar strategies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5}
            },
            "required": ["query"]
        }
    }
]

QUANT_TOOLS = COMMON_TOOLS + [
    {
        "name": "check_backtest_status",
        "description": "Check status of a pending backtest or robustness test.",
        "input_schema": {
            "type": "object",
            "properties": {"hypothesis_id": {"type": "string"}},
            "required": ["hypothesis_id"]
        }
    }
]

RISK_TOOLS = COMMON_TOOLS + [
    {"name": "check_positions", "description": "Current position details across all agents.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_exposure", "description": "Portfolio exposure breakdown: per-agent, per-pair, gross/net, correlation.",
     "input_schema": {"type": "object", "properties": {}}}
]

PM_TOOLS = COMMON_TOOLS + [
    {"name": "list_agent_messages", "description": "Recent inter-agent messages and task status.",
     "input_schema": {"type": "object", "properties": {
         "agent_id": {"type": "string"}, "since_hours": {"type": "integer", "default": 48}}}},
    {"name": "check_positions", "description": "Current position details across all agents.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "check_exposure", "description": "Portfolio exposure: per-agent, per-pair, gross/net.",
     "input_schema": {"type": "object", "properties": {}}}
]

AGENT_TOOLS = {"quant": QUANT_TOOLS, "risk_monitor": RISK_TOOLS, "portfolio_manager": PM_TOOLS}
```

### Agentic Caller

```python
# claude_interface/caller.py

MAX_TOOL_ITERATIONS = 5
TOOL_TIMEOUT_SECONDS = 60

def call_agent(agent_id, agent_config, digest, wake_reason, prior_response=None):
    brief = Path(agent_config["brief"]).read_text()
    model = select_model(wake_reason, prior_response, agent_config)
    tools = AGENT_TOOLS.get(agent_config["role"], COMMON_TOOLS)
    messages = [{"role": "user", "content": digest}]

    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        try:
            response = client.messages.create(
                model=model, max_tokens=MAX_OUTPUT_TOKENS,
                system=[{"type": "text", "text": brief, "cache_control": {"type": "ephemeral"}}],
                messages=messages, tools=tools if tools else None,
            )
        except Exception as e:
            log_failed_cycle(agent_id, "", str(e), wake_reason, model)
            send_telegram_alert("🚨 SYSTEM ERROR", f"Agent {agent_id} API call failed: {e}")
            return None

        if response.stop_reason == "end_turn":
            return parse_final_response(response, agent_id, model)

        if response.stop_reason == "tool_use":
            tool_results = execute_tool_calls(response, agent_id)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue
        break

    return parse_final_response(response, agent_id, model)
```

### Model Routing

Route by wake reason + agent request. No retroactive auto-routing.

```python
def select_model(wake_reason, prior_response, agent_config):
    if wake_reason.startswith("trigger"):
        return agent_config.get("escalation_model", TRIGGER_MODEL)
    requested = (prior_response or {}).get("requested_model")
    if requested in (TRIGGER_MODEL, DEFAULT_MODEL):
        return requested
    return agent_config.get("default_model", DEFAULT_MODEL)
```

### Tool Response Time Rule

| Operation | Typical Time | Interface |
|-----------|-------------|-----------|
| Correlation matrix (<=5 pairs) | 2-5s | Sync tool |
| Distribution / autocorrelation | 1-3s | Sync tool |
| Cointegration test | 3-10s | Sync tool |
| Memory query | <1s | Sync tool |
| Position/exposure check | <1s | Sync tool |
| Full backtest (90d) | 2-10min | Async (next cycle) |
| Robustness testing (1000 runs) | 5-30min | Async (next cycle) |
| New data feed setup | Manual | Owner request |

### Error Recovery

If `call_agent` returns `None`, cycle logged as failed. No immediate retry. After 3 consecutive failed cycles for any agent, pause that agent and send blocking owner request.

---

## Strategy Lifecycle

```
Hypothesis → Backtest → Robustness Testing → Paper → Live
```

Forward-only progression. Demotion (live → paper) permitted. Any stage can terminate into graveyard.

### Stage 1 — Hypothesis

Agent produces hypothesis document + strategy module. Key schema fields (in addition to original): `expected_trade_frequency`, `minimum_trade_count`, `target_regimes`, and `robustness` thresholds within `success_criteria`.

### Stage 2 — Backtest

Backtest runner executes against 90+ days historical data. If results meet agent's stated success criteria, system automatically advances to robustness testing.

### Stage 3 — Robustness Testing

Automated. Two tests, 1000 runs each:

**Random Entry Test:** Replace entry signals with random entries at same frequency. Keep exit logic, sizing, costs identical. Report strategy's percentile rank vs random distribution.

```python
# strategies/robustness.py

def random_entry_test(strategy_class, data, original_trades, n_runs=1000, seed=42):
    """Returns percentile rankings for Sharpe and total return vs random entries."""
    rng = np.random.default_rng(seed)
    original_entry_count = len([t for t in original_trades if t.is_entry])
    original_metrics = compute_metrics(original_trades)
    random_metrics = []
    for _ in range(n_runs):
        random_entries = generate_random_entries(data, count=original_entry_count, rng=rng)
        random_trades = run_with_entries(strategy_class, data, random_entries)
        random_metrics.append(compute_metrics(random_trades))
    return {
        "sharpe_percentile": percentile_rank(original_metrics.sharpe, [m.sharpe for m in random_metrics]),
        "total_return_percentile": percentile_rank(original_metrics.total_return, [m.total_return for m in random_metrics]),
        "mean_random_sharpe": np.mean([m.sharpe for m in random_metrics]),
        "n_runs": n_runs,
    }
```

**Return Permutation Test:** Shuffle trade return sequence, recompute equity curves. Tests path dependency.

```python
def return_permutation_test(trade_returns, starting_capital, n_runs=1000, seed=42):
    """Returns percentile rankings for final equity and drawdown resilience."""
    rng = np.random.default_rng(seed)
    original_curve = compute_equity_curve(trade_returns, starting_capital)
    original_dd = max_drawdown(original_curve)
    original_final = original_curve[-1]
    shuffled_finals, shuffled_dds = [], []
    for _ in range(n_runs):
        shuffled = rng.permutation(trade_returns)
        curve = compute_equity_curve(shuffled, starting_capital)
        shuffled_finals.append(curve[-1])
        shuffled_dds.append(max_drawdown(curve))
    return {
        "final_equity_percentile": percentile_rank(original_final, shuffled_finals),
        "drawdown_resilience_percentile": percentile_rank(-original_dd, [-d for d in shuffled_dds]),
        "n_runs": n_runs,
    }
```

Results surfaced in digest. Robustness thresholds are advisory — agent decides promotion.

### Stage 4 — Paper Trading

Live prices, simulated fills (including minimum order sizes and slippage). Logged identically to live trades.

### Stage 5 — Live Deployment

Real capital via Kraken API. Subject to portfolio risk gate.

### Graveyard

Namespaced by agent: `/strategies/graveyard/{namespace}/`. Full failure documentation.

---

## Strategy Module Interface

```python
# strategies/base.py — never modified
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd

@dataclass
class Signal:
    action: str            # "buy" | "sell" | "close" | "hold"
    pair: str
    size_pct: float        # % of agent's available capital (0.0-1.0)
    order_type: str        # "limit" | "market"
    limit_price: Optional[float] = None
    rationale: str = ""

class BaseStrategy(ABC):
    @abstractmethod
    def name(self) -> str: ...          # {namespace}_{hypothesis_id}
    @abstractmethod
    def required_feeds(self) -> list[str]: ...
    @abstractmethod
    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]: ...
    def on_fill(self, fill: dict) -> None: ...
    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict: return {}
```

---

## Events Table

System-wide audit trail. All components write here.

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    agent_id TEXT,
    cycle INTEGER,
    source TEXT NOT NULL,
    payload TEXT NOT NULL    -- JSON
);
```

---

## Risk Limits (risk/limits.py)

System-enforced. No agent can override.

```python
MINIMUM_WAKE_CADENCE_HOURS = 1
MAXIMUM_WAKE_CADENCE_HOURS = 24
MAX_TRIGGER_FIRES_PER_BASE_WINDOW = 2
TRIGGER_COOLDOWN_MINUTES = 30
CIRCUIT_BREAKER_DRAWDOWN_PCT = 0.30    # 30% drawdown from high-water mark → close all, pause all
                                        # HWM tracked in system_state table, resets on owner /resume
POSITION_LOSS_TRIGGER_PCT = 0.25
GLOBAL_MAX_GROSS_EXPOSURE = 0.80
GLOBAL_MAX_PAIR_EXPOSURE = 0.50
GLOBAL_MAX_CONCURRENT_POSITIONS = 10
DEFAULT_MAX_POSITIONS_PER_AGENT = 5
DEFAULT_MODEL = "claude-sonnet-4-6"
TRIGGER_MODEL = "claude-opus-4-6"
MAX_MONTHLY_API_BUDGET_USD = 50
MAX_OUTPUT_TOKENS = 8000
AUTO_APPROVE_DATA_FEED_MONTHLY_USD = 10
BUDGET_REQUEST_THRESHOLD_USD = 10
MINIMUM_ORDER_USD = 5.0
ROBUSTNESS_N_RUNS = 1000
ROBUSTNESS_RANDOM_SEED = 42
```

---

## Built-in Triggers (always active, not agent-configurable)

- Any live position reaches -25% unrealized → immediate wake for owning agent
- Exchange connectivity lost > 30 min → wake all agents + owner alert
- API call fails 3x consecutively for any agent → pause that agent, alert owner
- Portfolio drawdown >= 30% from high-water mark → circuit breaker: close all positions across all agents, pause all trading, blocking owner request
- 3 consecutive failed cycles for any agent → pause that agent, alert owner

---

## Data Collector

### Exchange Data (Phase 1)

Start with: OHLCV for major Kraken pairs (1m, 1h, 4h, 1d), orderbook snapshots (top 10), funding rates, volume + VWAP.

### Historical Backfill (Phase 1, before first cycle)

The backtest runner needs 90+ days of history on day one. Kraken's API rate-limits historical candle pulls. Build a one-time backfill script that:

1. Pulls max available history for all monitored pairs at all timeframes
2. Respects rate limits (sleep between requests)
3. Populates the OHLCV cache in SQLite
4. Logs coverage gaps (some pairs may have less history)
5. Reports total data coverage per pair/timeframe when complete

```bash
# scripts/backfill_historical.sh
# Run once before Phase 6. Takes 30-60 minutes depending on pair count.
python /workspace/data_collector/backfill.py --pairs all --days 180 --timeframes 1m,1h,4h,1d
```

The collector then maintains this data incrementally on its normal polling schedule.

### Supplementary Feeds (extensible, agent-requested)

Non-price data uses a flexible schema separate from OHLCV. This handles everything from daily sentiment indices to irregular event streams.

```sql
CREATE TABLE supplementary_feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_name TEXT NOT NULL,        -- "fear_greed_index", "btc_exchange_inflow", etc.
    timestamp TEXT NOT NULL,
    value REAL,                     -- numeric value (NULL for complex data)
    metadata TEXT,                  -- JSON for structured/complex data points
    source TEXT NOT NULL,           -- "glassnode", "alternative.me", "fred", etc.
    resolution TEXT DEFAULT 'daily' -- "hourly", "daily", "weekly", "event"
);
CREATE INDEX idx_supp_feed_time ON supplementary_feeds(feed_name, timestamp);
```

Each supplementary feed is implemented as a plugin in `data_collector/feeds/`:

```python
# data_collector/feeds/base_feed.py
class SupplementaryFeed(ABC):
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def source(self) -> str: ...
    @abstractmethod
    def resolution(self) -> str: ...
    @abstractmethod
    def fetch(self) -> list[dict]: ...
    def requires_api_key(self) -> bool: return False
    def estimated_monthly_cost(self) -> float: return 0.0
```

When an agent requests a new feed via `data_requests`, the system checks:
1. Does a plugin for this feed type exist? → Activate it
2. Does it require an API key? → Create an `owner_request`
3. Does it exceed the auto-approve budget? → Create a `budget_approval` request
4. None of the above? → Log as a `system_improvement_request` for the next review cycle

The digest builder presents supplementary data under `MARKET CONDITIONS` alongside price data. Each feed's latest values are included with source attribution and data freshness timestamp.

### Feed Registry

```sql
CREATE TABLE feed_registry (
    feed_name TEXT PRIMARY KEY,
    feed_type TEXT NOT NULL,        -- "exchange" | "on_chain" | "sentiment" | "macro" | "derivatives" | "network" | "prediction_market" | "alternative"
    source TEXT NOT NULL,
    resolution TEXT NOT NULL,
    status TEXT DEFAULT 'active',   -- "active" | "paused" | "error" | "pending_approval"
    requested_by TEXT,              -- agent_id that requested it
    activated_at TEXT,
    last_fetch TEXT,
    error_count INTEGER DEFAULT 0,
    config TEXT                     -- JSON: API endpoints, params, credentials reference
);
```

### Prediction Market Feeds — Implementation Notes

Prediction markets are a special category of supplementary feed. Unlike most feeds where the raw value is the signal (Fear & Greed = 25), prediction market signal is primarily in the *delta* — how fast and how far a probability is moving.

Store raw probabilities in `supplementary_feeds` like any other feed. The feed plugin should also compute and store:
```json
// metadata field for prediction market entries
{
  "market_id": "polymarket_fed_rate_cut_june",
  "market_title": "Fed cuts rates in June 2026",
  "probability": 0.62,
  "probability_24h_ago": 0.41,
  "delta_24h": 0.21,
  "delta_7d": 0.35,
  "volume_24h_usd": 125000,
  "liquidity_usd": 450000,
  "category": "economics",
  "resolution_date": "2026-06-18"
}
```

The digest builder should present prediction markets with deltas highlighted, since that's what the agent cares about:
```
Prediction Markets (Polymarket):
  Fed cuts June:     62% (↑21pp/24h, ↑35pp/7d)  ← significant move
  BTC > $100K July:  44% (↓3pp/24h)
  SEC crypto regs:   71% (flat)
```

Only surface markets with significant recent movement (e.g., >10pp delta in 24h) or high relevance to monitored pairs. Don't flood the digest with flat markets — the agent can request specific markets via `data_requests` if it wants to track something closely.

**API sources:**

- Polymarket: `https://gamma-api.polymarket.com/` — free, no auth needed for market data. Poll hourly for active markets
- Kalshi: `https://trading-api.kalshi.com/trade-api/v2/` — requires free account for API access. Covers economic event contracts with precise resolution dates

### Analysis Engine (data_collector/analysis.py)

Dual interface — sync tools (<60s, within-cycle) and async requests (between cycles). The analysis engine can operate on both OHLCV data and supplementary feeds. When an agent requests analysis involving supplementary data, the engine joins across both tables by timestamp.

---

## Digest Builder

### Per-Agent Scoping

| Section | Quant Sees | Risk Monitor Sees | PM Sees |
|---------|-----------|-------------------|---------|
| Portfolio | Own allocation | Full portfolio | Full portfolio |
| Strategies | Own namespace | All (read-only) | All + comparative |
| Benchmarks | All | All | All |
| Market + supplementary | All | Abbreviated | All |
| Analysis | Own requests | N/A | All |
| Graveyard | Own + optional others | N/A | All |
| Agent messages | Own inbox | Own inbox | All traffic |
| System updates | Own requests + global | System-level | All |
| Owner requests | Own | System-level | All |

Empty sections collapse to single line.

### SYSTEM UPDATES Section

This section surfaces everything that changed in the system between cycles, not just improvement requests:

- **Owner interventions:** pauses, resumes, config changes, capital reallocations, circuit breaker clears
- **New data feeds activated:** what's now available, source, resolution
- **Shipped improvements:** new tools or capabilities, with usage instructions
- **Pending improvement requests:** agent's own requests with current status
- **Agent additions/removals:** if new agents were enabled or existing ones disabled

The digest builder assembles this from the events table (filtering for system-level events since the agent's last cycle) and the `system_improvement_requests` table.

---

## Memory System

Per-agent memvid `.mv2` files. Sequential frame structure preserves insertion order for temporal queries. Each agent's retriever searches its own file by default. Cross-agent memory access possible (PM can query quant memory) but not default.

```
/memory/
  quant_primary.mv2
  risk_monitor.mv2        # (future)
  portfolio_manager.mv2   # (future)
```

Each cycle produces a per-agent memory record encoding: cycle number, regime, active strategies, killed strategies, agent notes, key events, tool calls, messages sent/received.

`query_memory` tool provides synchronous within-cycle access. Automatic retrieval also injects `RELEVANT HISTORY` into digest.

Implement in Phase 8 after core loop is stable.

---

## Benchmarks

Seeded at $500: `hodl_btc`, `hodl_eth`, `dca_btc`, `equal_weight_rebal`. Shared across agents. Per-strategy counterfactuals scoped to owning agent.

---

## Exchange Configuration

Kraken. Trade-only API. No withdrawal. Both executors enforce `MINIMUM_ORDER_USD`. Rejected signals logged and surfaced in digest.

---

## Observability

### Telegram Messages

```
📊 CYCLE SUMMARY      — per agent per cycle
💰 TRADE EXECUTED     — live trade (tagged with agent)
📋 PAPER TRADE        — paper execution
🔬 HYPOTHESIS QUEUED  — new hypothesis
🧪 ROBUSTNESS DONE    — testing complete
✅ STRATEGY PROMOTED  — lifecycle advance
❌ STRATEGY KILLED    — graveyard
⚠️  TRIGGER WAKE       — condition-fired
🔀 AGENT MESSAGE      — inter-agent (PM tasks, risk alerts)
🔴 OWNER ACTION REQ   — blocking
🟡 OWNER REQUEST      — non-blocking
🚨 SYSTEM ERROR       — component failure
🛑 CIRCUIT BREAKER    — all trading paused
```

### Bot Commands

```
/requests              — pending owner requests
/resolve <id> [note]   — resolve request
/pause [agent_id]      — pause all or specific agent
/resume [agent_id]     — resume (also clears circuit breaker)
/status                — system + all agent states
/cycle <agent_id>      — force immediate wake
/agents                — list agents with status, cadence, capital
/messages              — recent inter-agent messages
/review                — trigger immediate system improvement review cycle
/improvements          — list pending improvement requests with status
/ship <request_id>     — manually mark improvement as shipped
/decline <id> <note>   — decline improvement request with reason
```

### Dashboard

Single HTML regenerated each cycle. Per-agent equity curves, cross-agent comparison, agent message log, robustness results, risk gate approvals/rejections.

---

## STATE.md

```markdown
# System State
Last updated: [timestamp]

## Global
Total equity: $[balance]
High-water mark: $[hwm]
Drawdown from HWM: [pct]%
Circuit breaker: [armed | triggered]
Active agents: [count] / [total]

## Per Agent
### [agent_id]
Status: [running | paused | error]
Cycle: [number]
Capital: $[amount] ([pct]%)
Active / Paper / Queued / Graveyard: [counts]
Consecutive failed cycles: [count]
Wake cadence: [base]h → [effective]h
Next wake: [timestamp]
Last notes: [verbatim]
```

---

## Operational Foundation

These items are infrastructure hygiene. None are architecturally complex, but skipping them will cause problems once the system runs unattended.

### Configuration Template (config.yaml)

```yaml
# config.yaml — copy to workspace, fill in secrets from environment

# === Exchange ===
exchange:
  name: kraken
  api_key: ${KRAKEN_API_KEY}          # resolved from environment
  api_secret: ${KRAKEN_API_SECRET}    # resolved from environment
  sandbox: false                       # no sandbox — paper trading uses live prices

# === Claude API ===
claude:
  api_key: ${ANTHROPIC_API_KEY}

# === Telegram ===
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  owner_chat_id: ${TELEGRAM_CHAT_ID}

# === Agents ===
agents:
  quant_primary:
    role: quant
    brief: briefs/BRIEF_QUANT.md
    memory: memory/quant_primary.mv2
    default_model: claude-sonnet-4-6
    escalation_model: claude-opus-4-6
    base_cadence_hours: 6
    capital_allocation_pct: 1.0
    strategy_namespace: "primary"
    tools: [run_analysis, query_memory, check_backtest_status]
    max_positions: 5
    enabled: true

  # Uncomment and configure when ready:
  # quant_micro:
  #   role: quant
  #   brief: briefs/BRIEF_QUANT_MICRO.md
  #   memory: memory/quant_micro.mv2
  #   default_model: claude-sonnet-4-6
  #   escalation_model: claude-opus-4-6
  #   base_cadence_hours: 4
  #   capital_allocation_pct: 0.50
  #   strategy_namespace: "micro"
  #   tools: [run_analysis, query_memory, check_backtest_status]
  #   max_positions: 5
  #   enabled: false

  # quant_barbell:
  #   role: quant
  #   brief: briefs/BRIEF_QUANT_BARBELL.md
  #   ...

  # portfolio_manager:
  #   role: portfolio_manager
  #   brief: briefs/BRIEF_PM.md
  #   ...

  # risk_monitor:
  #   role: risk_monitor
  #   brief: briefs/BRIEF_RISK.md
  #   base_cadence_hours: 0.5
  #   ...

# === Data Collection ===
data:
  monitored_pairs:
    - BTC/USD
    - ETH/USD
    - SOL/USD
    - AVAX/USD
    - LINK/USD
  timeframes: [1m, 1h, 4h, 1d]
  orderbook_depth: 10
  collection_interval_seconds: 60

# === System Improvement Reviews ===
system_improvements:
  review_cadence: weekly
  review_day: sunday
  max_requests_per_agent_per_cycle: 3
  auto_decline_after_weeks: 8

# === Dry Run ===
dry_run: false    # if true, log everything but place no real orders
```

### Secrets Handling

**Never commit API keys to git.** Config.yaml references environment variables via `${VAR_NAME}` syntax. Use `pydantic-settings` to resolve these at startup.

```bash
# .env (gitignored)
KRAKEN_API_KEY=your_key_here
KRAKEN_API_SECRET=your_secret_here
ANTHROPIC_API_KEY=your_key_here
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

```python
# config.py
from pydantic_settings import BaseSettings
import yaml, os, re

def resolve_env_vars(config: dict) -> dict:
    """Recursively resolve ${VAR} references in config values."""
    def _resolve(val):
        if isinstance(val, str):
            return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), val)
        if isinstance(val, dict):
            return {k: _resolve(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_resolve(v) for v in val]
        return val
    return _resolve(config)

def load_config(path="config.yaml") -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return resolve_env_vars(raw)
```

Add to `.gitignore`: `.env`, `config.yaml` (commit `config.yaml.template` instead), `*.mv2`, `data/`, `dashboard/index.html`.

### Dependencies (requirements.txt)

```
# requirements.txt — pin versions for reproducibility
ccxt==4.4.26
vectorbt==0.26.2
pandas-ta==0.3.14b1
pandas>=2.1.0,<3.0
numpy>=1.24.0,<2.0
apscheduler==3.10.4
sqlalchemy==2.0.36
anthropic>=0.40.0
python-telegram-bot==21.7
pydantic-settings==2.6.1
pyyaml>=6.0
sentence-transformers==3.3.1
memvid>=0.3.0
requests>=2.31.0
```

Pin major+minor versions. Update deliberately, not accidentally. Run `pip install -r requirements.txt --break-system-packages` in the deployment environment.

### Logging

Every component should use Python's `logging` module with structured output. Log files rotate daily and are kept for 30 days.

```python
# logging_config.py
import logging
from logging.handlers import TimedRotatingFileHandler
import json

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "agent_id"):
            log_data["agent_id"] = record.agent_id
        if hasattr(record, "cycle"):
            log_data["cycle"] = record.cycle
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)

def setup_logging(log_dir="/workspace/logs"):
    handler = TimedRotatingFileHandler(
        f"{log_dir}/system.log", when="midnight", backupCount=30
    )
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # Also log to console for development
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(console)
```

Component-level loggers: `logging.getLogger("data_collector")`, `logging.getLogger("executor")`, etc. Set per-component log levels in config.yaml if needed.

### Testing

The risk gate, circuit breaker, instruction queue, and execution path handle real money. They need tests.

```
/workspace/tests/
  test_risk_gate.py          # per-agent limits, global limits, conflict detection
  test_circuit_breaker.py    # drawdown calculation, trigger conditions, position closing
  test_instruction_queue.py  # enqueue, approve, reject, execute flow
  test_executor_paper.py     # minimum order enforcement, fill simulation
  test_wake_controller.py    # cadence clamping, trigger rate limiting, cooldown
  test_digest_builder.py     # section collapsing, per-agent scoping
  test_output_parser.py      # JSON parsing, malformed output handling
  test_robustness.py         # random entry test, return permutation test
  conftest.py                # shared fixtures: mock exchange, test database
```

Use `pytest`. Tests run against an in-memory SQLite database with fixture data. Mock the exchange via ccxt's sandbox or a simple fake. The critical tests:

- Risk gate rejects a signal that would exceed global exposure
- Risk gate rejects a signal that would exceed agent capital allocation
- Circuit breaker fires at exactly 30% drawdown from HWM
- Circuit breaker closes all positions across all agents
- Instruction queue correctly sequences: pending → risk check → approved → executed
- Paper executor rejects orders below MINIMUM_ORDER_USD
- Wake controller clamps cadence to [min, max] bounds
- Wake controller enforces trigger cooldown
- Output parser handles malformed JSON gracefully (returns None, logs, doesn't crash)

Run tests before Phase 6 (first live cycle) and after any change to risk-related code.

### Graceful Shutdown

The system runs multiple concurrent concerns (APScheduler, data collector polling, trigger watcher). Ctrl+C or SIGTERM must shut down cleanly.

```python
# main.py (addition)
import signal
import sys

shutdown_requested = False

def handle_shutdown(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        # Second signal = force exit
        logging.warning("Forced shutdown")
        sys.exit(1)
    shutdown_requested = True
    logging.info("Graceful shutdown requested. Finishing current cycle...")

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# In the main loop / scheduler:
# - Check shutdown_requested before starting new cycles
# - If a cycle is in progress, let it finish
# - Close exchange connections (ccxt cleanup)
# - Flush all pending database writes
# - Write final STATE.md
# - Log shutdown complete
```

The wake controller checks `shutdown_requested` before firing each cycle. The data collector checks it between polling intervals. No component starts new work after shutdown is requested.

### RUNBOOK.md Skeleton

The runbook is a Phase 7 deliverable. Minimum sections:

```markdown
# RUNBOOK.md

## First-Time Setup
- Environment setup (.env, config.yaml, dependencies)
- Historical data backfill
- Verify exchange connection
- Run test suite
- Start system in dry-run mode
- First live cycle

## Daily Operations
- Reading Telegram summaries
- Responding to owner requests
- Monitoring API spend

## Managing Agents
- Adding a new agent (write brief, add config, enable)
- Pausing / resuming agents
- Changing capital allocations
- Enabling multi-agent mode

## Troubleshooting
- System not waking on schedule
- Failed cycle recovery
- Database issues
- Exchange connectivity problems
- Agent producing malformed output

## Circuit Breaker
- What triggers it
- How to investigate
- How to clear (/resume)
- When NOT to clear immediately

## System Improvement Reviews
- Running a review cycle
- Approving / declining requests
- Implementing improvements with Claude Code

## Backup and Recovery
- Database backup (cron job recommendation)
- Restoring from backup
- Disaster recovery steps

## Stopping the System
- Graceful shutdown
- Emergency stop
- Resuming after shutdown
```

---

## Directory Structure

```
/workspace
  /briefs
    BRIEF_QUANT.md
    BRIEF_QUANT_MICRO.md    # future
    BRIEF_QUANT_BARBELL.md  # future
    BRIEF_PM.md             # future
    BRIEF_RISK.md           # future
  /data
    /cache                  # OHLCV (SQLite)
    /trades
    /digest_log             # per-agent: digest_042_quant_primary.txt
    /response_log           # per-agent: response_042_quant_primary.json
    /analysis               # output of analytical runs
  /strategies
    base.py
    robustness.py
    /active/{namespace}_*
    /paper/{namespace}_*
    /backtest/{namespace}_*
    /graveyard/{namespace}/
    /hypotheses/{namespace}/
  /benchmarks
  /memory
    quant_primary.mv2
  /executor
    live.py
    paper.py
  /wake_controller
    controller.py
    cadence.py
    triggers.py
  /data_collector
    collector.py
    backfill.py             # one-time historical data backfill
    analysis.py             # sync + async analysis engine
    /feeds                  # supplementary feed plugins
      base_feed.py
      fear_greed.py         # example: Fear & Greed Index (free, daily)
      polymarket.py         # example: prediction market probabilities + deltas
      # additional feeds added as plugins per agent request
  /digest
    builder.py
    formatter.py
  /claude_interface
    caller.py
    parser.py
    tools.py
  /risk
    limits.py
    portfolio.py
  /scripts
    generate_review_report.py
    mark_shipped.py
    review_improvements.sh
    backfill_historical.sh  # wrapper for data backfill
  /tests
    conftest.py             # shared fixtures: mock exchange, test DB
    test_risk_gate.py
    test_circuit_breaker.py
    test_instruction_queue.py
    test_executor_paper.py
    test_wake_controller.py
    test_digest_builder.py
    test_output_parser.py
    test_robustness.py
  /logs                     # rotating JSON logs (gitignored)
  /dashboard
    index.html
  main.py                   # entry point with graceful shutdown
  config.py                 # config loader with env var resolution
  logging_config.py         # structured logging setup
  config.yaml               # gitignored — created from template
  config.yaml.template      # committed — reference for setup
  requirements.txt          # pinned dependencies
  .env                      # gitignored — secrets
  .gitignore
  STATE.md
  BUILD.md
  RUNBOOK.md
```

---

## Build Order

Phases 1-6: working single-agent system. Phases 7-9: hardening, multi-agent, memory.

### Phase 1 — Foundation + Schema
1. Directory structure (including briefs/, tests/, logs/, feeds/, namespaced strategy dirs)
2. `requirements.txt` with pinned versions, `pip install`
3. `.gitignore` (.env, config.yaml, *.mv2, data/, logs/, dashboard/index.html)
4. `config.yaml.template` → owner copies to `config.yaml` and fills in secrets
5. `config.py` — config loader with `${ENV_VAR}` resolution via pydantic-settings
6. `.env` with placeholder secrets
7. `logging_config.py` — structured JSON logging with rotation
8. `risk/limits.py` (all hard limits including global exposure, circuit breaker drawdown %)
9. Exchange connector via ccxt, verify connection and balance read
10. SQLite schema: trades, ohlcv_cache, strategy_registry, research_notes, instruction_queue, events, agent_messages, owner_requests/responses, failed_cycles, system_state (high-water mark, circuit breaker), system_improvement_requests, supplementary_feeds, feed_registry — agent_id columns where applicable
11. Basic data collector (OHLCV + volatility score)
12. Historical data backfill script (`data_collector/backfill.py`) — run before Phase 6, pulls 180 days for all monitored pairs

### Phase 2 — Digest and Caller
13. Digest builder with per-agent scoping, empty-section collapsing, SYSTEM UPDATES section
14. Tool definitions (claude_interface/tools.py)
15. Agentic caller with tool use loop
16. Tool executor — wire run_analysis and query_memory (analysis can be stub)
17. Output parser + instruction dispatcher (handles agent_messages, research_notes, analysis_requests, requested_model, system_improvement_requests with de-duplication)
18. Error recovery: failed cycle logging, consecutive failure tracking, auto-pause
19. End-to-end dummy cycle: digest → agentic call → JSON parse → queue write

### Phase 3 — Execution + Risk Gate
20. strategies/base.py
21. Paper executor (minimum order enforcement, full logging)
22. Live executor (minimum order enforcement)
23. risk/portfolio.py — per-agent and global checks, conflict detection
24. Wire queue flow: parser → pending → risk gate → approved/rejected → executor
25. Benchmark tracker (four defaults)
26. Verify rejected instructions logged and surfaced
27. **Test suite (critical path):**
    - test_risk_gate.py (per-agent limits, global limits, conflict detection)
    - test_circuit_breaker.py (drawdown calc, trigger, position closing)
    - test_instruction_queue.py (enqueue → approve → execute flow)
    - test_executor_paper.py (minimum order enforcement, fill simulation)
    - test_output_parser.py (malformed JSON handling)
    - conftest.py (mock exchange, in-memory test DB)
    - **All tests must pass before proceeding to Phase 4**

### Phase 4 — Wake Controller
28. `main.py` — entry point with graceful shutdown signal handlers
29. Per-agent cadence + modifier evaluation
30. Triggers: built-in (position loss, connectivity, circuit breaker) + agent-defined + agent wake requests from message bus
31. Wire wake_schedule from output to controller
32. Verify hard limits (cadence bounds, trigger rate limit, cooldown)
33. APScheduler — one schedule entry per enabled agent, checks shutdown_requested
34. test_wake_controller.py (cadence clamping, trigger rate limiting, cooldown enforcement)

### Phase 5 — Strategy Lifecycle + Analysis + Robustness
35. Analysis engine (sync + async interfaces), including supplementary feed joins
36. Supplementary feed plugin framework (`data_collector/feeds/base_feed.py`)
37. Backtest runner (vectorbt)
38. strategies/robustness.py (random_entry_test + return_permutation_test)
39. Wire: passing backtest → auto robustness → results in digest
40. Strategy registry (namespaced by agent)
41. Graveyard archiver (namespaced)
42. Research note lifecycle (age tracking, expiry notification at cycle 8/10)
43. test_robustness.py, test_digest_builder.py

### Phase 6 — First Live Cycle
44. Run historical data backfill (`scripts/backfill_historical.sh`)
45. Run full test suite, confirm all pass
46. STATE.md generator
47. First real digest for primary quant agent
48. Call agent in quant mode (with tools)
49. Parse output — expect research notes, tool calls, data_requests. Not full hypotheses
50. Verify wake controller picks up schedule
51. Confirm scheduler running
52. Verify events table capturing all activity

### Phase 7 — Hardening + Observability + Improvement Pipeline
53. Telegram message types (agent-tagged, robustness results, improvement notifications)
54. Bot commands (/pause agent, /agents, /messages, /review, /improvements, /ship, /decline)
55. Owner request dispatch — immediate Telegram for blocking/high
56. Owner response flow — resolved requests in next digest
57. Dashboard with cross-agent views, supplementary data displays
58. API budget tracking (per-agent + total)
59. Dry-run mode flag
60. System improvement pipeline:
    - Parser writes `system_improvement_requests` to SQLite with de-duplication
    - Review report generator (`scripts/generate_review_report.py`)
    - Digest builder surfaces `SYSTEM UPDATES` section (shipped, pending, owner interventions)
    - `/review` Telegram command triggers immediate review cycle
    - Configure Claude Code scheduled task for weekly review
61. RUNBOOK.md (complete, using skeleton from Operational Foundation section)
62. Database backup cron job recommendation in RUNBOOK

### Phase 8 — Memory (~2 weeks in)
63. memory/encoder.py — per-agent memvid encoding
64. Initial .mv2 file for primary quant
65. memory/retriever.py — semantic search
66. Wire query_memory tool to retriever (sync, within-cycle)
67. Automatic retrieval → RELEVANT HISTORY in digest
68. Wire memory_query_hints from output

### Phase 9 — Multi-Agent Activation (when ready)
69. Write PM, Risk, specialized quant briefs
70. Enable agents in config, set capital allocations (verify sum <= 1.0)
71. Verify inter-agent messaging + wake triggers
72. Verify cross-agent risk gate (global exposure limits, conflict detection)
73. PM comparative digest
74. Risk monitor cadence tuning + lightweight digest
75. Per-agent .mv2 memory files

---

## API Billing

| Scenario | Est. Cost/Cycle | Monthly (6h) |
|----------|----------------|--------------|
| Single agent, no tools | $0.003-0.005 | $3-5 |
| Single agent, 3 tool calls | $0.008-0.015 | $8-15 |
| 3 agents, mixed cadences | $0.02-0.04 | $20-40 |
| + occasional Opus | +$0.05-0.10/call | +$5-10 |

Monthly $50 budget covers comfortable multi-agent operation.

---

*End of build specification. Begin Phase 1.*
