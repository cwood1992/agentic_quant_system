"""Robustness testing for strategy validation.

Implements random-entry tests and return-permutation tests as described
in BUILD.md. A strategy must pass both tests before advancing from
backtest to paper trading.
"""

import numpy as np

from risk.limits import ROBUSTNESS_N_RUNS, ROBUSTNESS_RANDOM_SEED


def compute_equity_curve(
    trade_returns: list[float] | np.ndarray, starting_capital: float
) -> np.ndarray:
    """Compute an equity curve from a sequence of trade returns.

    Args:
        trade_returns: Fractional returns per trade (e.g. 0.02 = +2%).
        starting_capital: Initial capital in USD.

    Returns:
        Numpy array of equity values. Length = len(trade_returns) + 1,
        starting with starting_capital.
    """
    returns = np.asarray(trade_returns, dtype=np.float64)
    equity = np.empty(len(returns) + 1, dtype=np.float64)
    equity[0] = starting_capital
    for i, r in enumerate(returns):
        equity[i + 1] = equity[i] * (1 + r)
    return equity


def max_drawdown(equity_curve: np.ndarray) -> float:
    """Compute maximum drawdown from an equity curve.

    Args:
        equity_curve: Array of equity values over time.

    Returns:
        Maximum drawdown as a positive fraction (0.0 to 1.0).
        Returns 0.0 for curves with fewer than 2 points.
    """
    if len(equity_curve) < 2:
        return 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve[1:]:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd

    return float(max_dd)


def random_entry_test(
    strategy_class: type,
    data: list[dict],
    original_trades: list[dict],
    n_runs: int = ROBUSTNESS_N_RUNS,
    seed: int = ROBUSTNESS_RANDOM_SEED,
) -> dict:
    """Random entry test: compare strategy to random entries.

    Generates random entry points at the same frequency as the original
    strategy, holds for the same average duration, uses the same position
    sizing, and computes returns. Compares the original strategy's
    performance against the distribution of random runs.

    Args:
        strategy_class: The strategy class (unused in simplified version,
            kept for interface compatibility).
        data: List of candle dicts with at minimum 'close' prices.
        original_trades: List of trade dicts from the backtest, each with
            'return_pct', 'entry_time', 'exit_time'.
        n_runs: Number of random simulations to run.
        seed: Random seed for reproducibility.

    Returns:
        Dict with: sharpe_percentile, total_return_percentile,
        mean_random_sharpe, n_runs, original_sharpe, original_return.
    """
    if not original_trades or not data:
        return {
            "sharpe_percentile": 0.0,
            "total_return_percentile": 0.0,
            "mean_random_sharpe": 0.0,
            "n_runs": 0,
            "error": "No trades or data provided",
        }

    rng = np.random.RandomState(seed)
    closes = np.array([c["close"] for c in data], dtype=np.float64)
    n_candles = len(closes)

    # Original strategy metrics
    orig_returns = [t["return_pct"] for t in original_trades]
    orig_total_return = float(np.prod([1 + r for r in orig_returns]) - 1)
    orig_sharpe = _sharpe_from_returns(orig_returns)

    # Estimate average holding period (in candle indices)
    n_trades = len(original_trades)
    avg_hold = max(1, n_candles // max(n_trades, 1))

    # Run random simulations
    random_sharpes = np.empty(n_runs, dtype=np.float64)
    random_returns = np.empty(n_runs, dtype=np.float64)

    for run in range(n_runs):
        # Generate random entry points
        entry_indices = sorted(
            rng.choice(
                max(1, n_candles - avg_hold),
                size=min(n_trades, max(1, n_candles - avg_hold)),
                replace=False,
            )
        )

        run_trade_returns = []
        for entry_idx in entry_indices:
            exit_idx = min(entry_idx + avg_hold, n_candles - 1)
            entry_price = closes[entry_idx]
            exit_price = closes[exit_idx]
            if entry_price > 0:
                ret = (exit_price - entry_price) / entry_price
                run_trade_returns.append(ret)

        if run_trade_returns:
            random_sharpes[run] = _sharpe_from_returns(run_trade_returns)
            random_returns[run] = float(
                np.prod([1 + r for r in run_trade_returns]) - 1
            )
        else:
            random_sharpes[run] = 0.0
            random_returns[run] = 0.0

    # Compute percentiles
    sharpe_percentile = float(np.mean(random_sharpes <= orig_sharpe) * 100)
    return_percentile = float(np.mean(random_returns <= orig_total_return) * 100)

    return {
        "sharpe_percentile": round(sharpe_percentile, 2),
        "total_return_percentile": round(return_percentile, 2),
        "mean_random_sharpe": round(float(np.mean(random_sharpes)), 4),
        "original_sharpe": round(orig_sharpe, 4),
        "original_return": round(orig_total_return, 6),
        "n_runs": n_runs,
    }


def return_permutation_test(
    trade_returns: list[float] | np.ndarray,
    starting_capital: float,
    n_runs: int = ROBUSTNESS_N_RUNS,
    seed: int = ROBUSTNESS_RANDOM_SEED,
) -> dict:
    """Return permutation test: shuffle trade returns and recompute metrics.

    Tests whether the ordering of trades matters by shuffling the sequence
    of trade returns and recomputing equity curves. Evaluates whether the
    original ordering produces better drawdown characteristics than random
    orderings.

    Args:
        trade_returns: Fractional returns per trade.
        starting_capital: Initial capital in USD.
        n_runs: Number of permutation runs.
        seed: Random seed for reproducibility.

    Returns:
        Dict with: final_equity_percentile, drawdown_resilience_percentile,
        n_runs.
    """
    returns = np.asarray(trade_returns, dtype=np.float64)

    if len(returns) == 0:
        return {
            "final_equity_percentile": 0.0,
            "drawdown_resilience_percentile": 0.0,
            "n_runs": 0,
            "error": "No trade returns provided",
        }

    rng = np.random.RandomState(seed)

    # Original metrics
    orig_curve = compute_equity_curve(returns, starting_capital)
    orig_final = orig_curve[-1]
    orig_dd = max_drawdown(orig_curve)

    # Run permutations
    perm_finals = np.empty(n_runs, dtype=np.float64)
    perm_drawdowns = np.empty(n_runs, dtype=np.float64)

    for run in range(n_runs):
        shuffled = rng.permutation(returns)
        curve = compute_equity_curve(shuffled, starting_capital)
        perm_finals[run] = curve[-1]
        perm_drawdowns[run] = max_drawdown(curve)

    # Percentiles
    final_equity_percentile = float(np.mean(perm_finals <= orig_final) * 100)
    # For drawdown resilience: lower drawdown is better, so we compare
    # how many permutations have WORSE (higher) drawdown
    drawdown_resilience_percentile = float(
        np.mean(perm_drawdowns >= orig_dd) * 100
    )

    return {
        "final_equity_percentile": round(final_equity_percentile, 2),
        "drawdown_resilience_percentile": round(drawdown_resilience_percentile, 2),
        "original_final_equity": round(float(orig_final), 2),
        "original_max_drawdown": round(float(orig_dd), 6),
        "mean_permuted_final": round(float(np.mean(perm_finals)), 2),
        "mean_permuted_drawdown": round(float(np.mean(perm_drawdowns)), 6),
        "n_runs": n_runs,
    }


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _sharpe_from_returns(returns: list[float]) -> float:
    """Compute Sharpe ratio from a list of trade returns (no annualisation)."""
    if not returns:
        return 0.0
    arr = np.array(returns, dtype=np.float64)
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    if std == 0:
        return 0.0
    return float(np.mean(arr) / std)
