"""
Historical OHLCV backfill module.

Fetches historical candle data from the exchange for each pair/timeframe
combination and stores it in the ohlcv_cache table. Includes coverage
reporting with gap detection.
"""

import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time

# Ensure project root is on sys.path when run as standalone script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt

from config import load_config
from database.schema import get_db, create_all_tables
from exchange.connector import create_exchange

logger = logging.getLogger(__name__)

# Map timeframe strings to their duration in milliseconds
TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


def backfill(
    exchange,
    db_path: str,
    pairs: list[str],
    days: int,
    timeframes: list[str],
) -> None:
    """Pull historical OHLCV data for each pair/timeframe combination.

    Fetches in batches of 500 candles working backwards from now. Uses
    INSERT OR REPLACE for upsert semantics on the UNIQUE(pair, timeframe,
    timestamp) constraint.

    Args:
        exchange: A ccxt exchange instance.
        db_path: Path to the SQLite database file.
        pairs: List of trading pairs (e.g. ["BTC/USD", "ETH/USD"]).
        days: Number of days of history to fetch.
        timeframes: List of timeframe strings (e.g. ["1h", "4h", "1d"]).
    """
    conn = get_db(db_path)
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - (days * 86_400_000)

    total_combos = len(pairs) * len(timeframes)
    combo_idx = 0

    for pair in pairs:
        for timeframe in timeframes:
            combo_idx += 1
            logger.info(
                "[%d/%d] Backfilling %s %s — %d days",
                combo_idx, total_combos, pair, timeframe, days,
            )

            tf_ms = TIMEFRAME_MS.get(timeframe)
            if tf_ms is None:
                logger.warning(
                    "Unknown timeframe %s, skipping %s/%s",
                    timeframe, pair, timeframe,
                )
                continue

            cursor_ms = since_ms
            total_candles = 0

            while cursor_ms < now_ms:
                try:
                    candles = exchange.fetch_ohlcv(
                        pair, timeframe, since=cursor_ms, limit=500,
                    )
                except ccxt.NetworkError as e:
                    logger.warning(
                        "Network error fetching %s %s (since=%d), retrying: %s",
                        pair, timeframe, cursor_ms, e,
                    )
                    time.sleep(5)
                    continue
                except ccxt.ExchangeError as e:
                    logger.error(
                        "Exchange error fetching %s %s: %s — skipping combo",
                        pair, timeframe, e,
                    )
                    break

                if not candles:
                    break

                rows = []
                for c in candles:
                    ts = datetime.datetime.fromtimestamp(
                        c[0] / 1000, tz=datetime.timezone.utc,
                    ).isoformat()
                    rows.append((pair, timeframe, ts, c[1], c[2], c[3], c[4], c[5]))

                conn.executemany(
                    """INSERT OR REPLACE INTO ohlcv_cache
                       (pair, timeframe, timestamp, open, high, low, close, volume)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                conn.commit()

                total_candles += len(candles)

                # Advance cursor past the last candle we received
                last_ts_ms = candles[-1][0]
                cursor_ms = last_ts_ms + tf_ms

                # If we got fewer than 500 candles, we've reached the end
                if len(candles) < 500:
                    break

                # Rate limit: sleep 1-2 seconds between requests
                time.sleep(1.2)

            logger.info(
                "Finished %s %s — %d candles inserted/updated",
                pair, timeframe, total_candles,
            )

    conn.close()
    logger.info("Backfill complete for %d pair/timeframe combinations", total_combos)


def check_coverage(
    db_path: str,
    pairs: list[str],
    timeframes: list[str],
) -> dict:
    """Report coverage statistics for each pair/timeframe combination.

    For each combo reports: earliest timestamp, latest timestamp, total
    candle count, and any detected gaps. A gap is flagged when the time
    between consecutive candles exceeds 2x the expected interval.

    Args:
        db_path: Path to the SQLite database file.
        pairs: List of trading pairs.
        timeframes: List of timeframe strings.

    Returns:
        Dict keyed by "pair|timeframe" with coverage details.
    """
    conn = get_db(db_path)
    coverage = {}

    for pair in pairs:
        for timeframe in timeframes:
            key = f"{pair}|{timeframe}"
            tf_ms = TIMEFRAME_MS.get(timeframe)
            if tf_ms is None:
                coverage[key] = {"error": f"unknown timeframe {timeframe}"}
                continue

            rows = conn.execute(
                """SELECT timestamp FROM ohlcv_cache
                   WHERE pair = ? AND timeframe = ?
                   ORDER BY timestamp ASC""",
                (pair, timeframe),
            ).fetchall()

            if not rows:
                coverage[key] = {
                    "earliest": None,
                    "latest": None,
                    "total_candles": 0,
                    "gaps": [],
                }
                continue

            timestamps = [row["timestamp"] for row in rows]
            earliest = timestamps[0]
            latest = timestamps[-1]

            # Detect gaps: convert to epoch ms and check intervals
            epoch_ms_list = []
            for ts_str in timestamps:
                dt = datetime.datetime.fromisoformat(ts_str)
                epoch_ms_list.append(int(dt.timestamp() * 1000))

            gaps = []
            expected_interval_ms = tf_ms
            gap_threshold_ms = expected_interval_ms * 2

            for i in range(1, len(epoch_ms_list)):
                delta = epoch_ms_list[i] - epoch_ms_list[i - 1]
                if delta > gap_threshold_ms:
                    gap_start = timestamps[i - 1]
                    gap_end = timestamps[i]
                    missing_candles = int(delta / expected_interval_ms) - 1
                    gaps.append({
                        "from": gap_start,
                        "to": gap_end,
                        "missing_candles": missing_candles,
                    })

            coverage[key] = {
                "earliest": earliest,
                "latest": latest,
                "total_candles": len(timestamps),
                "gaps": gaps,
            }

    conn.close()
    return coverage


def main():
    """CLI entry point for historical OHLCV backfill."""
    parser = argparse.ArgumentParser(
        description="Backfill historical OHLCV data from exchange",
    )
    parser.add_argument(
        "--pairs",
        default="all",
        help='Comma-separated pairs or "all" to read from config (default: all)',
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Number of days to backfill (default: 180)",
    )
    parser.add_argument(
        "--timeframes",
        default="1m,1h,4h,1d",
        help="Comma-separated timeframes (default: 1m,1h,4h,1d)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--db",
        default="data/quant.db",
        help="Path to SQLite database (default: data/quant.db)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    config = load_config(args.config)

    # Resolve pairs
    if args.pairs == "all":
        pairs = config.get("data", {}).get("monitored_pairs", [])
        if not pairs:
            logger.error("No monitored_pairs found in config")
            return
        logger.info("Using pairs from config: %s", pairs)
    else:
        pairs = [p.strip() for p in args.pairs.split(",")]

    timeframes = [str(tf).strip() for tf in args.timeframes.split(",")]

    # Create exchange, ensure tables exist
    exchange = create_exchange(config)
    create_all_tables(args.db)

    # Run backfill
    logger.info(
        "Starting backfill: pairs=%s, days=%d, timeframes=%s",
        pairs, args.days, timeframes,
    )
    backfill(exchange, args.db, pairs, args.days, timeframes)

    # Print coverage report
    coverage = check_coverage(args.db, pairs, timeframes)
    logger.info("=== Coverage Report ===")
    for key, info in coverage.items():
        if info.get("error"):
            logger.warning("  %s — %s", key, info["error"])
            continue
        gap_count = len(info["gaps"])
        logger.info(
            "  %s — candles: %d, earliest: %s, latest: %s, gaps: %d",
            key,
            info["total_candles"],
            info["earliest"],
            info["latest"],
            gap_count,
        )
        for gap in info["gaps"]:
            logger.info(
                "    gap: %s → %s (~%d missing candles)",
                gap["from"], gap["to"], gap["missing_candles"],
            )


if __name__ == "__main__":
    main()
