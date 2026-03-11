"""OHLCV data collector for the agentic quant trading system.

Fetches candlestick data from the exchange via ccxt, upserts into
the ohlcv_cache table, and provides a volatility scoring utility.
"""

import math
import sqlite3
import threading
import time
from datetime import datetime, timezone

import ccxt
import numpy as np

from database.schema import get_db
from logging_config import get_logger


class OHLCVCollector:
    """Fetches OHLCV candles from an exchange and stores them in SQLite.

    Args:
        db_path: Path to the SQLite database file.
        exchange: A ccxt exchange instance (e.g. from ``create_exchange``).
        config: Application config dict (currently unused, reserved for
                future per-collector settings).
    """

    MAX_RETRIES = 3
    RETRY_DELAY_S = 1.0

    # Minimum seconds between fetches for each timeframe (timeframe_period / 4)
    _TIMEFRAME_SECONDS = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }

    def __init__(self, db_path: str, exchange: ccxt.Exchange, config: dict):
        self.db_path = db_path
        self.exchange = exchange
        self.config = config
        self.logger = get_logger("data_collector.collector")
        # Tracks last successful fetch time per (pair, timeframe)
        self._last_fetch: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Core collection
    # ------------------------------------------------------------------

    def collect_once(
        self, pairs: list[str], timeframes: list[str]
    ) -> None:
        """Fetch OHLCV data for every pair/timeframe combo and upsert into DB.

        Each combination is fetched with up to ``MAX_RETRIES`` attempts when
        the exchange raises ``RateLimitExceeded``.  Fetches are skipped when
        less than ``timeframe_period / 4`` has elapsed since the last fetch,
        preventing unnecessary exchange API calls for slow timeframes.

        Args:
            pairs: List of trading pairs (e.g. ``["BTC/USDT", "ETH/USDT"]``).
            timeframes: List of timeframe strings (e.g. ``["1h", "4h"]``).
        """
        now = time.time()
        conn = get_db(self.db_path)
        try:
            for pair in pairs:
                for timeframe in timeframes:
                    min_interval = self._TIMEFRAME_SECONDS.get(timeframe, 60) / 4
                    last = self._last_fetch.get((pair, timeframe), 0.0)
                    if now - last < min_interval:
                        continue
                    candles = self._fetch_with_retry(pair, timeframe)
                    if candles is None:
                        continue
                    self._upsert_candles(conn, pair, timeframe, candles)
                    self._last_fetch[(pair, timeframe)] = time.time()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def run(
        self,
        pairs: list[str],
        timeframes: list[str],
        interval_seconds: int,
        shutdown_event: threading.Event,
    ) -> None:
        """Continuously collect OHLCV data on a fixed interval.

        Blocks the calling thread until *shutdown_event* is set.

        Args:
            pairs: Trading pairs to collect.
            timeframes: Timeframes to collect.
            interval_seconds: Seconds to wait between collection cycles.
            shutdown_event: A ``threading.Event``; when set the loop exits
                           after the current cycle completes.
        """
        self.logger.info(
            "Collector run loop starting — pairs=%s, timeframes=%s, interval=%ds",
            pairs,
            timeframes,
            interval_seconds,
        )

        while not shutdown_event.is_set():
            try:
                self.collect_once(pairs, timeframes)
                self.logger.info("Collection cycle complete")
            except Exception:
                self.logger.exception("Unexpected error during collection cycle")

            # Wait for the interval, but check shutdown_event frequently so
            # we can exit promptly.
            if shutdown_event.wait(timeout=interval_seconds):
                break

        self.logger.info("Collector run loop stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_with_retry(
        self, pair: str, timeframe: str
    ) -> list | None:
        """Fetch OHLCV candles with rate-limit retry logic.

        Returns:
            A list of candle arrays from ccxt, or ``None`` if all retries
            were exhausted.
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                candles = self.exchange.fetch_ohlcv(
                    pair, timeframe, limit=100
                )
                self.logger.info(
                    "Fetched %d candles for %s/%s",
                    len(candles),
                    pair,
                    timeframe,
                )
                return candles
            except ccxt.RateLimitExceeded:
                self.logger.warning(
                    "Rate limit hit for %s/%s — attempt %d/%d, sleeping %ss",
                    pair,
                    timeframe,
                    attempt,
                    self.MAX_RETRIES,
                    self.RETRY_DELAY_S,
                )
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY_S)
            except ccxt.BaseError:
                self.logger.exception(
                    "Exchange error fetching %s/%s", pair, timeframe
                )
                return None

        self.logger.error(
            "All %d retries exhausted for %s/%s",
            self.MAX_RETRIES,
            pair,
            timeframe,
        )
        return None

    @staticmethod
    def _upsert_candles(
        conn: sqlite3.Connection,
        pair: str,
        timeframe: str,
        candles: list,
    ) -> None:
        """Insert or replace candle rows into ``ohlcv_cache``.

        Each candle from ccxt is ``[timestamp_ms, open, high, low, close, volume]``.
        The timestamp is stored as an ISO-8601 UTC string.
        """
        rows = []
        for c in candles:
            ts_iso = datetime.fromtimestamp(
                c[0] / 1000, tz=timezone.utc
            ).isoformat()
            rows.append((pair, timeframe, ts_iso, c[1], c[2], c[3], c[4], c[5]))

        conn.executemany(
            """
            INSERT OR REPLACE INTO ohlcv_cache
                (pair, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()


# ----------------------------------------------------------------------
# Standalone utilities
# ----------------------------------------------------------------------


def compute_volatility_score(
    db_path: str, pair: str, lookback_hours: int = 24
) -> float:
    """Compute a 0-100 volatility score from recent 1h candles.

    The score is derived from the annualised standard deviation of
    log returns over the lookback window:

        score = min(std_log_returns * sqrt(8760) * 100, 100)

    This maps ~0-1 annualised vol to 0-100.  If there are fewer than
    2 candles in the window, returns the neutral default of 50.0.

    Args:
        db_path: Path to the SQLite database.
        pair: Trading pair (e.g. ``"BTC/USDT"``).
        lookback_hours: Number of hours to look back (default 24).

    Returns:
        A float between 0.0 and 100.0.
    """
    conn = get_db(db_path)
    try:
        cutoff = datetime.fromtimestamp(
            time.time() - lookback_hours * 3600, tz=timezone.utc
        ).isoformat()

        rows = conn.execute(
            """
            SELECT close FROM ohlcv_cache
            WHERE pair = ? AND timeframe = '1h' AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (pair, cutoff),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return 50.0

    closes = np.array([r["close"] for r in rows], dtype=np.float64)

    # Guard against zero / negative prices (shouldn't happen, but be safe)
    if np.any(closes <= 0):
        return 50.0

    log_returns = np.diff(np.log(closes))

    if len(log_returns) == 0:
        return 50.0

    std_lr = float(np.std(log_returns, ddof=1))

    # Annualise (8760 hours/year) and scale to 0-100
    annualised = std_lr * math.sqrt(8760)
    score = min(annualised * 100.0, 100.0)

    return round(score, 2)
