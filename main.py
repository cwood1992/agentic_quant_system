"""Entry point for the agentic quant trading system.

Handles startup sequencing, signal-based graceful shutdown, data collector
thread management, and wake controller lifecycle.
"""

import importlib
import json as _json
import signal
import sys
import threading
from datetime import datetime, timezone

from benchmarks.tracker import BenchmarkTracker
from config import load_config, validate_config
from dashboard.generator import generate_dashboard
from data_collector.collector import OHLCVCollector
from data_collector.feeds.feed_manager import FeedManager
from database.schema import create_all_tables, get_db
from exchange.connector import create_exchange, verify_connection
from logging_config import get_logger, setup_logging
from state_generator import write_state_md
from strategies.backtest_runner import BacktestRunner
from strategies.base import BaseStrategy
from wake_controller.controller import WakeController

# Global shutdown state
shutdown_requested = False
_shutdown_count = 0
_shutdown_lock = threading.Lock()

logger = None  # Initialised after setup_logging


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown.

    First signal: set shutdown_requested flag and log.
    Second signal: force exit immediately.
    """
    global shutdown_requested, _shutdown_count

    with _shutdown_lock:
        _shutdown_count += 1
        count = _shutdown_count

    if count == 1:
        shutdown_requested = True
        if logger:
            logger.info("Graceful shutdown requested (signal %d)", signum)
        else:
            print(f"Graceful shutdown requested (signal {signum})")
    else:
        if logger:
            logger.warning("Forced shutdown (second signal)")
        else:
            print("Forced shutdown (second signal)")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Backtest runner helpers
# ---------------------------------------------------------------------------

def _get_latest_close(db_path: str, pair: str, timeframe: str = "1h") -> float | None:
    """Return the most recent close price for a pair/timeframe from ohlcv_cache."""
    conn = get_db(db_path)
    try:
        row = conn.execute(
            "SELECT close FROM ohlcv_cache WHERE pair=? AND timeframe=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (pair, timeframe),
        ).fetchone()
        return float(row["close"]) if row else None
    finally:
        conn.close()


def _update_registry(
    db_path: str,
    strategy_id: str,
    agent_id: str,
    stage: str | None = None,
    backtest_results: str | None = None,
) -> None:
    """Update a strategy_registry row's stage and/or backtest_results."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        if stage is not None:
            conn.execute(
                "UPDATE strategy_registry SET stage=?, backtest_results=?, updated_at=? "
                "WHERE strategy_id=? AND agent_id=?",
                (stage, backtest_results, now, strategy_id, agent_id),
            )
        else:
            conn.execute(
                "UPDATE strategy_registry SET backtest_results=?, updated_at=? "
                "WHERE strategy_id=? AND agent_id=?",
                (backtest_results, now, strategy_id, agent_id),
            )
        conn.commit()
    finally:
        conn.close()


def _process_pending_backtests(runner: BacktestRunner, db_path: str) -> None:
    """Find hypothesis-stage strategies with no backtest and run them."""
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_registry "
            "WHERE stage='hypothesis' AND backtest_results IS NULL"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        strategy_id = row["strategy_id"]
        agent_id = row["agent_id"]
        namespace = row["namespace"]
        config_json = row["config"] or "{}"
        hypothesis_config = _json.loads(config_json)

        # Mark in-progress so this hypothesis isn't picked up again on the next poll
        _update_registry(db_path, strategy_id, agent_id,
                         backtest_results='{"status":"running"}')

        # Dynamically load the strategy class.
        # Derive module path from strategy_id (strategies.hypotheses.<strategy_id>)
        # and fall back to namespace if that fails.
        strategy_class = None
        module_candidates = [
            f"strategies.hypotheses.{strategy_id}",
            namespace,
        ]
        load_error = None
        for module_path in module_candidates:
            try:
                module = importlib.import_module(module_path)
                strategy_class = next(
                    v for _k, v in vars(module).items()
                    if isinstance(v, type)
                    and issubclass(v, BaseStrategy)
                    and v is not BaseStrategy
                )
                break
            except StopIteration:
                load_error = f"No BaseStrategy subclass in module '{module_path}'"
            except Exception as exc:
                load_error = str(exc)

        if strategy_class is None:
            logger.error("Cannot load strategy class for %s: %s", strategy_id, load_error)
            _update_registry(db_path, strategy_id, agent_id,
                             stage="graveyard",
                             backtest_results=_json.dumps({"error": load_error}))
            continue

        # Ensure pair and timeframe are set in config for the runner
        if "pair" not in hypothesis_config:
            target_pairs = hypothesis_config.get("target_pairs", ["BTC/USD"])
            hypothesis_config["pair"] = target_pairs[0] if target_pairs else "BTC/USD"
        if "timeframe" not in hypothesis_config:
            hypothesis_config["timeframe"] = "4h"

        # Run backtest
        logger.info("Running backtest for %s (pair=%s tf=%s)",
                    strategy_id, hypothesis_config["pair"], hypothesis_config["timeframe"])
        results = runner.run_backtest(strategy_class, hypothesis_config)

        if results.get("success"):
            logger.info(
                "Backtest passed for %s: %d trades, sharpe=%.2f",
                strategy_id, results.get("trade_count", 0), results.get("sharpe_ratio", 0),
            )
            _update_registry(db_path, strategy_id, agent_id,
                             stage="backtest",
                             backtest_results=_json.dumps(results))
        else:
            logger.warning("Backtest failed for %s: %s",
                           strategy_id, results.get("failure_reason"))
            _update_registry(db_path, strategy_id, agent_id,
                             stage="graveyard",
                             backtest_results=_json.dumps(results))


def _run_backtest_loop(db_path: str, shutdown_event: threading.Event,
                       poll_interval: int = 60) -> None:
    """Background thread: poll for pending hypotheses and run backtests."""
    runner = BacktestRunner(db_path)
    logger.info("Backtest runner thread started (poll every %ds)", poll_interval)
    while not shutdown_event.wait(timeout=poll_interval):
        try:
            _process_pending_backtests(runner, db_path)
        except Exception:
            logger.exception("Error in backtest loop")
    logger.info("Backtest runner thread stopped")


# ---------------------------------------------------------------------------
# Benchmark tracker helpers
# ---------------------------------------------------------------------------

def _elapsed_weeks(tracker: BenchmarkTracker, bench_id: str) -> int:
    """Return number of whole weeks since the first history entry for a benchmark."""
    data = tracker._get_benchmark(bench_id)
    if not data:
        return 0
    history = data.get("history", [])
    if not history:
        return 0
    first = datetime.fromisoformat(history[0]["timestamp"])
    now = datetime.now(timezone.utc)
    return int((now - first).total_seconds() / 604800)


def _run_benchmark_loop(tracker: BenchmarkTracker, db_path: str,
                        shutdown_event: threading.Event,
                        poll_interval: int = 900) -> None:
    """Background thread: update benchmark values from latest OHLCV prices."""
    logger.info("Benchmark tracker thread started (poll every %ds)", poll_interval)
    while not shutdown_event.wait(timeout=poll_interval):
        try:
            btc_price = _get_latest_close(db_path, "BTC/USD")
            eth_price = _get_latest_close(db_path, "ETH/USD")
            if btc_price:
                tracker.update_hodl("hodl_btc", btc_price)
                elapsed = _elapsed_weeks(tracker, "dca_btc")
                tracker.update_dca("dca_btc", btc_price, elapsed)
            if eth_price:
                tracker.update_hodl("hodl_eth", eth_price)
            if btc_price and eth_price:
                tracker.update_equal_weight("equal_weight_rebal", btc_price, eth_price)
        except Exception:
            logger.exception("Error in benchmark update loop")
    logger.info("Benchmark tracker thread stopped")


def main():
    """Run the full system startup sequence and block until shutdown."""
    global logger

    # --- 1. Load and validate config ---
    config = load_config()
    validate_config(config)

    # --- 2. Set up logging ---
    log_dir = config.get("system", {}).get("log_dir", "logs")
    setup_logging(log_dir)
    logger = get_logger("main")
    logger.info("Configuration loaded and validated")

    # --- 3. Register signal handlers ---
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # --- 4. Initialise database ---
    db_path = config.get("system", {}).get("db_path", "data/system.db")
    create_all_tables(db_path)
    logger.info("Database tables initialised at %s", db_path)

    # --- 4b. Seed benchmarks ---
    benchmark_tracker = BenchmarkTracker(db_path)
    logger.info("BenchmarkTracker initialised (benchmarks seeded)")

    # --- 5. Connect to exchange ---
    exchange = create_exchange(config)
    conn_result = verify_connection(exchange)
    if not conn_result.get("connected"):
        logger.error(
            "Exchange connection failed: %s", conn_result.get("error", "unknown")
        )
        sys.exit(1)
    logger.info("Exchange connection verified (USD: %.2f)", conn_result.get("total_usd", 0))

    # --- 6. Start data collector thread ---
    shutdown_event = threading.Event()
    collector = OHLCVCollector(db_path, exchange, config)

    # Gather pairs and timeframes from config
    data_cfg = config.get("data", {})
    pairs = data_cfg.get("monitored_pairs", ["BTC/USD", "ETH/USD"])
    timeframes = data_cfg.get("timeframes", ["1h", "4h"])
    interval = data_cfg.get("collection_interval_seconds", 300)

    collector_thread = threading.Thread(
        target=collector.run,
        args=(pairs, timeframes, interval, shutdown_event),
        name="DataCollector",
        daemon=True,
    )
    collector_thread.start()
    logger.info("Data collector thread started")

    # --- 6b. Start backtest runner thread ---
    backtest_thread = threading.Thread(
        target=_run_backtest_loop,
        args=(db_path, shutdown_event),
        name="BacktestRunner",
        daemon=True,
    )
    backtest_thread.start()
    logger.info("Backtest runner thread started")

    # --- 6c. Start benchmark tracker thread ---
    benchmark_thread = threading.Thread(
        target=_run_benchmark_loop,
        args=(benchmark_tracker, db_path, shutdown_event),
        name="BenchmarkTracker",
        daemon=True,
    )
    benchmark_thread.start()
    logger.info("Benchmark tracker thread started")

    # --- 7. Start supplementary feed manager thread ---
    feed_cfg = config.get("supplementary_feeds", {})
    feed_manager_thread = None
    if feed_cfg.get("enabled", False):
        feed_manager = FeedManager(db_path, feed_cfg)
        poll_interval = feed_cfg.get("polling_interval_seconds", 3600)

        def _feed_manager_loop():
            logger.info("Feed manager thread started (poll every %ds)", poll_interval)
            feed_manager.run_active_feeds()
            feed_manager.process_data_requests()
            while not shutdown_event.wait(timeout=poll_interval):
                feed_manager.run_active_feeds()
                feed_manager.process_data_requests()
            logger.info("Feed manager thread stopped")

        feed_manager_thread = threading.Thread(
            target=_feed_manager_loop,
            name="FeedManager",
            daemon=True,
        )
        feed_manager_thread.start()
        logger.info("Feed manager thread started")

    # --- 8. Start wake controller ---
    wake_controller = WakeController(config, db_path, exchange)

    # Wrap cycle callback to write STATE.md after each cycle
    _original_run_agent_cycle = wake_controller._run_agent_cycle

    def _run_agent_cycle_with_state(agent_id, wake_reason="scheduled"):
        _original_run_agent_cycle(agent_id, wake_reason)
        write_state_md(db_path, config)
        try:
            output_path = config.get("system", {}).get("dashboard_output", "dashboard/output")
            generate_dashboard(db_path, config, output_path)
        except Exception:
            logger.exception("Dashboard generation failed")

    wake_controller._run_agent_cycle = _run_agent_cycle_with_state

    wake_controller.start()
    logger.info("Wake controller started")

    # --- 9. Block until shutdown requested ---
    try:
        while not shutdown_requested:
            # Sleep in short intervals to stay responsive to signals
            shutdown_event.wait(timeout=1.0)
            if shutdown_requested:
                break
    except KeyboardInterrupt:
        # Handled by signal handler, but just in case
        pass

    # --- 9. Shutdown sequence ---
    logger.info("Beginning shutdown sequence")

    # Write final STATE.md before tearing down
    write_state_md(db_path, config)

    # Stop wake controller
    wake_controller.stop()
    logger.info("Wake controller stopped")

    # Stop all background threads (shared shutdown_event)
    shutdown_event.set()
    collector_thread.join(timeout=30)
    if collector_thread.is_alive():
        logger.warning("Data collector thread did not stop within timeout")
    else:
        logger.info("Data collector thread stopped")

    backtest_thread.join(timeout=15)
    if backtest_thread.is_alive():
        logger.warning("Backtest runner thread did not stop within timeout")
    else:
        logger.info("Backtest runner thread stopped")

    benchmark_thread.join(timeout=10)
    if benchmark_thread.is_alive():
        logger.warning("Benchmark tracker thread did not stop within timeout")
    else:
        logger.info("Benchmark tracker thread stopped")

    if feed_manager_thread is not None:
        feed_manager_thread.join(timeout=15)
        if feed_manager_thread.is_alive():
            logger.warning("Feed manager thread did not stop within timeout")
        else:
            logger.info("Feed manager thread stopped")

    # Flush database (ensure WAL is checkpointed)
    try:
        conn = get_db(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        logger.info("Database WAL checkpointed")
    except Exception:
        logger.exception("Failed to checkpoint database WAL")

    logger.info("Shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
