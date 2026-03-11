# TASKS.md — Agentic Quant System Implementation Tasks

> Every subtask below is scoped to be completable in a single Claude Code session.
> Acceptance criteria (AC) are pass/fail. Test cases are specific scenarios.
> Dependencies are listed only where non-obvious (sequential task numbering within a phase implies natural ordering).

---

## Phase 1 — Foundation + Schema

### 1.1 — Directory Structure

- [x] Create the full workspace directory tree as specified in BUILD.md Directory Structure section
  - AC: All directories exist: `briefs/`, `data/cache/`, `data/trades/`, `data/digest_log/`, `data/response_log/`, `data/analysis/`, `strategies/active/`, `strategies/paper/`, `strategies/backtest/`, `strategies/graveyard/`, `strategies/hypotheses/`, `benchmarks/`, `memory/`, `executor/`, `wake_controller/`, `data_collector/feeds/`, `digest/`, `claude_interface/`, `risk/`, `scripts/`, `tests/`, `logs/`, `dashboard/`
- [x] Create `__init__.py` files in every Python package directory
  - AC: Every directory containing `.py` files has an `__init__.py`; `import risk`, `import executor`, etc. all resolve without ImportError
- [x] Create placeholder `BRIEF_QUANT.md` in `briefs/` copied from BRIEF.md content
  - AC: File exists at `briefs/BRIEF_QUANT.md` and contains the full quant agent brief

### 1.2 — requirements.txt with Pinned Versions

- [x] Create `requirements.txt` with exact versions from BUILD.md: ccxt==4.4.26, vectorbt==0.26.2, pandas-ta==0.3.14b1, pandas>=2.1.0,<3.0, numpy>=1.24.0,<2.0, apscheduler==3.10.4, sqlalchemy==2.0.36, anthropic>=0.40.0, python-telegram-bot==21.7, pydantic-settings==2.6.1, pyyaml>=6.0, sentence-transformers==3.3.1, memvid>=0.3.0, requests>=2.31.0
  - AC: `pip install -r requirements.txt` completes without errors in a clean virtualenv
- [x] Add `pytest>=7.0` and `pytest-asyncio` to dev dependencies section
  - AC: `pytest --version` succeeds after install

### 1.3 — .gitignore

- [x] Create `.gitignore` with entries: `.env`, `config.yaml`, `*.mv2`, `data/`, `logs/`, `dashboard/index.html`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `*.egg-info/`
  - AC: `git status` does not show any of these file patterns as untracked after they exist on disk

### 1.4 — config.yaml.template

- [x] Create `config.yaml.template` with the full configuration schema from BUILD.md including exchange, claude, telegram, agents (quant_primary enabled, others commented), data, system_improvements, and dry_run sections
  - AC: File is valid YAML; loading it with `yaml.safe_load()` produces a dict with top-level keys: `exchange`, `claude`, `telegram`, `agents`, `data`, `system_improvements`, `dry_run`
  - AC: All secret values use `${ENV_VAR}` syntax, never raw credentials

### 1.5 — config.py — Config Loader with Env Var Resolution

- [x] Implement `resolve_env_vars(config: dict) -> dict` that recursively substitutes `${VAR_NAME}` patterns with `os.environ` values
  - AC: Given `{"key": "${FOO}"}` and `os.environ["FOO"] = "bar"`, returns `{"key": "bar"}`
  - Test: Nested dict `{"a": {"b": "${X}"}}` with `X=1` resolves to `{"a": {"b": "1"}}`
  - Test: Unset variable `${MISSING}` is left as literal `${MISSING}` (not crash)
  - Test: List values `["${A}", "plain"]` resolve correctly
- [x] Implement `load_config(path="config.yaml") -> dict` that reads YAML and runs env var resolution
  - AC: Returns fully resolved config dict
  - Test: Loading the template file with all env vars set produces no `${` patterns in output
- [x] Add startup validation: check that all enabled agent `capital_allocation_pct` values sum to <= 1.0
  - AC: Raises `ValueError` with descriptive message if sum > 1.0
  - Test: Two agents at 0.6 each raises; two agents at 0.5 each passes; single agent at 1.0 passes

### 1.6 — .env Placeholder

- [x] Create `.env` with placeholder values for `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - AC: File exists, contains all five variables with `your_*_here` placeholder values
  - AC: File is listed in `.gitignore`

### 1.7 — logging_config.py — Structured JSON Logging

- [x] Implement `JSONFormatter` class that outputs JSON with keys: `timestamp`, `level`, `component`, `message`, and optional `agent_id`, `cycle`, `exception`
  - AC: Formatting a log record produces valid JSON parseable by `json.loads()`
  - Test: Record with `exc_info` includes `exception` key with traceback string
  - Test: Record with extra `agent_id="quant_primary"` includes that field
- [x] Implement `setup_logging(log_dir)` that configures `TimedRotatingFileHandler` (midnight rotation, 30-day retention) plus console handler
  - AC: After calling `setup_logging`, `logging.getLogger("test").info("hello")` writes to both console and log file
  - AC: Log file path is `{log_dir}/system.log`
- [x] Add component-level logger factory: `get_logger(component: str, agent_id: str = None) -> logging.Logger`
  - AC: Returns a logger named by component with optional `agent_id` extra field attached

### 1.8 — risk/limits.py — All Hard Limits

- [x] Define all constants from BUILD.md Risk Limits section as module-level variables
  - AC: All 16 constants present: `MINIMUM_WAKE_CADENCE_HOURS=1`, `MAXIMUM_WAKE_CADENCE_HOURS=24`, `MAX_TRIGGER_FIRES_PER_BASE_WINDOW=2`, `TRIGGER_COOLDOWN_MINUTES=30`, `CIRCUIT_BREAKER_DRAWDOWN_PCT=0.30`, `POSITION_LOSS_TRIGGER_PCT=0.25`, `GLOBAL_MAX_GROSS_EXPOSURE=0.80`, `GLOBAL_MAX_PAIR_EXPOSURE=0.50`, `GLOBAL_MAX_CONCURRENT_POSITIONS=10`, `DEFAULT_MAX_POSITIONS_PER_AGENT=5`, `DEFAULT_MODEL="claude-sonnet-4-6"`, `TRIGGER_MODEL="claude-opus-4-6"`, `MAX_MONTHLY_API_BUDGET_USD=50`, `MAX_OUTPUT_TOKENS=8000`, `AUTO_APPROVE_DATA_FEED_MONTHLY_USD=10`, `MINIMUM_ORDER_USD=5.0`, `ROBUSTNESS_N_RUNS=1000`, `ROBUSTNESS_RANDOM_SEED=42`
  - Test: Import module and verify `CIRCUIT_BREAKER_DRAWDOWN_PCT == 0.30` and `GLOBAL_MAX_GROSS_EXPOSURE == 0.80`

### 1.9 — Exchange Connector via ccxt

- [x] Implement `exchange/connector.py` with `create_exchange(config: dict) -> ccxt.Exchange` that initializes Kraken with API credentials from config
  - AC: Returns a ccxt.kraken instance with `apiKey` and `secret` set
  - AC: If `config["exchange"]["sandbox"]` is True, sandbox mode is enabled
- [x] Implement `verify_connection(exchange) -> dict` that calls `fetch_balance()` and returns `{"connected": True, "total_usd": float}` or `{"connected": False, "error": str}`
  - AC: Returns connected=True with valid credentials; returns connected=False with descriptive error for invalid credentials
  - Test: Mock ccxt exchange, verify `verify_connection` returns expected structure
- [x] Implement `fetch_ticker(exchange, pair: str) -> dict` wrapper with retry logic (3 attempts, exponential backoff)
  - AC: Retries on `ccxt.NetworkError`; raises after 3 failures
  - Test: Mock exchange that fails twice then succeeds; verify 3 calls made and result returned

### 1.10 — SQLite Schema — All Tables

- [x] Create `database/schema.py` with `create_all_tables(db_path: str)` that creates the `trades` table
  - AC: Table has columns: id, timestamp, agent_id, strategy_id, pair, action, size_usd, price, order_type, fill_price, fill_timestamp, fees, pnl, paper (boolean), rationale, status
- [x] Create `ohlcv_cache` table
  - AC: Columns: id, pair, timeframe, timestamp, open, high, low, close, volume; unique constraint on (pair, timeframe, timestamp)
- [x] Create `strategy_registry` table
  - AC: Columns: id, strategy_id, agent_id, namespace, hypothesis_id, stage (hypothesis/backtest/robustness/paper/live/graveyard), created_at, updated_at, config (JSON), backtest_results (JSON), robustness_results (JSON), paper_results (JSON)
- [x] Create `research_notes` table
  - AC: Columns: id, note_id, agent_id, cycle, created_at, observation, potential_edge, questions, requested_data (JSON), status, age_cycles
- [x] Create `instruction_queue` table matching BUILD.md schema exactly
  - AC: Columns match: id, created_at, cycle, agent_id, strategy_namespace, instruction_type, payload (JSON), status, risk_check_result (JSON), executed_at, execution_result (JSON)
- [x] Create `events` table matching BUILD.md schema
  - AC: Columns: id, timestamp, event_type, agent_id, cycle, source, payload (JSON)
- [x] Create `agent_messages` table matching BUILD.md schema
  - AC: Columns: id, created_at, from_agent, to_agent, message_type, priority, payload (JSON), read_by_cycle, expires_at, status
- [x] Create `owner_requests` table
  - AC: Columns: id, request_id, agent_id, cycle, created_at, type, urgency, title, description, blocked_work (JSON), suggested_action, resolution_method, status, resolved_at, resolution_note
- [x] Create `failed_cycles` table
  - AC: Columns: id, agent_id, cycle, timestamp, raw_output, error, wake_reason, model_used
- [x] Create `system_state` table for high-water mark and circuit breaker state
  - AC: Columns: id, key, value (JSON), updated_at; seeds initial rows for `high_water_mark` and `circuit_breaker_status`
- [x] Create `system_improvement_requests` table matching BUILD.md schema exactly
  - AC: All columns present: id, request_id (UNIQUE), created_at, agent_id, cycle, title, problem, impact, category, priority, examples (JSON), status, status_note, reviewed_at, shipped_at, review_cycle
- [x] Create `supplementary_feeds` table and `feed_registry` table matching BUILD.md schemas
  - AC: `supplementary_feeds` has: id, feed_name, timestamp, value, metadata (JSON), source, resolution; index on (feed_name, timestamp)
  - AC: `feed_registry` has: feed_name (PK), feed_type, source, resolution, status, requested_by, activated_at, last_fetch, error_count, config (JSON)
- [x] Implement `get_db(db_path: str) -> sqlite3.Connection` connection factory with WAL mode enabled
  - AC: Connection uses WAL journal mode; `PRAGMA journal_mode` returns `wal`
  - Test: Create all tables in in-memory DB, verify all tables exist via `SELECT name FROM sqlite_master WHERE type='table'`

### 1.11 — Basic Data Collector (OHLCV + Volatility Score)

- [x] Implement `data_collector/collector.py` with `OHLCVCollector` class that polls Kraken for configured pairs and timeframes
  - AC: `collect_once(pairs, timeframes)` fetches OHLCV data and inserts into `ohlcv_cache` table
  - AC: Handles `ccxt.RateLimitExceeded` by sleeping and retrying
  - Test: Mock exchange returns known candle data; verify correct rows inserted into DB
- [x] Implement upsert logic for OHLCV data (update existing, insert new based on unique constraint)
  - AC: Running `collect_once` twice with overlapping data does not create duplicates
  - Test: Insert 10 candles, re-run with 5 overlapping + 5 new, verify 15 total rows
- [x] Implement `compute_volatility_score(pair: str, lookback_hours: int = 24) -> float` that returns 0-100 score based on recent price volatility
  - AC: Returns float between 0 and 100
  - AC: Higher realized volatility produces higher score
  - Test: Feed synthetic data with known std dev, verify score ordering (low vol < high vol)
- [x] Implement polling loop with configurable interval from `config.data.collection_interval_seconds`
  - AC: Collector runs on schedule and logs each collection cycle
  - AC: Respects `shutdown_requested` flag between polls

### 1.12 — Historical Data Backfill Script

- [x] Implement `data_collector/backfill.py` with `backfill(pairs, days, timeframes)` function
  - AC: Pulls up to 180 days of historical OHLCV for each pair/timeframe combination from Kraken
  - AC: Respects Kraken API rate limits (sleeps between requests)
  - AC: Populates `ohlcv_cache` table using same upsert logic as collector
- [x] Implement coverage gap detection and reporting
  - AC: After backfill, prints per-pair/timeframe coverage summary: earliest timestamp, latest timestamp, total candles, any gaps
  - Test: Backfill with mock exchange that has a gap in data; verify gap is reported
- [x] Create `scripts/backfill_historical.sh` wrapper script
  - AC: Script calls `python data_collector/backfill.py --pairs all --days 180 --timeframes 1m,1h,4h,1d`
  - AC: Script exits with non-zero code if backfill fails

### Phase 1 Gate

- [x] **Verification checkpoint**: All 12 tasks complete. Run `python -c "from database.schema import create_all_tables; create_all_tables(':memory:')"` succeeds. Config loads from template with env vars. Exchange connector initializes (mock mode). Data collector runs one collection cycle against mock exchange. Logging produces valid JSON.

---

## Phase 2 — Digest and Caller

### 2.13 — Digest Builder with Per-Agent Scoping

- [x] Implement `digest/builder.py` with `DigestBuilder` class that takes `agent_id`, `agent_config`, and DB connection
  - AC: Constructor accepts agent_id, role, and db connection
- [x] Implement `build_portfolio_section(agent_id)` — shows only the agent's own positions and allocation
  - AC: Quant agent sees only own positions; PM sees all positions
  - Test: Two agents with different positions; quant_primary digest contains only its own
- [x] Implement `build_benchmark_section()` — renders all benchmarks with 24h/7d/30d performance
  - AC: Output includes all four default benchmarks
  - AC: Shows which agent strategies beat which benchmarks
- [x] Implement `build_strategy_sections(agent_id, namespace)` — live, paper, backtest queue, hypothesis queue sections
  - AC: Strategies filtered by agent's namespace
  - Test: Strategies from two namespaces in DB; digest for "primary" shows only "primary_*" strategies
- [x] Implement `build_market_conditions()` — all monitored pairs with trend, volatility score, supplementary feeds
  - AC: Includes volatility score 0-100 for each pair
  - AC: Includes latest supplementary feed values with source attribution and freshness timestamp
- [x] Implement `build_agent_messages_section(agent_id)` — unread messages for this agent
  - AC: Shows messages where `to_agent` matches agent_id or is "all"
  - AC: Shows sender, timestamp, priority, type, content
  - Test: Insert messages to "quant_primary" and to "all"; both appear in quant_primary digest
- [x] Implement `build_system_updates_section(agent_id)` — owner interventions, shipped improvements, new feeds, pending requests
  - AC: Surfaces events since agent's last cycle from events table
  - AC: Includes system_improvement_requests with status for this agent
- [x] Implement empty-section collapsing: sections with no data render as single line "--- SECTION_NAME --- (empty)"
  - AC: Section with no data produces exactly one line, not a multi-line block
  - Test: Build digest with no trades; RECENT TRADES section is single collapsed line
- [x] Implement `build_full_digest(agent_id) -> str` that assembles all sections with the header format from BRIEF.md
  - AC: Header includes agent_id, cycle number, timestamp, capital allocated, wake reason
  - AC: All sections present in correct order matching BRIEF.md digest format

### 2.14 — Tool Definitions (claude_interface/tools.py)

- [x] Define `COMMON_TOOLS` list with `run_analysis` and `query_memory` tool schemas matching BUILD.md exactly
  - AC: Each tool has `name`, `description`, `input_schema` with correct `properties` and `required` fields
- [x] Define `QUANT_TOOLS = COMMON_TOOLS + [check_backtest_status]`
  - AC: `check_backtest_status` schema has `hypothesis_id` as required string property
- [x] Define `RISK_TOOLS = COMMON_TOOLS + [check_positions, check_exposure]`
  - AC: Both tools have empty properties schemas (no required inputs)
- [x] Define `PM_TOOLS = COMMON_TOOLS + [list_agent_messages, check_positions, check_exposure]`
  - AC: `list_agent_messages` has optional `agent_id` (string) and `since_hours` (integer, default 48)
- [x] Define `AGENT_TOOLS` mapping: `{"quant": QUANT_TOOLS, "risk_monitor": RISK_TOOLS, "portfolio_manager": PM_TOOLS}`
  - AC: Lookup by role returns correct tool list

### 2.15 — Agentic Caller with Tool Use Loop

- [x] Implement `claude_interface/caller.py` with `call_agent(agent_id, agent_config, digest, wake_reason, prior_response=None) -> dict|None`
  - AC: Reads brief from `agent_config["brief"]` path
  - AC: Uses brief as system prompt with `cache_control: {"type": "ephemeral"}`
  - AC: Passes digest as user message
  - AC: Returns parsed JSON dict on success, None on failure
- [x] Implement tool use loop: up to `MAX_TOOL_ITERATIONS=5` rounds of tool calls
  - AC: On `stop_reason == "tool_use"`, executes tool calls and appends results to messages
  - AC: On `stop_reason == "end_turn"`, returns parsed final response
  - AC: Loop exits after 5 tool iterations even if agent keeps requesting tools
  - Test: Mock Anthropic client that returns 3 tool_use rounds then end_turn; verify all 3 tool results provided
- [x] Implement `select_model(wake_reason, prior_response, agent_config) -> str` per BUILD.md logic
  - AC: Trigger wake reasons return escalation_model (opus)
  - AC: Agent's `requested_model` from prior response is honored if valid
  - AC: Default returns `agent_config["default_model"]`
  - Test: wake_reason="trigger:position_loss" returns opus; wake_reason="scheduled" returns sonnet; prior_response with requested_model="claude-opus-4-6" returns opus
- [x] Implement error handling: catch API exceptions, log failed cycle, return None
  - AC: `anthropic.APIError` is caught, logged to `failed_cycles` table, Telegram alert sent
  - AC: Never raises unhandled exception to caller

### 2.16 — Tool Executor

- [x] Implement `claude_interface/tool_executor.py` with `execute_tool_calls(response, agent_id) -> list[dict]` that dispatches to handler functions
  - AC: Returns list of tool_result content blocks matching Anthropic API format
  - AC: Unknown tool names return error result without crashing
- [x] Implement `handle_run_analysis(params, agent_id) -> str` that calls the analysis engine
  - AC: Returns JSON string with analysis results
  - AC: Times out after 60 seconds (TOOL_TIMEOUT_SECONDS)
  - AC: Stub implementation returns placeholder results with structure matching expected output
- [x] Implement `handle_query_memory(params, agent_id) -> str` stub
  - AC: Returns JSON string with empty results list (real implementation in Phase 8)
  - AC: Response format: `{"results": [], "note": "Memory system not yet active"}`
- [x] Implement `handle_check_backtest_status(params, agent_id) -> str`
  - AC: Looks up strategy_registry by hypothesis_id, returns current stage and any results
  - Test: Insert a strategy in "robustness" stage; tool returns stage and robustness results JSON

### 2.17 — Output Parser + Instruction Dispatcher

- [x] Implement `claude_interface/parser.py` with `parse_agent_output(raw_text: str, agent_id: str) -> dict|None`
  - AC: Returns parsed dict if valid JSON; returns None if unparseable
  - AC: Logs raw output to `data/response_log/response_{cycle}_{agent_id}.json` regardless of parse success
  - Test: Valid JSON string returns dict; `"not json at all"` returns None and logs to failed_cycles
  - Test: JSON with markdown fencing (```json ... ```) is stripped and parsed successfully
- [x] Implement `dispatch_instructions(parsed_output: dict, agent_id: str, cycle: int)` that routes all output fields
  - AC: `strategy_actions` items inserted into `instruction_queue` with type='strategy_action'
  - AC: `new_hypotheses` items create strategy_registry entries at stage='hypothesis' and write code modules to `strategies/backtest/{namespace}_{id}.py`
  - AC: `research_notes` items insert/update `research_notes` table
  - AC: `analysis_requests` items logged to events table for async processing
  - AC: `data_requests` items trigger feed check logic (exists? needs API key? needs budget?)
  - AC: `benchmark_actions` items inserted into instruction_queue with type='benchmark_action'
  - AC: `owner_requests` items inserted into owner_requests table
  - AC: `wake_schedule` updates the agent's cadence in wake controller
  - AC: `requested_model` stored for next cycle's model selection
- [x] Implement agent message routing from `agent_messages` field
  - AC: Each message inserted into `agent_messages` table with correct from_agent, to_agent, type, priority, payload
  - AC: Messages with `priority: "wake"` trigger a wake flag for the recipient
  - Test: Output with `agent_messages: [{"to_agent": "portfolio_manager", "priority": "wake", ...}]` creates a DB row and sets wake flag
- [x] Implement system_improvement_request handling with de-duplication
  - AC: New requests inserted into `system_improvement_requests` table
  - AC: If request title is similar (>80% fuzzy match) to existing pending request, merge: update impact field, upgrade priority if higher
  - AC: Per-agent budget of 3 requests per cycle enforced; excess logged but held
  - Test: Submit request "Rolling beta computation", then submit "Rolling beta calculation" same cycle; only one row exists with merged impact

### 2.18 — Error Recovery: Failed Cycle Logging

- [x] Implement `log_failed_cycle(agent_id, raw_output, error, wake_reason, model)` that inserts into `failed_cycles` table
  - AC: Row created with all fields populated including timestamp
- [x] Implement `check_consecutive_failures(agent_id) -> int` that counts consecutive failed cycles
  - AC: Returns count of most recent unbroken streak of failures
  - Test: 2 successes then 3 failures returns 3; 3 failures then 1 success then 2 failures returns 2
- [x] Implement auto-pause logic: if `check_consecutive_failures(agent_id) >= 3`, pause agent and send Telegram alert
  - AC: Agent status set to "paused" in system_state
  - AC: Telegram message sent with agent_id and failure count
  - AC: Paused agent is skipped by wake controller

### 2.19 — End-to-End Dummy Cycle

- [x] Wire complete cycle flow: build digest -> call agent (mock) -> parse output -> dispatch instructions -> verify queue entries
  - AC: Starting from empty DB, a single cycle produces: digest string, API call (mocked), parsed response, instruction_queue entries, events logged
  - Test: Mock Anthropic client returns a valid quant output JSON with strategy_actions, research_notes, and wake_schedule; verify each dispatched to correct table
- [x] Verify digest is logged to `data/digest_log/digest_{cycle}_{agent_id}.txt`
  - AC: File created with full digest content
- [x] Verify response is logged to `data/response_log/response_{cycle}_{agent_id}.json`
  - AC: File created with raw response JSON

### Phase 2 Gate

- [x] **Verification checkpoint**: Complete cycle executes end-to-end with mock Anthropic API. Digest contains all sections in correct format. Output parser handles valid JSON, malformed JSON, and JSON with markdown fencing. Instruction dispatcher routes all output fields to correct tables. Failed cycle logging and auto-pause work. Agent messages routed correctly.

---

## Phase 3 — Execution + Risk Gate

### 3.20 — strategies/base.py

- [x] Implement `Signal` dataclass with fields: `action` (str), `pair` (str), `size_pct` (float), `order_type` (str), `limit_price` (Optional[float], default None), `rationale` (str, default "")
  - AC: `Signal(action="buy", pair="BTC/USD", size_pct=0.1, order_type="market")` creates valid instance
  - AC: `action` values are validated against `{"buy", "sell", "close", "hold"}`
- [x] Implement `BaseStrategy` abstract class with abstract methods `name() -> str`, `required_feeds() -> list[str]`, `on_data(data) -> list[Signal]` and optional methods `on_fill(fill)`, `on_cycle(cycle_number, portfolio_state) -> dict`
  - AC: Cannot instantiate BaseStrategy directly (raises TypeError)
  - AC: Concrete subclass implementing all abstract methods can be instantiated
  - Test: Create `TestStrategy(BaseStrategy)` implementing all abstracts; verify it instantiates and `on_data({})` returns a list

### 3.21 — Paper Executor

- [x] Implement `executor/paper.py` with `PaperExecutor` class
  - AC: Constructor accepts DB connection and config
- [x] Implement `execute_signal(signal: Signal, agent_id: str, strategy_id: str, agent_capital: float) -> dict`
  - AC: Computes order size in USD from `signal.size_pct * agent_capital`
  - AC: Rejects orders where computed USD size < `MINIMUM_ORDER_USD` ($5); returns `{"status": "rejected", "reason": "below_minimum_order"}`
  - Test: Signal with size_pct=0.005 on $500 capital = $2.50, rejected
  - Test: Signal with size_pct=0.02 on $500 capital = $10.00, accepted
- [x] Implement simulated fill logic: use current market price with configurable slippage (default 0.1%)
  - AC: Fill price for buy = market_price * (1 + slippage); for sell = market_price * (1 - slippage)
  - AC: Fill logged to `trades` table with `paper=True`
  - Test: Market price 50000, buy order, 0.1% slippage -> fill price 50050
- [x] Implement position tracking for paper portfolio: maintain open positions, calculate unrealized PnL
  - AC: `get_positions(agent_id) -> list[dict]` returns current open positions with entry price, current price, unrealized PnL
  - AC: "close" signal closes existing position and computes realized PnL
  - Test: Buy BTC at 50000, price moves to 51000 -> unrealized PnL = +2% * position_size

### 3.22 — Live Executor

- [x] Implement `executor/live.py` with `LiveExecutor` class wrapping ccxt Kraken
  - AC: Constructor accepts exchange instance and DB connection
- [x] Implement `execute_signal(signal, agent_id, strategy_id, agent_capital) -> dict`
  - AC: Rejects orders below `MINIMUM_ORDER_USD`
  - AC: Places real order via `exchange.create_order()`
  - AC: Logs to `trades` table with `paper=False`
  - AC: Returns fill details including actual fill price, fees, order ID
- [x] Implement order status polling for limit orders
  - AC: Polls order status until filled, partially filled, or expired
  - AC: Timeout after configurable period (default 5 minutes for limit orders)
- [x] Implement dry_run mode: log order details but do not call exchange
  - AC: When `config["dry_run"] == True`, all orders are logged but `exchange.create_order()` is never called
  - Test: In dry_run mode, execute_signal returns `{"status": "dry_run", "would_have_placed": {...}}`

### 3.23 — risk/portfolio.py — Per-Agent and Global Checks

- [x] Implement `check_agent_limits(signal, agent_id, agent_positions, agent_capital, agent_config) -> tuple[bool, str]`
  - AC: Returns `(False, "Would exceed agent capital allocation")` if position value after signal exceeds agent_capital
  - AC: Returns `(False, "Agent at max concurrent positions")` if agent has >= max_positions and signal is a buy
  - AC: Returns `(True, "passed")` otherwise
  - Test: Agent with 5 positions (max=5) submits buy -> rejected
  - Test: Agent with $500 capital, existing $400 exposure, submits buy for 30% -> rejected (would be $550)
  - Test: Agent with $500 capital, $200 exposure, submits buy for 20% -> passed
- [x] Implement `check_global_limits(signal, agent_id, all_positions, portfolio_state) -> tuple[bool, str]`
  - AC: Rejects if gross exposure after signal would exceed 80% of total equity
  - AC: Rejects if per-pair exposure after signal would exceed 50% of total equity
  - AC: Detects cross-agent conflicts (same pair, opposing direction) and logs them without blocking
  - Test: Total equity $1000, current gross $750, new signal for $100 -> rejected (would be 85%)
  - Test: Total equity $1000, BTC exposure $450, new BTC buy for $100 -> rejected (would be 55%)
  - Test: Agent A long BTC, Agent B submits short BTC -> approved but conflict logged and PM alerted
- [x] Implement `check_and_approve(instruction_id, db) -> str` main entry point
  - AC: Runs agent limits then global limits in sequence
  - AC: Updates instruction_queue status to "approved" or "rejected" with reason
  - AC: Rejected instructions create an event in events table
  - Test: Full flow: insert instruction -> check_and_approve -> verify status updated correctly
- [x] Implement circuit breaker check: `check_circuit_breaker(portfolio_state) -> tuple[bool, str]`
  - AC: Returns `(True, "circuit_breaker_active")` if portfolio equity is >= 30% below high-water mark
  - AC: When triggered: updates system_state to `circuit_breaker_status: "triggered"`, closes all positions across all agents, pauses all agents
  - Test: HWM $1000, current equity $690 -> NOT triggered (31% remaining = 69% = 31% drawdown > 30% threshold... wait: 30% drawdown means equity = $700. $690 < $700 -> triggered)
  - Test: HWM $1000, current equity $710 -> NOT triggered (29% drawdown)
  - Test: HWM $1000, current equity $700 -> triggered (exactly 30%)

### 3.24 — Wire Queue Flow

- [x] Implement `queue/processor.py` with `process_pending_instructions(db)` that processes all pending instructions
  - AC: Flow: pending -> risk gate check -> approved/rejected -> if approved, route to correct executor (paper or live based on strategy stage)
  - AC: Updates instruction status at each step
  - AC: Execution results stored in `execution_result` column as JSON
  - Test: Insert 3 instructions (1 should pass risk, 1 should fail agent limits, 1 should fail global limits); after processing, verify correct statuses
- [x] Implement signal extraction from strategy_action payloads
  - AC: Parses the payload JSON to extract Signal parameters
  - AC: Handles all action types: promote, demote, kill, modify, hold
  - AC: "promote" advances strategy stage in registry; "kill" moves to graveyard

### 3.25 — Benchmark Tracker

- [x] Implement `benchmarks/tracker.py` with `BenchmarkTracker` class
  - AC: Constructor seeds four default benchmarks at $500: `hodl_btc`, `hodl_eth`, `dca_btc`, `equal_weight_rebal`
- [x] Implement `hodl_btc` and `hodl_eth`: calculate current value based on price change from initial purchase
  - AC: Initial BTC price at seed time recorded; current value = $500 * (current_price / initial_price)
  - Test: Initial BTC $50000, current $55000 -> value = $550
- [x] Implement `dca_btc`: simulate weekly $500/(total_weeks) purchases
  - AC: Tracks weekly buy amounts and average cost basis
  - AC: Current value reflects all accumulated BTC at current price
- [x] Implement `equal_weight_rebal`: 50/50 BTC/ETH rebalanced weekly
  - AC: Tracks rebalancing events and current allocation
  - Test: After one week with BTC +10%, ETH -5%, rebalance brings both back to 50/50
- [x] Implement `get_benchmark_performance(benchmark_id, period) -> dict` returning 24h, 7d, 30d returns
  - AC: Returns dict with keys `return_24h`, `return_7d`, `return_30d`, `current_value`, `initial_value`
- [x] Implement agent-defined benchmark management: add, remove, modify via instruction queue
  - AC: `benchmark_action` instructions processed correctly

### 3.26 — Verify Rejected Instructions Surface in Digest

- [x] Add `build_risk_gate_log_section(agent_id)` to digest builder
  - AC: Shows all rejected instructions for this agent since last cycle with rejection reasons
  - Test: Insert 2 rejected instructions for agent; digest section lists both with reasons
- [x] Log rejection events to events table
  - AC: Each rejection creates an event with type="risk_gate_rejection", payload containing signal details and reason

### 3.27 — Test Suite (Critical Path)

- [x] Implement `tests/conftest.py` with shared fixtures
  - AC: `db` fixture provides in-memory SQLite with all tables created
  - AC: `mock_exchange` fixture provides a fake ccxt exchange with configurable responses
  - AC: `sample_config` fixture provides a complete config dict for testing
  - AC: `sample_positions` fixture provides realistic position data for risk gate testing
- [x] Implement `tests/test_risk_gate.py`
  - Test: `test_agent_limits_rejects_exceeding_capital` — signal that would push exposure over agent capital
  - Test: `test_agent_limits_rejects_max_positions` — buy signal when agent at max positions
  - Test: `test_agent_limits_allows_close_at_max_positions` — close signal allowed even at max positions
  - Test: `test_global_gross_exposure_rejection` — gross exposure would exceed 80%
  - Test: `test_global_pair_exposure_rejection` — pair exposure would exceed 50%
  - Test: `test_cross_agent_conflict_detection` — opposing positions logged, PM alerted, signal not blocked
  - Test: `test_approved_signal_passes_all_checks` — valid signal approved
- [x] Implement `tests/test_circuit_breaker.py`
  - Test: `test_triggers_at_30pct_drawdown` — equity exactly 30% below HWM triggers breaker
  - Test: `test_does_not_trigger_below_threshold` — 29% drawdown does not trigger
  - Test: `test_closes_all_positions_all_agents` — when triggered, all open positions across all agents are closed
  - Test: `test_pauses_all_agents` — all agents set to paused status
  - Test: `test_hwm_tracking` — HWM updates when equity reaches new high
  - Test: `test_hwm_resets_on_resume` — owner /resume resets HWM to current equity
- [x] Implement `tests/test_instruction_queue.py`
  - Test: `test_pending_to_approved_flow` — instruction goes pending -> risk check -> approved
  - Test: `test_pending_to_rejected_flow` — instruction goes pending -> risk check -> rejected
  - Test: `test_approved_to_executed_flow` — approved instruction routes to executor
  - Test: `test_failed_execution_status` — executor error updates status to "failed"
  - Test: `test_queue_ordering` — instructions processed in FIFO order
- [x] Implement `tests/test_executor_paper.py`
  - Test: `test_rejects_below_minimum_order` — $4 order rejected, $5 order accepted
  - Test: `test_slippage_applied_correctly` — buy fills above market, sell fills below
  - Test: `test_position_tracking` — buy creates position, close realizes PnL
  - Test: `test_trade_logged_to_db` — every execution creates a trades table row
  - Test: `test_paper_flag_set` — all trades marked paper=True
- [x] Implement `tests/test_output_parser.py`
  - Test: `test_valid_json_parsed` — returns dict
  - Test: `test_malformed_json_returns_none` — returns None, logs to failed_cycles
  - Test: `test_markdown_fenced_json_parsed` — strips fencing, returns dict
  - Test: `test_empty_string_returns_none`
  - Test: `test_partial_json_returns_none` — truncated JSON handled gracefully
  - Test: `test_response_always_logged` — raw output saved to response_log regardless of parse success

### Phase 3 Gate

- [x] **Verification checkpoint**: `pytest tests/` passes all tests. Risk gate correctly rejects signals exceeding agent capital, global gross exposure (80%), and global per-pair exposure (50%). Circuit breaker fires at exactly 30% drawdown. Paper executor enforces $5 minimum. Instruction queue flows correctly through all states. Output parser handles all edge cases.

---

## Phase 4 — Wake Controller

### 4.28 — main.py Entry Point with Graceful Shutdown

- [x] Implement `main.py` with `shutdown_requested` flag and signal handlers for SIGINT and SIGTERM
  - AC: First SIGINT sets `shutdown_requested = True` and logs "Graceful shutdown requested"
  - AC: Second SIGINT forces `sys.exit(1)`
- [x] Implement main startup sequence: load config, setup logging, create DB, verify exchange connection, start data collector, start wake controller
  - AC: Startup fails fast with clear error if config invalid, DB creation fails, or exchange unreachable
  - AC: Logs each startup step
- [x] Implement main shutdown sequence: stop accepting new cycles, wait for current cycle to finish, close exchange connections, flush DB writes, write final STATE.md, log "shutdown complete"
  - AC: No data loss on graceful shutdown
  - Test: Start system, trigger shutdown, verify STATE.md written and logs contain "shutdown complete"

### 4.29 — Per-Agent Cadence + Modifier Evaluation

- [x] Implement `wake_controller/cadence.py` with `compute_effective_cadence(agent_id, base_cadence_hours, modifiers, current_conditions) -> float`
  - AC: Applies modifiers in sequence: e.g., `volatility_score > 70` with `multiplier: 0.5` halves cadence
  - AC: Clamps result to [MINIMUM_WAKE_CADENCE_HOURS, MAXIMUM_WAKE_CADENCE_HOURS] = [1, 24]
  - Test: Base 6h, volatility=80 with modifier `>70 -> 0.5x` -> effective 3h
  - Test: Base 6h, modifier would give 0.5h -> clamped to 1h
  - Test: Base 6h, modifier would give 30h -> clamped to 24h
  - Test: No modifiers -> returns base cadence unchanged
- [x] Implement `evaluate_modifiers(modifiers: list[dict], conditions: dict) -> float` that evaluates condition expressions against current data
  - AC: Supports conditions like `"volatility_score > 70"`, `"paper_strategy_count > 0"`
  - AC: Returns the product of all matching modifiers
  - Test: Two modifiers both match (0.5 and 0.8) -> combined multiplier 0.4

### 4.30 — Triggers: Built-in + Agent-Defined + Agent Wake Requests

- [x] Implement `wake_controller/triggers.py` with `BuiltInTriggers` class
  - AC: `check_position_loss(agent_id) -> bool` returns True if any live position has unrealized loss >= 25%
  - AC: `check_connectivity(exchange) -> bool` returns True if exchange down > 30 minutes
  - AC: `check_circuit_breaker(portfolio_state) -> bool` returns True if drawdown >= 30%
  - AC: `check_consecutive_failures(agent_id) -> bool` returns True if 3+ consecutive failed cycles
  - Test: Position with -26% unrealized -> triggers; -24% -> does not
- [x] Implement agent-defined triggers: parse `conditional_triggers` from wake_schedule output
  - AC: Evaluates conditions like `"paper_strategy_X pnl crosses success_threshold"`
  - AC: Triggers `immediate_wake` when condition met
  - Test: Paper strategy PnL crosses threshold -> trigger fires
- [x] Implement agent wake requests from message bus: check for `priority: "wake"` messages
  - AC: Polls `agent_messages` table for unread wake-priority messages every 5 minutes
  - AC: Fires out-of-cycle wake for recipient agent
  - Test: Insert wake-priority message for agent_id; trigger check finds it and returns True
- [x] Implement trigger rate limiting
  - AC: Max `MAX_TRIGGER_FIRES_PER_BASE_WINDOW = 2` trigger-fired wakes per base cadence window
  - AC: Minimum `TRIGGER_COOLDOWN_MINUTES = 30` between any two wakes for the same agent
  - Test: Two triggers fire within 30 minutes -> second is suppressed
  - Test: Three triggers within one base window -> third is suppressed

### 4.31 — Wire wake_schedule from Output to Controller

- [x] Implement `update_agent_schedule(agent_id, wake_schedule: dict)` that updates cadence, modifiers, triggers, and memory_query_hints
  - AC: New base_cadence_hours takes effect for next wake calculation
  - AC: New modifiers replace previous modifiers
  - AC: New conditional_triggers replace previous triggers
  - AC: memory_query_hints stored for next cycle's digest builder
  - Test: Agent outputs `base_cadence_hours: 4`; next computed wake is based on 4h, not the config default

### 4.32 — Verify Hard Limits

- [x] Implement cadence clamping validation
  - AC: Agent requesting cadence < 1h gets clamped to 1h with warning logged
  - AC: Agent requesting cadence > 24h gets clamped to 24h with warning logged
- [x] Implement trigger cooldown enforcement
  - AC: After any wake (scheduled or triggered), no new trigger wake for 30 minutes
  - Test: Trigger fires at T=0; another trigger at T=20min -> suppressed; trigger at T=35min -> fires
- [x] Implement per-base-window trigger limit
  - AC: Track trigger fire count per rolling base_cadence window
  - Test: Base cadence 6h, triggers fire at T=0 and T=1h (allowed), trigger at T=2h -> suppressed (already at max 2)

### 4.33 — APScheduler Integration

- [x] Implement `wake_controller/controller.py` with `WakeController` class using APScheduler
  - AC: Creates one `IntervalTrigger` job per enabled agent based on effective cadence
  - AC: Job callback: checks `shutdown_requested`, builds digest, calls agent, dispatches output
  - AC: Reschedules job when cadence changes
- [x] Implement trigger polling job: runs every 5 minutes, checks all built-in and agent-defined triggers
  - AC: If trigger fires and rate limits allow, schedules immediate one-off job for the target agent
- [x] Implement scheduler startup and shutdown
  - AC: `start()` begins the scheduler with configured jobs
  - AC: `stop()` waits for current jobs to complete, then shuts down scheduler
  - Test: Start controller with one agent at 6h cadence; verify job scheduled; stop controller; verify clean shutdown

### 4.34 — test_wake_controller.py

- [x] Test cadence clamping
  - Test: `test_cadence_clamped_to_minimum` — 0.5h input -> 1h effective
  - Test: `test_cadence_clamped_to_maximum` — 30h input -> 24h effective
  - Test: `test_cadence_modifiers_applied` — base 6h with 0.5x modifier -> 3h effective
- [x] Test trigger rate limiting
  - Test: `test_trigger_cooldown_enforced` — two triggers within 30 min, second suppressed
  - Test: `test_trigger_max_per_window` — third trigger in base window suppressed
  - Test: `test_trigger_cooldown_respects_scheduled_wakes` — scheduled wake resets cooldown timer
- [x] Test wake request handling
  - Test: `test_wake_priority_message_triggers_wake` — wake-priority message fires immediate wake
  - Test: `test_wake_request_subject_to_cooldown` — wake request during cooldown suppressed

### Phase 4 Gate

- [x] **Verification checkpoint**: Wake controller starts, schedules agent wakes, processes triggers, enforces all rate limits. Graceful shutdown completes without data loss. `pytest tests/test_wake_controller.py` passes.

---

## Phase 5 — Strategy Lifecycle + Analysis + Robustness

### 5.35 — Analysis Engine (Sync + Async)

- [x] Implement `data_collector/analysis.py` with `AnalysisEngine` class
  - AC: Constructor accepts DB connection for data access
- [x] Implement sync analysis methods (each completes in <60s):
  - `correlation(pairs, timeframe, lookback_days) -> dict` — correlation matrix
  - `rolling_sharpe(pair, timeframe, lookback_days, window) -> dict` — rolling Sharpe ratio series
  - `autocorrelation(pair, timeframe, lookback_days, max_lag) -> dict` — autocorrelation at multiple lags
  - `distribution(pair, timeframe, lookback_days) -> dict` — return distribution stats (mean, std, skew, kurtosis, percentiles)
  - `cointegration(pairs, timeframe, lookback_days) -> dict` — Engle-Granger cointegration test
  - AC: Each returns a dict with results and metadata (pairs, timeframe, computed_at)
  - Test: Feed 100 synthetic candles with known correlation; verify correlation output matches expected value within tolerance
- [x] Implement supplementary feed joins: analysis can operate on OHLCV joined with supplementary_feeds by timestamp
  - AC: `correlation` analysis can correlate a price series with a supplementary feed (e.g., fear_greed_index)
  - Test: Insert OHLCV and fear_greed data with known relationship; correlation analysis detects it
- [x] Implement async analysis request processing: `process_pending_analysis(db)` picks up analysis_requests from events and runs them
  - AC: Results stored in `data/analysis/` directory as JSON files
  - AC: Results surfaced in next digest under REQUESTED ANALYSIS section

### 5.36 — Supplementary Feed Plugin Framework

- [x] Implement `data_collector/feeds/base_feed.py` with `SupplementaryFeed` ABC
  - AC: Abstract methods: `name() -> str`, `source() -> str`, `resolution() -> str`, `fetch() -> list[dict]`
  - AC: Optional methods: `requires_api_key() -> bool` (default False), `estimated_monthly_cost() -> float` (default 0.0)
- [x] Implement `data_collector/feeds/fear_greed.py` as reference implementation
  - AC: Fetches Fear & Greed Index from alternative.me API (free, no key)
  - AC: Returns `[{"feed_name": "fear_greed_index", "timestamp": ..., "value": 0-100, "source": "alternative.me"}]`
  - Test: Mock API response; verify fetch returns correctly structured data
- [x] Implement feed manager: `FeedManager` that loads, schedules, and runs active feeds
  - AC: Auto-discovers feed plugins in `data_collector/feeds/` directory
  - AC: Only runs feeds with status="active" in feed_registry
  - AC: Inserts fetched data into supplementary_feeds table
  - AC: Updates last_fetch and error_count in feed_registry
  - Test: Register and activate fear_greed feed; run collection; verify data in supplementary_feeds table
- [x] Implement data_request processing: when agent requests new feed, check existence and requirements
  - AC: Existing plugin with no key required -> auto-activate
  - AC: Plugin requiring API key -> create owner_request
  - AC: Feed costing > $10/month -> create budget_approval owner_request
  - AC: No plugin exists -> create system_improvement_request

### 5.37 — Backtest Runner

- [x] Implement `strategies/backtest_runner.py` with `BacktestRunner` class
  - AC: Constructor accepts DB connection for OHLCV data access
- [x] Implement `run_backtest(strategy_class, hypothesis_config, lookback_days=90) -> dict`
  - AC: Loads historical OHLCV data for required feeds
  - AC: Instantiates strategy and calls `on_data()` for each data point in chronological order
  - AC: Collects all Signals and simulates execution with slippage and fees
  - AC: Computes metrics: total_return, sharpe_ratio, max_drawdown, win_rate, trade_count, avg_trade_duration
  - AC: Computes counterfactual benchmark performance over same period
  - Test: Strategy that buys and holds BTC for 90 days -> metrics match manual calculation
  - Test: Strategy that generates 0 trades -> returns valid metrics with trade_count=0
- [x] Implement automatic advancement: if backtest meets success_criteria in hypothesis, automatically queue robustness testing
  - AC: Checks `success_criteria.metric` against `success_criteria.threshold` and `minimum_trade_count`
  - AC: If met, updates strategy_registry stage to "robustness" and queues robustness run
  - AC: If not met, updates stage to "graveyard" with failure documentation
  - Test: Strategy with Sharpe 1.5 (threshold 1.0) and 20 trades (min 15) -> advances to robustness
  - Test: Strategy with Sharpe 0.5 (threshold 1.0) -> moved to graveyard

### 5.38 — strategies/robustness.py

- [x] Implement `random_entry_test(strategy_class, data, original_trades, n_runs=1000, seed=42) -> dict`
  - AC: Generates random entry signals at same frequency as original strategy
  - AC: Keeps exit logic, sizing, and costs identical
  - AC: Returns `sharpe_percentile`, `total_return_percentile`, `mean_random_sharpe`, `n_runs`
  - AC: Percentile calculated as percentage of random runs the strategy beats
  - Test: Strategy with genuinely random entries -> percentile near 50th
  - Test: Strategy with perfect entries (always buy at bottom) -> percentile near 99th+
- [x] Implement `return_permutation_test(trade_returns, starting_capital, n_runs=1000, seed=42) -> dict`
  - AC: Shuffles trade return sequence and recomputes equity curves
  - AC: Returns `final_equity_percentile`, `drawdown_resilience_percentile`, `n_runs`
  - Test: Returns with no path dependency (all same size) -> percentile near 50th
  - Test: Returns with strong path dependency (big wins early) -> percentile significantly above 50th
- [x] Implement `compute_equity_curve(trade_returns, starting_capital) -> np.array` and `max_drawdown(equity_curve) -> float`
  - AC: Equity curve correctly compounds returns
  - AC: Max drawdown returns the largest peak-to-trough decline as a positive fraction
  - Test: Curve [100, 110, 90, 95] -> max_drawdown = (110-90)/110 = 0.1818

### 5.39 — Wire: Passing Backtest to Auto Robustness to Digest

- [x] Implement the pipeline: backtest completes -> check success criteria -> if pass, auto-queue robustness -> robustness completes -> results stored -> surfaced in digest
  - AC: No manual intervention required between backtest pass and robustness start
  - AC: Robustness results stored in strategy_registry.robustness_results as JSON
  - AC: Digest BACKTEST QUEUE section shows status: "pending backtest", "backtesting", "pending robustness", "robustness running", "awaiting review"
  - Test: Submit hypothesis -> backtest passes -> robustness runs -> digest shows results with percentiles
- [x] Add robustness results to digest builder
  - AC: Shows random entry percentiles and return permutation percentiles
  - AC: Highlights if percentiles are below the hypothesis's stated thresholds

### 5.40 — Strategy Registry (Namespaced)

- [x] Implement `strategies/registry.py` with `StrategyRegistry` class
  - AC: All operations scoped by agent's strategy namespace
  - AC: `register(hypothesis_id, agent_id, namespace, config) -> str` creates registry entry at "hypothesis" stage
  - AC: `advance(strategy_id, new_stage)` moves strategy forward in lifecycle (hypothesis->backtest->robustness->paper->live)
  - AC: `demote(strategy_id, reason)` moves live strategy back to paper
  - AC: `kill(strategy_id, reason)` moves to graveyard
  - Test: Register strategy, advance through all stages, verify stage history recorded
- [x] Implement `get_strategies_by_stage(agent_id, stage) -> list[dict]`
  - AC: Returns only strategies in the specified stage for the given agent
  - Test: Multiple strategies at different stages; filter by "paper" returns only paper strategies
- [x] Implement strategy file management: move strategy modules between directories on stage transitions
  - AC: On promote to paper: copy from `strategies/backtest/` to `strategies/paper/`
  - AC: On promote to live: copy from `strategies/paper/` to `strategies/active/`
  - AC: On kill: move to `strategies/graveyard/{namespace}/`

### 5.41 — Graveyard Archiver (Namespaced)

- [x] Implement `strategies/graveyard.py` with `GraveyardArchiver` class
  - AC: `archive(strategy_id, reason, agent_id, namespace)` moves strategy to graveyard
  - AC: Archives full documentation: hypothesis, backtest results, robustness results, paper results, kill reason
  - AC: Files stored in `strategies/graveyard/{namespace}/{strategy_id}/`
  - Test: Archive a strategy with full results; verify all artifacts present in graveyard directory
- [x] Implement `get_graveyard_summary(agent_id) -> dict` for digest
  - AC: Returns count by failure type (backtest_fail, robustness_fail, paper_fail, manual_kill)
  - AC: Returns recent failures (last 5) with brief reason
  - Test: Archive 3 strategies with different failure types; summary counts match

### 5.42 — Research Note Lifecycle

- [x] Implement research note age tracking: increment `age_cycles` on each agent cycle
  - AC: Every cycle, all research notes for that agent have age_cycles incremented
- [x] Implement expiry notification: when age_cycles reaches 8, mark for "approaching expiry" in digest
  - AC: Digest HYPOTHESIS QUEUE section highlights notes at cycle 8 with "expires in 2 cycles"
  - Test: Note at age 8 shows warning; note at age 7 does not
- [x] Implement auto-expiry at cycle 10: notes with age_cycles >= 10 and status still "research" are expired
  - AC: Status updated to "expired"
  - AC: Expired notes no longer appear in digest
  - Test: Note at age 10 auto-expires; note at age 9 does not

### 5.43 — Test Robustness and Digest Builder

- [x] Implement `tests/test_robustness.py`
  - Test: `test_random_entry_produces_valid_percentiles` — percentiles are floats between 0 and 100
  - Test: `test_random_entry_reproducible_with_seed` — same seed produces same results
  - Test: `test_return_permutation_produces_valid_percentiles` — percentiles between 0 and 100
  - Test: `test_return_permutation_reproducible_with_seed` — same seed, same results
  - Test: `test_equity_curve_computation` — known returns produce known equity curve
  - Test: `test_max_drawdown_computation` — known curve produces known drawdown
- [x] Implement `tests/test_digest_builder.py`
  - Test: `test_per_agent_scoping` — quant agent sees only own positions and strategies
  - Test: `test_empty_section_collapsing` — sections with no data produce single line
  - Test: `test_supplementary_feeds_included` — supplementary feed data appears in MARKET CONDITIONS
  - Test: `test_robustness_results_in_backtest_queue` — completed robustness shows percentiles
  - Test: `test_agent_messages_shown` — unread messages appear in digest
  - Test: `test_system_updates_section` — shipped improvements, new feeds, owner interventions appear

### Phase 5 Gate

- [x] **Verification checkpoint**: Full strategy lifecycle works: hypothesis -> backtest -> auto-robustness -> results in digest. Analysis engine runs sync analyses within 60s. Supplementary feed framework loads plugins and fetches data. Research notes age and expire correctly. `pytest tests/test_robustness.py tests/test_digest_builder.py` passes.

---

## Phase 6 — First Live Cycle

### 6.44 — Run Historical Data Backfill

- [x] Execute `scripts/backfill_historical.sh` against real Kraken API
  - AC: 90+ days of OHLCV data loaded for all monitored pairs (BTC/USD, ETH/USD, SOL/USD, AVAX/USD, LINK/USD) at all timeframes (1m, 1h, 4h, 1d)
  - AC: Coverage report shows no critical gaps in 1h and 4h data
  - AC: Total data volume logged

### 6.45 — Run Full Test Suite

- [x] Execute `pytest tests/` and confirm all tests pass
  - AC: Zero test failures
  - AC: All critical path tests from Phase 3 pass
  - AC: Test coverage report generated

### 6.46 — STATE.md Generator

- [x] Implement `state_generator.py` with `generate_state_md(db) -> str`
  - AC: Output matches STATE.md format from BUILD.md: Global section (total equity, HWM, drawdown, circuit breaker status, active agents) + Per Agent sections (status, cycle, capital, strategy counts, consecutive failures, wake cadence, next wake, last notes)
  - Test: Populate DB with known state; verify STATE.md output matches expected content
- [x] Wire STATE.md generation to run after each cycle and on shutdown
  - AC: STATE.md file updated after every agent cycle completion
  - AC: STATE.md written during graceful shutdown

### 6.47 — First Real Digest for Primary Quant Agent

- [x] Build first digest with real market data
  - AC: Digest includes real OHLCV data from backfilled history
  - AC: Digest includes real volatility scores
  - AC: PORTFOLIO STATE shows actual exchange balance
  - AC: All sections present (most will be empty/collapsed for first cycle)
  - AC: Digest logged to `data/digest_log/digest_001_quant_primary.txt`

### 6.48 — Call Agent in Quant Mode (with Tools)

- [x] Execute first real cycle: digest -> Anthropic API call with BRIEF_QUANT.md as system prompt and real digest as user message
  - AC: API call succeeds and returns valid JSON
  - AC: Agent has access to tools (run_analysis, query_memory, check_backtest_status)
  - AC: Response logged to `data/response_log/response_001_quant_primary.json`

### 6.49 — Parse Output — Expect Research Notes, Tool Calls, Data Requests

- [x] Parse and dispatch first cycle output
  - AC: Output parser successfully extracts JSON
  - AC: Expected first-cycle output includes: research_notes (observations about market), analysis_requests (asking for data), wake_schedule, cycle_notes
  - AC: Per BRIEF.md: "First cycle expectation: research notes, tool calls, analysis requests, wake schedule. Not full hypotheses."
  - AC: Any instructions dispatched correctly to respective tables

### 6.50 — Verify Wake Controller Picks Up Schedule

- [x] Confirm wake controller schedules next wake based on agent's output
  - AC: If agent output included `wake_schedule.base_cadence_hours`, next wake uses that value
  - AC: If agent output included modifiers, they are evaluated against current conditions
  - AC: Next wake time logged

### 6.51 — Confirm Scheduler Running

- [x] Verify APScheduler is running with correct jobs
  - AC: At least one job scheduled for quant_primary
  - AC: Data collector polling job running at configured interval
  - AC: Trigger check job running every 5 minutes
  - AC: System stays up and schedules fire on time

### 6.52 — Verify Events Table Capturing All Activity

- [x] Audit events table after first cycle
  - AC: Events logged for: cycle_start, digest_built, api_call_made, response_parsed, instructions_dispatched, cycle_complete
  - AC: Each event has correct agent_id, cycle number, timestamp, and payload
  - Test: Query events for cycle 1; verify at least 5 distinct event_types present

### Phase 6 Gate

- [x] **Verification checkpoint**: System runs end-to-end with real Kraken data and real Anthropic API. First cycle produces valid output. Wake controller schedules future cycles. Events table captures full audit trail. STATE.md reflects accurate system state. System continues running autonomously after first cycle.

---

## Phase 7 — Hardening + Observability + Improvement Pipeline

### 7.53 — Telegram Message Types

- [x] Implement `telegram/notifier.py` with `TelegramNotifier` class wrapping python-telegram-bot
  - AC: Constructor accepts bot_token and chat_id from config
  - AC: `send_message(text, parse_mode="HTML")` sends to configured chat
- [x] Implement per-agent cycle summary message (icon: chart emoji)
  - AC: Sent after each cycle; includes agent_id, cycle number, regime classification, active strategies count, key actions taken
  - AC: Tagged with agent_id for multi-agent clarity
- [x] Implement trade execution messages: live trade (money bag emoji), paper trade (clipboard emoji)
  - AC: Includes pair, action, size, fill price, strategy_id, agent_id
- [x] Implement hypothesis/strategy lifecycle messages: queued (microscope emoji), robustness done (test tube emoji), promoted (checkmark emoji), killed (cross emoji)
  - AC: Each includes strategy_id, agent_id, relevant metrics
- [x] Implement trigger and alert messages: trigger wake (shuffle emoji), agent message (message emoji), owner request blocking (red circle emoji) / non-blocking (yellow circle emoji), system error (siren emoji), circuit breaker (stop emoji)
  - AC: Blocking owner requests sent with high priority formatting
  - AC: Circuit breaker message includes current equity, HWM, drawdown percentage
- [x] Implement improvement pipeline notifications: shipped improvements shown in cycle summaries
  - AC: When an improvement is marked shipped, next relevant agent cycle summary mentions it

### 7.54 — Bot Commands

- [x] Implement `/requests` command — list pending owner requests with urgency
  - AC: Returns formatted list of all pending owner_requests with id, type, urgency, title, requesting agent
- [x] Implement `/resolve <id> [note]` — resolve an owner request
  - AC: Updates owner_requests status to "resolved" with resolution_note and timestamp
  - AC: Resolution surfaced in requesting agent's next digest
- [x] Implement `/pause [agent_id]` — pause specific or all agents
  - AC: Without agent_id: pauses all agents
  - AC: With agent_id: pauses only that agent
  - AC: Paused agents skipped by wake controller
  - AC: Sends confirmation message
- [x] Implement `/resume [agent_id]` — resume agents, clears circuit breaker
  - AC: Resumes specified or all agents
  - AC: If circuit breaker was triggered, clears it and resets HWM to current equity
  - AC: Sends confirmation with current equity and new HWM
- [x] Implement `/status` — system and all agent states
  - AC: Shows: total equity, HWM, drawdown, circuit breaker status, active agents
  - AC: Per agent: status, cycle count, capital, strategy counts, next wake
- [x] Implement `/cycle <agent_id>` — force immediate wake
  - AC: Schedules one-off immediate wake for specified agent
  - AC: Subject to cooldown (rejects if within cooldown window)
- [x] Implement `/agents` — list agents with status, cadence, capital allocation
  - AC: Shows all configured agents (enabled and disabled) with current state
- [x] Implement `/messages` — recent inter-agent messages
  - AC: Shows last 10 messages with from, to, type, priority, timestamp, preview

### 7.55 — Owner Request Dispatch

- [x] Implement immediate Telegram notification for blocking and high-urgency owner requests
  - AC: Blocking requests sent immediately when created (not batched with cycle summary)
  - AC: Message includes request_id, type, urgency, title, full description, suggested_action, blocked_work
  - Test: Insert blocking owner_request; verify Telegram message sent within 5 seconds

### 7.56 — Owner Response Flow

- [x] Implement resolved request surfacing in next digest
  - AC: When owner resolves request via `/resolve`, the resolution appears in agent's next digest under SYSTEM UPDATES
  - AC: Includes request_id, resolution_note, and any action taken
  - Test: Resolve request; build digest for requesting agent; verify resolution appears

### 7.57 — Dashboard

- [x] Implement `dashboard/generator.py` that produces `dashboard/index.html`
  - AC: Single-file HTML with inline CSS and JS (no external dependencies)
  - AC: Shows per-agent equity curves (paper and live)
  - AC: Shows cross-agent comparison table
- [x] Add strategy lifecycle visualization
  - AC: Shows all strategies by stage with key metrics
  - AC: Graveyard summary with failure reasons
- [x] Add robustness results display
  - AC: For each tested strategy: random entry percentile bar, return permutation percentile bar
- [x] Add risk gate log visualization
  - AC: Shows recent approvals and rejections with reasons
- [x] Add agent message log
  - AC: Timeline of inter-agent messages
- [x] Add supplementary data displays
  - AC: Latest values of active supplementary feeds
- [x] Wire dashboard regeneration to run after each cycle
  - AC: `index.html` updated after every agent cycle

### 7.58 — API Budget Tracking

- [x] Implement `billing/tracker.py` with `APIBudgetTracker` class
  - AC: Tracks per-cycle cost based on token usage from Anthropic API response
  - AC: Tracks per-agent and total monthly spend
  - AC: Alerts owner via Telegram if projected monthly spend exceeds $50 (MAX_MONTHLY_API_BUDGET_USD)
  - Test: Simulate 10 cycles at $0.01 each over 3 days; projected monthly = ~$1.00; no alert
  - Test: Simulate 10 cycles at $0.50 each over 3 days; projected monthly = ~$50; alert fires
- [x] Add budget tracking to digest: SYSTEM HEALTH section includes current month spend and projection
  - AC: Shows "$X spent this month, projected $Y at current rate"

### 7.59 — Dry-Run Mode Flag

- [x] Implement dry_run mode throughout the system
  - AC: When `config["dry_run"] == True`: live executor logs but does not place orders; all other components run normally
  - AC: Telegram messages tagged with "[DRY RUN]" prefix
  - AC: STATE.md shows "Mode: DRY RUN"
  - Test: Run full cycle in dry_run mode; verify no exchange API calls made for order placement; verify orders logged

### 7.60 — System Improvement Pipeline

- [x] Implement parser writing system_improvement_requests to SQLite with de-duplication
  - AC: New requests from agent output inserted into system_improvement_requests table
  - AC: Near-duplicate detection (similar title + overlapping problem): merge impact, upgrade priority
  - AC: Per-agent limit of 3 requests per cycle enforced
  - Test: Submit "Rolling beta computation" and "Rolling beta calculation" -> merged into one row
  - Test: Submit 5 requests in one cycle -> only 3 inserted, 2 held for next cycle
- [x] Implement `scripts/generate_review_report.py`
  - AC: Queries all pending improvement requests grouped by priority (high first) then by category
  - AC: Output format matches BUILD.md review report format: request ID, requesting agent, cycle/age, title, problem, impact, category, APPROVE/DECLINE/DEFER options
  - Test: Insert 3 requests (1 high, 2 normal); report shows high priority first
- [x] Implement `scripts/mark_shipped.py --requests <ids>`
  - AC: Updates status to "shipped", sets shipped_at timestamp
  - AC: Adds status_note describing what was implemented
- [x] Add SYSTEM UPDATES digest section for improvements
  - AC: Shipped improvements since last cycle shown with: title, what changed, how to use it
  - AC: Pending requests shown with current status
  - Test: Mark request shipped; next digest for requesting agent shows it under SYSTEM UPDATES
- [x] Implement `/review` Telegram command
  - AC: Triggers immediate generation of review report
  - AC: Sends report summary to Telegram
- [x] Implement `/improvements` Telegram command
  - AC: Lists all pending improvement requests with ID, title, agent, priority, age
- [x] Implement `/ship <request_id>` Telegram command
  - AC: Marks request as shipped with owner-provided note
- [x] Implement `/decline <id> <note>` Telegram command
  - AC: Marks request as declined with reason; reason surfaced to agent in next digest

### 7.61 — RUNBOOK.md

- [x] Write RUNBOOK.md with all sections from BUILD.md skeleton
  - AC: Contains: First-Time Setup, Daily Operations, Managing Agents, Troubleshooting, Circuit Breaker, System Improvement Reviews, Backup and Recovery, Stopping the System
  - AC: Each section has actionable steps, not just placeholders
  - AC: First-Time Setup references specific scripts and commands
  - AC: Troubleshooting includes specific error scenarios and recovery steps

### 7.62 — Database Backup Cron Job

- [x] Document database backup recommendation in RUNBOOK.md
  - AC: Recommends daily SQLite backup via cron (`sqlite3 db.sqlite ".backup backup_$(date).sqlite"`)
  - AC: Recommends 7-day retention for backups
  - AC: Includes restore procedure
- [x] Implement `scripts/backup_db.sh` backup script
  - AC: Copies SQLite database to backup directory with timestamp
  - AC: Removes backups older than 7 days
  - AC: Logs backup success/failure

### Phase 7 Gate

- [x] **Verification checkpoint**: Telegram bot responds to all commands. Owner requests flow end-to-end (agent creates -> Telegram notification -> owner resolves -> agent sees resolution). Dashboard renders with real data. API budget tracking alerts on projected overspend. Dry-run mode prevents real orders. System improvement pipeline handles full lifecycle (request -> review -> ship -> notify agent). RUNBOOK.md is complete and accurate.

---

## Phase 8 — Memory (~2 weeks in)

### 8.63 — memory/encoder.py — Per-Agent Memvid Encoding

- [x] Implement `memory/encoder.py` with `MemoryEncoder` class
  - AC: Constructor accepts agent_id and path to .mv2 file
- [x] Implement `encode_cycle(cycle_data: dict)` that produces a memory record from cycle output
  - AC: Record includes: cycle number, timestamp, regime_classification, market_assessment, active strategies (names + stages), killed strategies, cycle_notes (verbatim), key events, tool calls made, messages sent/received
  - AC: Record encoded and appended to agent's .mv2 file using memvid API
  - Test: Encode a sample cycle; verify record retrievable from .mv2 file
- [x] Implement automatic encoding: wire into cycle completion flow
  - AC: After every successful cycle, memory encoding runs automatically
  - AC: Failed cycles are not encoded (no garbage in memory)

### 8.64 — Initial .mv2 File for Primary Quant

- [x] Create initial .mv2 file for quant_primary agent
  - AC: File created at `memory/quant_primary.mv2`
  - AC: If system has been running, backfill from existing response_log files
  - AC: If fresh start, create empty .mv2 ready for first encoding

### 8.65 — memory/retriever.py — Semantic Search

- [x] Implement `memory/retriever.py` with `MemoryRetriever` class
  - AC: Constructor accepts path to .mv2 file
- [x] Implement `search(query: str, top_k: int = 5) -> list[dict]` semantic search
  - AC: Returns top_k most relevant memory records sorted by relevance score
  - AC: Each result includes: cycle number, timestamp, summary, relevance_score
  - Test: Encode 10 cycles about different topics; search for "momentum strategy" returns cycles where momentum was discussed
- [x] Implement `get_recent(n: int = 3) -> list[dict]` for temporal queries
  - AC: Returns the n most recent memory records
  - AC: Preserves insertion order (sequential frame structure)

### 8.66 — Wire query_memory Tool to Retriever

- [x] Replace stub `handle_query_memory` in tool_executor.py with real retriever call
  - AC: Tool call `query_memory(query="momentum strategies", top_k=3)` returns actual search results from agent's .mv2 file
  - AC: Response time < 1 second
  - AC: Returns empty results list (not error) if .mv2 file has no relevant records
  - Test: Encode 5 cycles, call tool with relevant query, verify results returned within 1s

### 8.67 — Automatic Retrieval -> RELEVANT HISTORY in Digest

- [x] Implement automatic memory retrieval in digest builder
  - AC: Before building digest, system constructs a context query from: current market conditions, active strategies, recent events
  - AC: Retrieves top 5 relevant historical records
  - AC: Populates RELEVANT HISTORY section with: cycle number, timestamp, summary, why relevant now
  - Test: Agent discussed "funding rate arbitrage" in cycle 5; current conditions show extreme funding rates; cycle 5 surfaced in RELEVANT HISTORY
- [x] Implement relevance explanation: for each retrieved record, generate brief explanation of why it was surfaced
  - AC: Explanation is 1-2 sentences connecting the historical context to current conditions

### 8.68 — Wire memory_query_hints from Output

- [x] Implement memory_query_hints processing from agent output
  - AC: Agent's `wake_schedule.memory_query_hints` (e.g., `["search: similar volatility regime", "search: prior funding rate arb attempts"]`) are stored and used in next cycle's automatic retrieval
  - AC: Hints supplement (not replace) the automatic context query
  - Test: Agent outputs hint "search: BTC correlation breakdown"; next cycle's RELEVANT HISTORY prioritizes records mentioning BTC correlation

### Phase 8 Gate

- [x] **Verification checkpoint**: Memory encoding runs after each cycle. Semantic search returns relevant results. query_memory tool returns real results within 1 second. RELEVANT HISTORY section populated in digest with explanations. memory_query_hints from agent output influence next cycle's retrieval.

---

## Phase 9 — Multi-Agent Activation (when ready)

### 9.69 — Write PM, Risk, and Specialized Quant Briefs

- [ ] Write `briefs/BRIEF_PM.md` — Portfolio Manager brief
  - AC: Defines PM role: allocates capital across quant agents, resolves cross-agent conflicts, sets risk budgets, monitors portfolio-level performance
  - AC: Specifies PM output format: capital_allocations, task_requests, regime_broadcast, risk_budget_updates
  - AC: Includes PM-specific tool descriptions: list_agent_messages, check_positions, check_exposure
  - AC: Default cadence: 24h
- [ ] Write `briefs/BRIEF_RISK.md` — Risk Monitor brief
  - AC: Defines Risk Monitor role: fast-cadence portfolio health checks, correlation monitoring, exposure alerts
  - AC: Read-only (no trade authority), can wake any agent
  - AC: Specifies lightweight digest format (abbreviated market conditions, full exposure data)
  - AC: Default cadence: 30 minutes
- [ ] Write `briefs/BRIEF_QUANT_MICRO.md` — Micro-trading Quant brief
  - AC: Specialized for high-frequency-ish strategies (4h cadence)
  - AC: Mandate: mean-reversion, orderbook signals, short-duration trades
  - AC: Distinct strategy namespace: "micro"
- [ ] Write `briefs/BRIEF_QUANT_BARBELL.md` — Barbell Quant brief
  - AC: Specialized for barbell strategy: combination of safe positions + high-risk high-reward bets
  - AC: Longer cadence (8h), more conservative sizing
  - AC: Distinct strategy namespace: "barbell"

### 9.70 — Enable Agents in Config, Set Capital Allocations

- [ ] Add new agent configurations to config.yaml with correct capital allocations
  - AC: All enabled quant agents' capital_allocation_pct sums to exactly 1.0
  - AC: Startup validation catches sum > 1.0
  - Test: Enable quant_primary at 0.5 + quant_micro at 0.3 + quant_barbell at 0.2 = 1.0 -> passes validation
  - Test: Enable all three at 0.5 each = 1.5 -> raises ValueError
- [ ] Update wake controller to schedule jobs for all enabled agents
  - AC: Each enabled agent gets its own scheduled job at its configured cadence
  - AC: Disabled agents are not scheduled

### 9.71 — Verify Inter-Agent Messaging + Wake Triggers

- [ ] Test message delivery between agents
  - AC: Quant agent sends escalation to PM; PM sees it in AGENT MESSAGES section of next digest
  - AC: PM sends task_request to quant; quant sees it in AGENT MESSAGES
  - AC: Risk monitor sends risk_alert to all; all agents see it
  - Test: Insert message from quant_primary to portfolio_manager with priority "wake"; verify PM wakes out of cycle
- [ ] Test task lifecycle
  - AC: PM sends task_request with task_id; quant responds with task_response referencing same task_id; PM sees response
  - AC: Tasks ignored for 3 cycles surfaced as stale in PM digest
  - Test: Create task, wait 3 quant cycles without response; verify "stale task" warning in PM digest
- [ ] Test broadcast messages
  - AC: Message with to_agent="all" delivered to every agent's digest
  - AC: PM regime_broadcast reaches all active quant agents

### 9.72 — Verify Cross-Agent Risk Gate

- [ ] Test global exposure limits across agents
  - AC: Agent A at 40% exposure, Agent B tries to add 45% -> rejected (would exceed 80% global gross)
  - Test: Both agents long BTC; combined pair exposure approaching 50% -> next BTC buy rejected
- [ ] Test cross-agent conflict detection
  - AC: Agent A long BTC, Agent B shorts BTC -> conflict logged, PM alerted via risk_alert message
  - AC: Conflicting signal is NOT blocked (PM decides resolution)
  - Test: Create opposing positions from two agents; verify agent_messages table has conflict alert to PM
- [ ] Test circuit breaker across all agents
  - AC: Circuit breaker fires -> ALL agents paused, ALL positions (both agents) closed
  - AC: Owner must /resume to clear
  - Test: Set equity to 30% below HWM; verify all agents paused and all positions closed

### 9.73 — PM Comparative Digest

- [ ] Implement PM-specific digest sections
  - AC: PM sees ALL strategies across ALL agents with comparative metrics
  - AC: PM sees full portfolio exposure breakdown by agent
  - AC: PM sees all inter-agent messages (full traffic, not just own inbox)
  - AC: PM sees all agent performance: equity curves, Sharpe ratios, strategy success rates
- [ ] Implement capital allocation tracking in PM digest
  - AC: Shows current allocation vs. performance: "quant_primary: 50% capital, 60% of total PnL"
  - AC: Shows per-agent risk utilization: "quant_micro using 35% of its allocated capital"

### 9.74 — Risk Monitor Cadence Tuning + Lightweight Digest

- [ ] Implement lightweight digest for risk monitor (30-minute cadence)
  - AC: Abbreviated MARKET CONDITIONS (only monitored pairs with extreme moves)
  - AC: Full exposure breakdown: per-agent, per-pair, gross/net
  - AC: Position-level detail: entry price, current price, unrealized PnL, time held
  - AC: Correlation matrix of current positions
  - AC: NO strategy details, NO hypothesis queue, NO graveyard (not risk monitor's concern)
- [ ] Implement risk monitor tools: check_positions, check_exposure
  - AC: `check_positions` returns current positions across all agents: pair, size, entry, current, unrealized_pnl, agent_id
  - AC: `check_exposure` returns: gross exposure, net exposure, per-pair exposure, per-agent exposure, correlation between position pairs
- [ ] Tune polling to minimize API cost
  - AC: Risk monitor digest is significantly smaller than quant digest (target: <1000 tokens of context)
  - AC: Risk monitor response expected to be short: risk assessment + any risk_alert messages
  - AC: Use default_model (Sonnet) for all risk monitor cycles

### 9.75 — Per-Agent .mv2 Memory Files

- [ ] Create .mv2 files for all new agents
  - AC: `memory/risk_monitor.mv2` created and wired to risk monitor's memory encoder
  - AC: `memory/portfolio_manager.mv2` created and wired to PM's memory encoder
  - AC: Each new quant agent gets its own `memory/{agent_id}.mv2`
- [ ] Implement cross-agent memory access for PM
  - AC: PM's query_memory tool can optionally search other agents' memory files
  - AC: PM tool schema includes optional `agent_id` parameter; default = own memory; specific agent_id = that agent's memory
  - Test: PM queries quant_primary's memory; receives results from quant_primary.mv2
- [ ] Verify per-agent memory isolation for non-PM agents
  - AC: Quant agents can only search their own memory (no cross-agent parameter)
  - AC: Risk monitor can only search its own memory
  - Test: Quant agent tool call with agent_id parameter is ignored; only own memory searched

### Phase 9 Gate

- [ ] **Verification checkpoint**: Multiple agents run concurrently on independent cadences. Inter-agent messaging works end-to-end including wake triggers. PM receives comparative digest with all agent data. Risk monitor runs at 30-minute cadence with lightweight digest. Global risk gate enforces cross-agent limits. Circuit breaker pauses all agents. Capital allocations sum to 1.0. Per-agent memory files operational with PM cross-agent access.

---

## Final Integration Verification

- [ ] Run full system with at least 2 agents for 24 hours
  - AC: Both agents complete multiple cycles without crashes
  - AC: Inter-agent messages delivered correctly
  - AC: Risk gate handles concurrent signals from multiple agents
  - AC: Dashboard shows multi-agent data
  - AC: STATE.md reflects all agents accurately
  - AC: API budget stays within projected range
- [ ] Verify all Telegram commands work with multi-agent system
  - AC: `/status` shows all agents
  - AC: `/pause quant_micro` pauses only quant_micro
  - AC: `/agents` lists all with correct state
- [ ] Verify graceful shutdown preserves all state across all agents
  - AC: Shutdown waits for any in-progress cycles
  - AC: All agent states written to STATE.md
  - AC: Restart picks up where it left off

---

### Critical Files for Implementation

- `d:\Projects\Programming\agentic_quant_system\BUILD.md` - The complete build specification containing all SQL schemas, Python pseudocode, config templates, interface contracts, and the 75-task build order that every subtask above traces back to
- `d:\Projects\Programming\agentic_quant_system\BRIEF.md` - The quant agent operating brief defining the digest format, output JSON schema, strategy lifecycle, tool interface, and hard limits that constrain implementation decisions across Phases 2-6
- `risk/portfolio.py` (to be created) - Core risk gate implementation handling per-agent limits, global exposure checks (80% gross, 50% per-pair), cross-agent conflict detection, and circuit breaker logic; the most safety-critical code in the system
- `claude_interface/caller.py` (to be created) - The agentic caller implementing the tool-use loop (up to 5 iterations), model routing (Sonnet vs Opus), error recovery, and the core API integration that drives every agent cycle
- `digest/builder.py` (to be created) - The digest builder implementing per-agent scoping, all 15+ sections from the BRIEF.md format, empty-section collapsing, and supplementary feed integration; the primary interface between the system and every agent