# Agentic Quant System

Autonomous multi-agent crypto quantitative trading system powered by Claude.

## Overview

AI agents wake on scheduled cadences, receive structured digests of market and portfolio state, reason about strategies using Claude, and emit structured JSON instructions that flow through a risk gate before execution on Kraken. The system manages the full strategy lifecycle from hypothesis generation through backtesting, robustness testing, paper trading, and live execution.

## Architecture

```
Persistent Runtime (24/7)
  ├── Data Collector + Analysis Engine (OHLCV, supplementary feeds → SQLite)
  ├── Executor Engine (live via Kraken API / paper with simulated fills)
  ├── Benchmark Tracker (hodl_btc, hodl_eth, dca variants)
  ├── Robustness Tester (1000-run random entry + permutation tests)
  └── Instruction Queue + Risk Gate
            │
      Digest Builder (per-agent scoped data assembly)
            │
      Wake Controller (cadence scheduling + event triggers)
            │
      Agent Caller (Claude API tool loop, max 10 tool calls/cycle)
            │
      Output Parser → Instruction Dispatcher
```

**Agent hierarchy:** Quant agents (strategy research + trading) → Portfolio Manager (capital allocation, conflict resolution) → Risk Monitor (fast-cadence exposure tracking).

**Strategy lifecycle:** Hypothesis → Backtest → Robustness Testing → Paper Trading → Live. Forward-only promotion. Every strategy requires a counterfactual benchmark.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Database | SQLite |
| Exchange | Kraken via `ccxt` |
| AI | Claude API (`anthropic` SDK) — Sonnet default, Opus for escalation |
| Backtesting | `vectorbt` |
| Scheduling | `APScheduler` |
| Memory | `memvid` + `sentence-transformers` |
| Dashboard | FastAPI + uvicorn |
| Config | YAML + `pydantic-settings`, secrets in `.env` |
| Notifications | `python-telegram-bot` |
| Testing | `pytest` |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env        # fill in API keys
cp config.yaml.template config.yaml

# 3. Initialize database and backfill data
python -c "from database.schema import create_all_tables; create_all_tables('data/system.db')"
python data_collector/backfill.py --pairs all --days 180 --timeframes "1m,1h,4h,1d"

# 4. Start the system
python main.py
```

The dashboard is available at `http://localhost:8501` once the system starts.

See [RUNBOOK.md](RUNBOOK.md) for detailed setup and operational procedures.

## Dashboard

The web dashboard is served automatically on port 8501 and auto-refreshes every 60 seconds. Accessible from any machine on the network.

Sections:
- **Strategy Lifecycle Funnel** — visual count at each stage
- **Equity Curve** — portfolio value over time
- **Research Notes** — agent observations with status and age
- **Backtest & Robustness Results** — per-strategy performance metrics
- **Graveyard Analysis** — killed strategies with failure reasons
- **Recent Trades** — execution log with paper/live mode
- **Risk Gate Log** — instruction approval/rejection audit trail
- **Failed Cycles** — agent errors and recovery tracking
- **Supplementary Feeds** — Fear & Greed Index, prediction markets

Configure in `config.yaml`:
```yaml
dashboard:
  enabled: true
  host: "0.0.0.0"
  port: 8501
```

## Key Files

| File | Purpose |
|------|---------|
| [BUILD.md](BUILD.md) | Full build specification: architecture, schema, component design, build phases |
| [BRIEF.md](BRIEF.md) | Quant agent system prompt — reasoning, tools, output format, hard limits |
| [RUNBOOK.md](RUNBOOK.md) | Operational procedures: setup, monitoring, troubleshooting, recovery |
| [TASKS.md](TASKS.md) | Implementation checklist with acceptance criteria |
| [STATE.md](STATE.md) | Auto-generated system state snapshot |
| [CLAUDE.md](CLAUDE.md) | Guidance for Claude Code when working in this repo |

## Configuration

- **`config.yaml`** — Main config (agents, risk limits, data collection, dashboard). See `config.yaml.template`.
- **`.env`** — Secrets: Kraken API keys, Anthropic API key, Telegram token. See `.env.example`.

## Commands

```bash
python main.py              # Start the system
pytest                      # Run all tests
```

## Critical Constraints

- **Circuit breaker:** 30% drawdown from high-water mark → close all positions, pause all agents
- **Global limits:** 80% gross exposure, 50% per-pair, 10 max positions
- **Per-agent limits:** 5 max positions (default), capital allocation ceiling
- **API budget:** $50/month for Claude API calls
- **Agent output:** 16,000 max tokens, 10 max tool calls per cycle
- **Minimum order size:** $5 (Kraken minimum)
- **Robustness testing:** 1,000 random-entry runs + 1,000 return-permutation runs; backtest must have >8 trades
