"""Hard limit constants for the agentic quant trading system.

All values are enforced globally and must not be overridden at runtime.
Sourced from the system specification in BUILD.md and BRIEF.md.
"""

# --- Wake / Scheduling ---
MINIMUM_WAKE_CADENCE_HOURS = 1
MAXIMUM_WAKE_CADENCE_HOURS = 24
MAX_TRIGGER_FIRES_PER_BASE_WINDOW = 2
TRIGGER_COOLDOWN_MINUTES = 30

# --- Risk / Drawdown ---
CIRCUIT_BREAKER_DRAWDOWN_PCT = 0.30
POSITION_LOSS_TRIGGER_PCT = 0.25

# --- Exposure ---
GLOBAL_MAX_GROSS_EXPOSURE = 0.80
GLOBAL_MAX_PAIR_EXPOSURE = 0.50
GLOBAL_MAX_CONCURRENT_POSITIONS = 10
DEFAULT_MAX_POSITIONS_PER_AGENT = 5

# --- Claude API / Model Routing ---
DEFAULT_MODEL = "claude-sonnet-4-6"
TRIGGER_MODEL = "claude-opus-4-6"
MAX_MONTHLY_API_BUDGET_USD = 50
MAX_OUTPUT_TOKENS = 16000

# --- Data Feeds ---
AUTO_APPROVE_DATA_FEED_MONTHLY_USD = 10

# --- Order / Execution ---
MINIMUM_ORDER_USD = 5.0

# --- Robustness Testing ---
ROBUSTNESS_N_RUNS = 1000
ROBUSTNESS_RANDOM_SEED = 42
