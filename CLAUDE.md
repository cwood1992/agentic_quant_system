# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Autonomous multi-agent crypto quantitative trading system. AI agents (powered by Claude) wake on scheduled cadences, receive structured digests of market/portfolio state, reason about strategies, and emit structured JSON instructions that flow through a risk gate before execution on Kraken.

**Current state:** Specification phase — BRIEF.md and BUILD.md define the complete system design. Implementation follows a 9-phase build plan.

## Key Specification Files

- **BUILD.md** — Full build specification: architecture, database schema, component design, build phases, configuration templates. This is the primary reference for implementation.
- **BRIEF.md** — Quant agent system prompt (injected as the system message on every wake cycle). Defines agent reasoning, tool use, strategy lifecycle, output format, and hard limits.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python |
| Database | SQLite |
| Exchange | Kraken via `ccxt` |
| AI | Claude API (`anthropic` SDK) — Sonnet default, Opus for escalation |
| Backtesting | `vectorbt` |
| Scheduling | `APScheduler` |
| Memory | `memvid` + `sentence-transformers` |
| Config | YAML + `pydantic-settings`, secrets in `.env` |
| Notifications | `python-telegram-bot` |
| Testing | `pytest` |

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
      Agent Caller (Claude API tool loop, max 5 tool calls/cycle)
            │
      Output Parser → Instruction Dispatcher
```

**Agent hierarchy:** Quant agents (strategy research + trading) → Portfolio Manager (capital allocation, conflict resolution) → Risk Monitor (fast-cadence exposure tracking). Single-agent mode first (Phase 1–6), multi-agent in Phase 9.

**Strategy lifecycle:** Hypothesis → Backtest → Robustness Testing → Paper Trading → Live. Forward-only promotion. Every strategy requires a counterfactual benchmark.

## Build Phases

| Phase | Focus |
|-------|-------|
| 1 | Foundation: config, logging, DB schema, exchange connector, OHLCV backfill |
| 2 | Digest builder, agent caller with tool loop, output parser |
| 3 | Paper + live executor, risk gate, benchmark tracker |
| 4 | Wake controller, cadence clamping, trigger rate limiting |
| 5 | Strategy lifecycle, backtest runner, robustness tester, analysis engine |
| 6 | First live cycle (requires full test suite passing) |
| 7 | Telegram bot, dashboard, API budget tracking, improvement pipeline |
| 8 | Memvid memory encoding/retrieval, semantic history in digest |
| 9 | Multi-agent: PM brief, risk brief, capital splits, message routing |

## Commands

```bash
# Run all tests (must pass before Phase 6)
pytest

# Run a single test
pytest tests/test_risk_gate.py

# Entry point
python main.py
```

## Project Structure (Target)

```
briefs/              # Agent system prompts (BRIEF_QUANT.md, BRIEF_PM.md, etc.)
strategies/          # base.py (abstract interface), robustness.py, active/, paper/, backtest/, graveyard/
data_collector/      # collector.py, backfill.py, analysis.py, feeds/
executor/            # live.py, paper.py
risk/                # limits.py, portfolio.py (risk gate)
wake_controller/     # controller.py, cadence.py, triggers.py
digest/              # builder.py, formatter.py
claude_interface/    # caller.py, parser.py, tools.py
memory/              # Per-agent .mv2 files
benchmarks/          # Benchmark tracking
tests/               # pytest suite with conftest.py fixtures
scripts/             # Utility scripts (backfill, review reports)
data/                # cache/, trades/, digest_log/, response_log/
logs/                # Rotating JSON logs (30-day retention)
dashboard/           # Per-cycle metrics visualization
```

## Critical Constraints

- **Circuit breaker:** 30% drawdown from high-water mark → close all positions, pause all agents
- **Global limits:** 80% gross exposure, 50% per-pair, 10 max positions
- **Per-agent limits:** 5 max positions (default), capital allocation ceiling
- **API budget:** $50/month for Claude API calls
- **Agent output:** 8,000 max tokens, 10 max tool calls per cycle
- **Wake cadence:** 1–24 hour range, 30-min cooldown between wakes, max 2 triggers per base window
- **Minimum order size:** $5 (Kraken minimum, enforced in both live and paper)
- **Robustness testing:** 1000 random-entry runs + 1000 return-permutation runs; backtest must have >8 trades

## Configuration

- `config.yaml` — Main config (YAML with env var resolution). Template in `config.yaml.template`.
- `.env` — Secrets (Kraken API keys, Claude API key, Telegram token). Gitignored.
- Per-agent config specifies: role, brief path, memory path, model routing, cadence, tools, capital allocation.
