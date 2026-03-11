"""Entry point for the agentic quant trading system.

Handles startup sequencing, signal-based graceful shutdown, data collector
thread management, and wake controller lifecycle.
"""

import signal
import sys
import threading

from config import load_config, validate_config
from data_collector.collector import OHLCVCollector
from data_collector.feeds.feed_manager import FeedManager
from database.schema import create_all_tables, get_db
from exchange.connector import create_exchange, verify_connection
from logging_config import get_logger, setup_logging
from state_generator import write_state_md
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

    # Stop data collector and feed manager (shared shutdown_event)
    shutdown_event.set()
    collector_thread.join(timeout=30)
    if collector_thread.is_alive():
        logger.warning("Data collector thread did not stop within timeout")
    else:
        logger.info("Data collector thread stopped")

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


if __name__ == "__main__":
    main()
