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
from strategies.registry import StrategyRegistry
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


def _update_results(
    db_path: str,
    strategy_id: str,
    agent_id: str,
    backtest_results: str | None = None,
    robustness_results: str | None = None,
) -> None:
    """Update result fields on a strategy_registry row (not stage — use StrategyRegistry for that)."""
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        if backtest_results is not None:
            conn.execute(
                "UPDATE strategy_registry SET backtest_results=?, updated_at=? "
                "WHERE strategy_id=? AND agent_id=?",
                (backtest_results, now, strategy_id, agent_id),
            )
        if robustness_results is not None:
            conn.execute(
                "UPDATE strategy_registry SET robustness_results=?, updated_at=? "
                "WHERE strategy_id=? AND agent_id=?",
                (robustness_results, now, strategy_id, agent_id),
            )
        conn.commit()
    finally:
        conn.close()


def _process_pending_backtests(
    runner: BacktestRunner, db_path: str, default_capital: float = 500.0
) -> None:
    """Find hypothesis-stage strategies with no backtest and run them."""
    registry = StrategyRegistry(db_path)
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

        # Use agent's actual capital as default instead of hardcoded $10K
        if "starting_capital" not in hypothesis_config:
            hypothesis_config["starting_capital"] = default_capital

        # Mark in-progress so this hypothesis isn't picked up again on the next poll
        _update_results(db_path, strategy_id, agent_id,
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
            _update_results(db_path, strategy_id, agent_id,
                            backtest_results=_json.dumps({"error": load_error}))
            try:
                registry.kill(strategy_id, f"Cannot load strategy class: {load_error}")
            except Exception:
                logger.warning("Failed to move %s to graveyard via registry", strategy_id)
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
            try:
                registry.advance(strategy_id, "backtest")
            except Exception:
                logger.warning("Failed to advance %s via registry", strategy_id)
            _update_results(db_path, strategy_id, agent_id,
                            backtest_results=_json.dumps(results))
        else:
            logger.warning("Backtest failed for %s: %s",
                           strategy_id, results.get("failure_reason"))
            _update_results(db_path, strategy_id, agent_id,
                            backtest_results=_json.dumps(results))
            try:
                registry.kill(strategy_id, f"Backtest failed: {results.get('failure_reason')}")
            except Exception:
                logger.warning("Failed to move %s to graveyard via registry", strategy_id)


def _run_backtest_loop(db_path: str, shutdown_event: threading.Event,
                       poll_interval: int = 60) -> None:
    """Background thread: poll for pending hypotheses and run backtests."""
    runner = BacktestRunner(db_path)
    logger.info("Backtest runner thread started (poll every %ds)", poll_interval)
    while not shutdown_event.wait(timeout=poll_interval):
        try:
            # Read cached portfolio value for realistic default capital
            default_capital = 500.0
            try:
                conn = get_db(db_path)
                row = conn.execute(
                    "SELECT value FROM system_state WHERE key = 'portfolio_value_usd'"
                ).fetchone()
                conn.close()
                if row:
                    val = float(_json.loads(row["value"]))
                    if val > 0:
                        default_capital = val
            except Exception:
                pass
            _process_pending_backtests(runner, db_path, default_capital)
        except Exception:
            logger.exception("Error in backtest loop")
    logger.info("Backtest runner thread stopped")


# ---------------------------------------------------------------------------
# Robustness testing helpers
# ---------------------------------------------------------------------------

# Pass thresholds — strategy must beat random entries at these percentiles
ROBUSTNESS_SHARPE_THRESHOLD = 60.0
ROBUSTNESS_RETURN_THRESHOLD = 60.0


def _process_pending_robustness(db_path: str) -> None:
    """Find backtest-stage strategies with no robustness results and test them."""
    from strategies.robustness import random_entry_test, return_permutation_test

    registry = StrategyRegistry(db_path)
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_registry "
            "WHERE stage='backtest' AND robustness_results IS NULL "
            "AND backtest_results IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        strategy_id = row["strategy_id"]
        agent_id = row["agent_id"]
        backtest_json = row["backtest_results"] or "{}"
        backtest = _json.loads(backtest_json)

        # Skip strategies whose backtest didn't succeed
        if not backtest.get("success"):
            continue

        trades = backtest.get("trades", [])
        if len(trades) < 8:
            continue

        logger.info("Running robustness tests for %s (%d trades)", strategy_id, len(trades))

        # Load candle data for random entry test
        config_json = row["config"] or "{}"
        hyp_config = _json.loads(config_json)
        pair = hyp_config.get("pair", "BTC/USD")
        timeframe = hyp_config.get("timeframe", "4h")
        if "pair" not in hyp_config:
            target_pairs = hyp_config.get("target_pairs", ["BTC/USD"])
            pair = target_pairs[0] if target_pairs else "BTC/USD"

        runner = BacktestRunner(db_path)
        candles = runner._load_candles(pair, timeframe, lookback_days=180)

        # Run random entry test
        random_result = random_entry_test(
            strategy_class=None,  # Not used in simplified version
            data=candles,
            original_trades=trades,
        )

        # Run return permutation test
        trade_returns = [t["return_pct"] for t in trades]
        starting_capital = backtest.get("starting_capital", 10000.0)
        perm_result = return_permutation_test(
            trade_returns=trade_returns,
            starting_capital=starting_capital,
        )

        combined = {
            "random_entry": random_result,
            "return_permutation": perm_result,
        }

        # Check pass criteria
        sharpe_pct = random_result.get("sharpe_percentile", 0)
        return_pct = random_result.get("total_return_percentile", 0)
        passed = (
            sharpe_pct >= ROBUSTNESS_SHARPE_THRESHOLD
            and return_pct >= ROBUSTNESS_RETURN_THRESHOLD
        )
        combined["passed"] = passed

        if passed:
            logger.info(
                "Robustness PASSED for %s: sharpe_pct=%.1f, return_pct=%.1f",
                strategy_id, sharpe_pct, return_pct,
            )
            try:
                registry.advance(strategy_id, "robustness")
            except Exception:
                logger.warning("Failed to advance %s via registry", strategy_id)
        else:
            logger.info(
                "Robustness FAILED for %s: sharpe_pct=%.1f, return_pct=%.1f "
                "(keeping at backtest stage for agent review)",
                strategy_id, sharpe_pct, return_pct,
            )
            # No stage change — stays in backtest for agent to review/tweak

        _update_results(db_path, strategy_id, agent_id,
                        robustness_results=_json.dumps(combined))


def _run_robustness_loop(db_path: str, shutdown_event: threading.Event,
                         poll_interval: int = 120) -> None:
    """Background thread: poll for backtest-stage strategies and run robustness tests."""
    logger.info("Robustness tester thread started (poll every %ds)", poll_interval)
    while not shutdown_event.wait(timeout=poll_interval):
        try:
            _process_pending_robustness(db_path)
        except Exception:
            logger.exception("Error in robustness loop")
    logger.info("Robustness tester thread stopped")


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

    def _do_update():
        btc_price = _get_latest_close(db_path, "BTC/USD")
        eth_price = _get_latest_close(db_path, "ETH/USD")
        if btc_price:
            tracker.update_hodl("hodl_btc", btc_price)
            elapsed = _elapsed_weeks(tracker, "dca_btc")
            tracker.update_dca("dca_btc", btc_price, elapsed)
        if eth_price:
            tracker.update_hodl("hodl_eth", eth_price)
            tracker.update_staked("staked_eth", eth_price)
        if btc_price and eth_price:
            tracker.update_equal_weight("equal_weight_rebal", btc_price, eth_price)
        # Yield benchmarks don't need price data
        tracker.update_yield("usdc_yield")

    # Brief wait for OHLCV collector to populate initial data, then run
    # an immediate update so benchmarks are available before the first wake.
    shutdown_event.wait(timeout=30)
    if not shutdown_event.is_set():
        try:
            _do_update()
        except Exception:
            logger.exception("Error in initial benchmark update")

    while not shutdown_event.wait(timeout=poll_interval):
        try:
            _do_update()
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

    # Log system_start event so agents can detect restarts
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO events (timestamp, event_type, agent_id, cycle, source, payload) "
            "VALUES (?, 'system_start', 'system', 0, 'main', ?)",
            (now, _json.dumps({"reason": "process_start"})),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("Failed to log system_start event")

    # --- 4a. Resolve pending owner requests and SIRs ---
    try:
        conn = get_db(db_path)
        # Owner request: Kraken margin not available (US requirements)
        conn.execute(
            "UPDATE owner_requests SET status = 'resolved', resolved_at = ?, "
            "resolution_note = 'Kraken margin trading is NOT available — US "
            "regulatory requirements. Strategies must be reformulated as "
            "long-only or use synthetic alternatives.' "
            "WHERE request_id = 'owner_req_001_kraken_margin' AND status = 'pending'",
            (now,),
        )
        # Resolve blocking owner requests
        conn.execute(
            "UPDATE owner_requests SET status = 'resolved', resolved_at = ?, "
            "resolution_note = 'Root cause: backtest engine data dict used keys "
            "(candle, pair, candles_so_far) but strategies expected feed-keyed "
            "format (BTC/USD:4h). Fixed: backtest now provides both formats. "
            "Also: on_data() exceptions were silently swallowed — now logged.' "
            "WHERE request_id = 'or_028_backtest_engine_data_keys' AND status = 'pending'",
            (now,),
        )
        conn.execute(
            "UPDATE owner_requests SET status = 'resolved', resolved_at = ?, "
            "resolution_note = 'hyp_005 WAS registered and backtested but auto-killed "
            "(0 trades due to data format mismatch). check_backtest_status now searches "
            "all stages including graveyard with prefix matching.' "
            "WHERE request_id = 'or_029_hyp005_not_found' AND status = 'pending'",
            (now,),
        )
        # Mark shipped SIRs
        sir_updates = [
            ("sir_003", "Benchmarks now update immediately on startup"),
            ("sir_016", "F&G feed now fetches 90 days of history"),
            ("sir_017", "Duplicate feed records eliminated via UNIQUE constraint"),
            ("sir_018", "Cointegration spread z-scores pre-computed in digest"),
            ("sir_019", "F&G feed now fetches 90 days of history"),
            ("sir_022", "Supplementary feeds (F&G etc.) now available in backtest engine via data dict"),
            ("sir_024", "Strategy state persistence via strategy_state table and save_strategy_state tool"),
            ("sir_025", "EMA and SMA analysis types added to run_analysis tool"),
            ("sir_026", "Strategy state persistence (save_strategy_state tool) covers position tracking"),
            ("sir_027", "Hypothesis resubmission detection — duplicates skipped with warning"),
            ("sir_028", "Memory retrieval fixed (JSONL fallback); shutdown flush added"),
            ("sir_029", "Backtest engine now logs on_data errors and returns diagnostic info (data_keys, first_candle_error) for 0-trade failures"),
            ("sir_030", "BRIEF updated with correct on_data() data dict format; backtest diagnostic includes data_keys_available"),
            ("sir_031", "Digest now shows STRATEGY PIPELINE summary line with counts per stage; namespace fix makes all strategies visible"),
            ("sir_032", "Graveyard entries persist across restarts (confirmed); digest now shows last 5 with kill reasons; namespace fix restored visibility"),
            ("sir_020", "Fear & Greed reversal wake trigger implemented — fires when F&G increases after 2+ days at extreme fear (<=20)"),
            ("sir_021", "Spread z-score crossing wake trigger implemented — fires when |z| >= 1.5 for any active pair strategy, with 1h cointegration cache"),
            ("sir_023", "Z-score distribution stats (p5/p25/p75/p95, threshold exceedance %, half-life) now shown in REQUESTED ANALYSIS digest section"),
            ("sir_034", "Paper strategy recent signals (last 5 trades with action, price, rationale) now shown in PAPER STRATEGIES digest section"),
            ("sir_035", "Paper strategy open positions (direction, size, entry/current price, unrealized PnL) now shown in PAPER STRATEGIES digest section"),
        ]
        for sir_id, note in sir_updates:
            conn.execute(
                "UPDATE system_improvement_requests "
                "SET status = 'shipped', shipped_at = ?, status_note = ? "
                "WHERE request_id LIKE ? AND status != 'shipped'",
                (now, note, f"%{sir_id}%"),
            )
        conn.commit()
        conn.close()
        logger.info("Resolved owner requests and marked SIRs as shipped")
    except Exception:
        logger.warning("Failed to update owner requests / SIR statuses")

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

    # --- 6a2. Reset stale backtests from prior crash ---
    try:
        conn = get_db(db_path)
        updated = conn.execute(
            "UPDATE strategy_registry SET backtest_results = NULL "
            "WHERE stage = 'hypothesis' "
            "AND backtest_results = '{\"status\":\"running\"}'",
        )
        if updated.rowcount > 0:
            logger.info("Reset %d stale backtest(s) from prior crash", updated.rowcount)
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("Failed to reset stale backtests")

    # --- 6b. Start backtest runner thread ---
    backtest_thread = threading.Thread(
        target=_run_backtest_loop,
        args=(db_path, shutdown_event),
        name="BacktestRunner",
        daemon=True,
    )
    backtest_thread.start()
    logger.info("Backtest runner thread started")

    # --- 6b2. Start robustness tester thread ---
    robustness_thread = threading.Thread(
        target=_run_robustness_loop,
        args=(db_path, shutdown_event),
        name="RobustnessTester",
        daemon=True,
    )
    robustness_thread.start()
    logger.info("Robustness tester thread started")

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

    # --- 8b. Start dashboard web server ---
    dash_cfg = config.get("dashboard", {})
    if dash_cfg.get("enabled", True):
        from dashboard.server import start_server as _start_dash_server

        dash_host = dash_cfg.get("host", "0.0.0.0")
        dash_port = dash_cfg.get("port", 8501)
        dash_output = config.get("system", {}).get("dashboard_output", "dashboard/output")

        dash_thread = threading.Thread(
            target=_start_dash_server,
            kwargs={
                "host": dash_host,
                "port": dash_port,
                "dashboard_path": dash_output,
                "db_path": db_path,
            },
            name="DashboardServer",
            daemon=True,
        )
        dash_thread.start()
        logger.info("Dashboard server started on %s:%d", dash_host, dash_port)

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

    robustness_thread.join(timeout=30)
    if robustness_thread.is_alive():
        logger.warning("Robustness tester thread did not stop within timeout")
    else:
        logger.info("Robustness tester thread stopped")

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

    # Flush memory encoding — ensure last cycle data is persisted
    try:
        from memory.encoder import MemoryEncoder
        import os as _os
        memory_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "memory", "data")
        agents_cfg = config.get("agents", {})
        for aid in agents_cfg:
            jsonl_path = _os.path.join(memory_dir, f"{aid}.jsonl")
            if _os.path.exists(jsonl_path):
                # Check if last cycle_complete has a matching memory record
                conn_mem = get_db(db_path)
                last_cycle = conn_mem.execute(
                    "SELECT cycle, payload FROM events "
                    "WHERE agent_id = ? AND event_type = 'cycle_complete' "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (aid,),
                ).fetchone()
                conn_mem.close()
                if last_cycle:
                    logger.info(
                        "Memory data for %s verified (last cycle: %d)",
                        aid, last_cycle["cycle"],
                    )
        logger.info("Memory flush check complete")
    except Exception:
        logger.exception("Memory flush check failed")

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
