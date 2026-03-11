"""Backtest runner for strategy evaluation.

Loads OHLCV data from the database, runs a strategy chronologically,
simulates execution with slippage and fees, and computes performance
metrics. Auto-advances strategies through the lifecycle based on results.
"""

import json
from datetime import datetime, timezone, timedelta

import numpy as np

from database.schema import get_db
from logging_config import get_logger
from strategies.base import BaseStrategy, Signal

logger = get_logger("strategies.backtest_runner")

# Default simulation parameters
DEFAULT_SLIPPAGE_BPS = 10  # basis points
DEFAULT_FEE_RATE = 0.001  # 0.1%
MINIMUM_TRADE_COUNT = 8


class BacktestRunner:
    """Run backtests for strategy evaluation.

    Args:
        db_path: Path to the SQLite database file.
        slippage_bps: Slippage in basis points (default 10).
        fee_rate: Fee rate as a fraction (default 0.001).
    """

    def __init__(
        self,
        db_path: str,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        fee_rate: float = DEFAULT_FEE_RATE,
    ):
        self.db_path = db_path
        self.slippage_bps = slippage_bps
        self.fee_rate = fee_rate

    def run_backtest(
        self,
        strategy_class: type,
        hypothesis_config: dict,
        lookback_days: int = 90,
    ) -> dict:
        """Run a backtest for a strategy class with given config.

        Loads OHLCV data, instantiates the strategy, feeds data
        chronologically via on_data(), simulates fills with slippage
        and fees, then computes performance metrics.

        Args:
            strategy_class: A BaseStrategy subclass (not an instance).
            hypothesis_config: Dict with at minimum 'pair' and 'timeframe'.
            lookback_days: Days of historical data to use.

        Returns:
            Dict with: total_return, sharpe_ratio, max_drawdown, win_rate,
            trade_count, avg_trade_duration, trades, equity_curve,
            benchmark_return, success, failure_reason (if any).
        """
        pair = hypothesis_config.get("pair", "BTC/USDT")
        timeframe = hypothesis_config.get("timeframe", "1h")

        # Load OHLCV data
        candles = self._load_candles(pair, timeframe, lookback_days)
        if len(candles) < 10:
            return {
                "success": False,
                "failure_reason": f"Insufficient data: {len(candles)} candles",
                "trade_count": 0,
            }

        # Instantiate strategy
        if isinstance(strategy_class, type):
            strategy = strategy_class(**hypothesis_config.get("params", {}))
        else:
            strategy = strategy_class

        # Run simulation
        starting_capital = hypothesis_config.get("starting_capital", 10000.0)
        trades, equity_curve = self._simulate(
            strategy, candles, pair, starting_capital
        )

        trade_count = len(trades)
        if trade_count < MINIMUM_TRADE_COUNT:
            return {
                "success": False,
                "failure_reason": (
                    f"Too few trades: {trade_count} < {MINIMUM_TRADE_COUNT}"
                ),
                "trade_count": trade_count,
                "trades": trades,
                "equity_curve": equity_curve.tolist(),
            }

        # Compute metrics
        metrics = self._compute_metrics(
            trades, equity_curve, candles, starting_capital, timeframe
        )
        metrics["success"] = True
        metrics["trade_count"] = trade_count
        metrics["trades"] = trades
        metrics["equity_curve"] = equity_curve.tolist()

        return metrics

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_candles(
        self, pair: str, timeframe: str, lookback_days: int
    ) -> list[dict]:
        """Load OHLCV candles from the database.

        Returns:
            List of dicts with keys: timestamp, open, high, low, close, volume.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()

        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_cache
                WHERE pair = ? AND timeframe = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (pair, timeframe, cutoff),
            ).fetchall()
        finally:
            conn.close()

        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Simulation engine
    # ------------------------------------------------------------------

    def _simulate(
        self,
        strategy: BaseStrategy,
        candles: list[dict],
        pair: str,
        starting_capital: float,
    ) -> tuple[list[dict], np.ndarray]:
        """Simulate strategy execution over historical candles.

        Args:
            strategy: Instantiated strategy.
            candles: Chronological candle data.
            pair: Trading pair.
            starting_capital: Initial capital in USD.

        Returns:
            Tuple of (trades list, equity curve ndarray).
        """
        capital = starting_capital
        position = 0.0  # position size in base currency
        entry_price = 0.0
        entry_time = ""
        trades: list[dict] = []
        equity_values = [starting_capital]

        for i, candle in enumerate(candles):
            # Build data dict for strategy
            data = {
                "candle": candle,
                "pair": pair,
                "index": i,
                "candles_so_far": candles[: i + 1],
            }

            signals = strategy.on_data(data)

            for signal in signals:
                if not isinstance(signal, Signal):
                    continue

                price = candle["close"]

                if signal.action == "buy" and position == 0.0:
                    # Apply slippage (buy at higher price)
                    fill_price = price * (1 + self.slippage_bps / 10000)
                    size_usd = capital * signal.size_pct
                    fee = size_usd * self.fee_rate
                    position = (size_usd - fee) / fill_price
                    entry_price = fill_price
                    entry_time = candle["timestamp"]
                    capital -= size_usd

                elif signal.action == "sell" and position == 0.0:
                    # Short: simplified as negative position
                    fill_price = price * (1 - self.slippage_bps / 10000)
                    size_usd = abs(capital) * signal.size_pct
                    fee = size_usd * self.fee_rate
                    position = -(size_usd - fee) / fill_price
                    entry_price = fill_price
                    entry_time = candle["timestamp"]
                    capital -= size_usd

                elif signal.action == "close" and position != 0.0:
                    # Close position
                    if position > 0:
                        fill_price = price * (1 - self.slippage_bps / 10000)
                    else:
                        fill_price = price * (1 + self.slippage_bps / 10000)

                    close_value = abs(position) * fill_price
                    fee = close_value * self.fee_rate
                    pnl = close_value - fee - abs(position) * entry_price
                    if position < 0:
                        pnl = abs(position) * entry_price - close_value - fee

                    capital += close_value - fee
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": candle["timestamp"],
                        "entry_price": entry_price,
                        "exit_price": fill_price,
                        "side": "long" if position > 0 else "short",
                        "pnl": pnl,
                        "return_pct": pnl / (abs(position) * entry_price)
                        if entry_price > 0
                        else 0.0,
                    })
                    position = 0.0
                    entry_price = 0.0

            # Mark-to-market equity
            if position != 0.0:
                mtm = abs(position) * candle["close"]
                if position < 0:
                    mtm = abs(position) * (2 * entry_price - candle["close"])
                equity_values.append(capital + mtm)
            else:
                equity_values.append(capital)

        return trades, np.array(equity_values, dtype=np.float64)

    # ------------------------------------------------------------------
    # Metrics computation
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        trades: list[dict],
        equity_curve: np.ndarray,
        candles: list[dict],
        starting_capital: float,
        timeframe: str,
    ) -> dict:
        """Compute backtest performance metrics.

        Returns:
            Dict with total_return, sharpe_ratio, max_drawdown, win_rate,
            avg_trade_duration, benchmark_return.
        """
        final_equity = equity_curve[-1]
        total_return = (final_equity - starting_capital) / starting_capital

        # Sharpe ratio from equity curve returns
        if len(equity_curve) > 1:
            eq_returns = np.diff(equity_curve) / equity_curve[:-1]
            if np.std(eq_returns) > 0:
                annualisation = self._annualisation_factor(timeframe)
                sharpe = float(
                    np.mean(eq_returns)
                    / np.std(eq_returns, ddof=1)
                    * np.sqrt(annualisation)
                )
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Max drawdown
        max_dd = float(self._max_drawdown(equity_curve))

        # Win rate
        if trades:
            wins = sum(1 for t in trades if t["pnl"] > 0)
            win_rate = wins / len(trades)
        else:
            win_rate = 0.0

        # Average trade duration (placeholder -- uses index difference)
        avg_duration = 0.0
        if trades:
            durations = []
            for t in trades:
                try:
                    entry_dt = datetime.fromisoformat(t["entry_time"])
                    exit_dt = datetime.fromisoformat(t["exit_time"])
                    durations.append((exit_dt - entry_dt).total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass
            if durations:
                avg_duration = sum(durations) / len(durations)

        # Counterfactual benchmark: buy-and-hold
        if candles:
            first_close = candles[0]["close"]
            last_close = candles[-1]["close"]
            benchmark_return = (last_close - first_close) / first_close
        else:
            benchmark_return = 0.0

        return {
            "total_return": round(total_return, 6),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown": round(max_dd, 6),
            "win_rate": round(win_rate, 4),
            "avg_trade_duration_hours": round(avg_duration, 2),
            "benchmark_return": round(benchmark_return, 6),
            "starting_capital": starting_capital,
            "final_equity": round(final_equity, 2),
        }

    @staticmethod
    def _max_drawdown(equity_curve: np.ndarray) -> float:
        """Compute maximum drawdown from an equity curve.

        Args:
            equity_curve: Array of equity values over time.

        Returns:
            Maximum drawdown as a positive fraction (0.0 to 1.0).
        """
        if len(equity_curve) < 2:
            return 0.0

        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve[1:]:
            if val > peak:
                peak = val
            dd = (peak - val) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return max_dd

    @staticmethod
    def _annualisation_factor(timeframe: str) -> float:
        """Return periods per year for a given timeframe."""
        mapping = {
            "1m": 525960,
            "5m": 105192,
            "15m": 35064,
            "1h": 8760,
            "4h": 2190,
            "1d": 365,
            "1w": 52,
        }
        return mapping.get(timeframe, 8760)
