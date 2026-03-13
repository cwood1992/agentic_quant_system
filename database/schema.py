"""
SQLite schema for the agentic quant trading system.

Creates all tables defined in BUILD.md. Uses WAL mode for concurrent read access.
"""

import sqlite3
import json
import datetime


def get_db(db_path: str) -> sqlite3.Connection:
    """Create a SQLite connection with WAL mode enabled.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        sqlite3.Connection with WAL journal mode.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def create_all_tables(db_path: str) -> None:
    """Create all tables required by the system.

    Args:
        db_path: Path to the SQLite database file.
    """
    conn = get_db(db_path)
    cursor = conn.cursor()

    # --- trades ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            action TEXT NOT NULL,
            size_usd REAL NOT NULL,
            price REAL NOT NULL,
            order_type TEXT NOT NULL,
            fill_price REAL,
            fill_timestamp TEXT,
            fees REAL,
            pnl REAL,
            paper INTEGER NOT NULL DEFAULT 1,
            rationale TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)

    # --- ohlcv_cache ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            UNIQUE(pair, timeframe, timestamp)
        )
    """)

    # --- strategy_registry ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            hypothesis_id TEXT,
            stage TEXT NOT NULL DEFAULT 'hypothesis',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            config TEXT,
            backtest_results TEXT,
            robustness_results TEXT,
            paper_results TEXT
        )
    """)

    # --- research_notes ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS research_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            observation TEXT NOT NULL,
            potential_edge TEXT,
            questions TEXT,
            requested_data TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            age_cycles INTEGER NOT NULL DEFAULT 0
        )
    """)

    # --- instruction_queue ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS instruction_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            strategy_namespace TEXT NOT NULL,
            instruction_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            risk_check_result TEXT,
            executed_at TEXT,
            execution_result TEXT
        )
    """)

    # --- events ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            agent_id TEXT,
            cycle INTEGER,
            source TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)

    # --- agent_messages ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            message_type TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            payload TEXT NOT NULL,
            read_by_cycle INTEGER,
            expires_at TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)

    # --- owner_requests ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS owner_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            type TEXT NOT NULL,
            urgency TEXT NOT NULL DEFAULT 'normal',
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            blocked_work TEXT,
            suggested_action TEXT,
            resolution_method TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT,
            resolution_note TEXT
        )
    """)

    # --- failed_cycles ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS failed_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            raw_output TEXT,
            error TEXT,
            wake_reason TEXT,
            model_used TEXT
        )
    """)

    # --- system_state ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Seed initial system_state rows
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cursor.execute("""
        INSERT OR IGNORE INTO system_state (key, value, updated_at)
        VALUES (?, ?, ?)
    """, ("high_water_mark", json.dumps({"amount": 0}), now))
    cursor.execute("""
        INSERT OR IGNORE INTO system_state (key, value, updated_at)
        VALUES (?, ?, ?)
    """, ("circuit_breaker_status", json.dumps({"status": "normal"}), now))

    # --- system_improvement_requests ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_improvement_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            title TEXT NOT NULL,
            problem TEXT NOT NULL,
            impact TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT DEFAULT 'normal',
            examples TEXT,
            status TEXT DEFAULT 'pending',
            status_note TEXT,
            reviewed_at TEXT,
            shipped_at TEXT,
            review_cycle INTEGER
        )
    """)

    # --- supplementary_feeds ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS supplementary_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            value REAL,
            metadata TEXT,
            source TEXT NOT NULL,
            resolution TEXT DEFAULT 'daily'
        )
    """)
    # Deduplicate any existing rows before adding unique constraint
    cursor.execute("""
        DELETE FROM supplementary_feeds
        WHERE id NOT IN (
            SELECT MIN(id) FROM supplementary_feeds
            GROUP BY feed_name, timestamp
        )
    """)
    cursor.execute("DROP INDEX IF EXISTS idx_supp_feed_time")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_supp_feed_unique
        ON supplementary_feeds(feed_name, timestamp)
    """)

    # --- feed_registry ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feed_registry (
            feed_name TEXT PRIMARY KEY,
            feed_type TEXT NOT NULL,
            source TEXT NOT NULL,
            resolution TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            requested_by TEXT,
            activated_at TEXT,
            last_fetch TEXT,
            error_count INTEGER DEFAULT 0,
            config TEXT
        )
    """)

    conn.commit()
    conn.close()
