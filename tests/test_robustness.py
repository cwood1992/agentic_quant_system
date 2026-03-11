"""Tests for the robustness testing module."""

import numpy as np
import pytest

from strategies.robustness import (
    compute_equity_curve,
    max_drawdown,
    random_entry_test,
    return_permutation_test,
)


class TestComputeEquityCurve:
    """Tests for compute_equity_curve."""

    def test_equity_curve_computation(self):
        """Equity curve should compound returns starting from initial capital."""
        returns = [0.10, -0.05, 0.03]
        capital = 10000.0
        curve = compute_equity_curve(returns, capital)

        assert len(curve) == 4
        assert curve[0] == capital
        assert curve[1] == pytest.approx(11000.0)  # +10%
        assert curve[2] == pytest.approx(10450.0)  # -5%
        assert curve[3] == pytest.approx(10763.5)  # +3%

    def test_equity_curve_empty_returns(self):
        """Empty returns should produce a single-element curve."""
        curve = compute_equity_curve([], 5000.0)
        assert len(curve) == 1
        assert curve[0] == 5000.0

    def test_equity_curve_all_positive(self):
        """All positive returns should produce a monotonically increasing curve."""
        returns = [0.01, 0.02, 0.03, 0.01]
        curve = compute_equity_curve(returns, 1000.0)
        for i in range(1, len(curve)):
            assert curve[i] > curve[i - 1]

    def test_equity_curve_accepts_numpy_array(self):
        """Should accept numpy arrays as input."""
        returns = np.array([0.05, -0.02])
        curve = compute_equity_curve(returns, 10000.0)
        assert len(curve) == 3
        assert curve[1] == pytest.approx(10500.0)


class TestMaxDrawdown:
    """Tests for max_drawdown."""

    def test_max_drawdown_computation(self):
        """Should correctly compute max drawdown from an equity curve."""
        # Peak at 12000, trough at 9000 => drawdown = 3000/12000 = 0.25
        curve = np.array([10000.0, 12000.0, 9000.0, 11000.0])
        dd = max_drawdown(curve)
        assert dd == pytest.approx(0.25)

    def test_max_drawdown_no_drawdown(self):
        """Monotonically increasing curve should have zero drawdown."""
        curve = np.array([100.0, 200.0, 300.0, 400.0])
        dd = max_drawdown(curve)
        assert dd == 0.0

    def test_max_drawdown_single_point(self):
        """Single point curve should have zero drawdown."""
        curve = np.array([100.0])
        dd = max_drawdown(curve)
        assert dd == 0.0

    def test_max_drawdown_empty(self):
        """Empty curve should have zero drawdown."""
        curve = np.array([])
        dd = max_drawdown(curve)
        assert dd == 0.0

    def test_max_drawdown_complete_loss(self):
        """Total loss should give drawdown of 1.0."""
        curve = np.array([10000.0, 5000.0, 0.0])
        dd = max_drawdown(curve)
        assert dd == pytest.approx(1.0)


class TestRandomEntryTest:
    """Tests for random_entry_test."""

    @pytest.fixture
    def sample_data(self):
        """Generate sample candle data with a clear uptrend."""
        np.random.seed(42)
        n = 200
        prices = 100.0 + np.cumsum(np.random.randn(n) * 0.5 + 0.1)
        return [{"close": float(p)} for p in prices]

    @pytest.fixture
    def sample_trades(self):
        """Generate sample trades with positive returns."""
        return [
            {"return_pct": 0.05, "entry_time": "2024-01-01", "exit_time": "2024-01-02"},
            {"return_pct": 0.03, "entry_time": "2024-01-03", "exit_time": "2024-01-04"},
            {"return_pct": -0.02, "entry_time": "2024-01-05", "exit_time": "2024-01-06"},
            {"return_pct": 0.04, "entry_time": "2024-01-07", "exit_time": "2024-01-08"},
            {"return_pct": 0.01, "entry_time": "2024-01-09", "exit_time": "2024-01-10"},
            {"return_pct": 0.06, "entry_time": "2024-01-11", "exit_time": "2024-01-12"},
            {"return_pct": -0.01, "entry_time": "2024-01-13", "exit_time": "2024-01-14"},
            {"return_pct": 0.03, "entry_time": "2024-01-15", "exit_time": "2024-01-16"},
            {"return_pct": 0.02, "entry_time": "2024-01-17", "exit_time": "2024-01-18"},
            {"return_pct": 0.04, "entry_time": "2024-01-19", "exit_time": "2024-01-20"},
        ]

    def test_random_entry_produces_valid_percentiles(
        self, sample_data, sample_trades
    ):
        """Percentiles should be between 0 and 100."""
        result = random_entry_test(
            strategy_class=None,
            data=sample_data,
            original_trades=sample_trades,
            n_runs=100,
            seed=42,
        )

        assert 0.0 <= result["sharpe_percentile"] <= 100.0
        assert 0.0 <= result["total_return_percentile"] <= 100.0
        assert result["n_runs"] == 100

    def test_random_entry_reproducible_with_seed(
        self, sample_data, sample_trades
    ):
        """Same seed should produce identical results."""
        r1 = random_entry_test(
            strategy_class=None,
            data=sample_data,
            original_trades=sample_trades,
            n_runs=50,
            seed=123,
        )
        r2 = random_entry_test(
            strategy_class=None,
            data=sample_data,
            original_trades=sample_trades,
            n_runs=50,
            seed=123,
        )

        assert r1["sharpe_percentile"] == r2["sharpe_percentile"]
        assert r1["total_return_percentile"] == r2["total_return_percentile"]
        assert r1["mean_random_sharpe"] == r2["mean_random_sharpe"]

    def test_random_entry_empty_trades(self, sample_data):
        """Empty trades should return error result."""
        result = random_entry_test(
            strategy_class=None,
            data=sample_data,
            original_trades=[],
            n_runs=10,
        )
        assert "error" in result

    def test_random_entry_empty_data(self, sample_trades):
        """Empty data should return error result."""
        result = random_entry_test(
            strategy_class=None,
            data=[],
            original_trades=sample_trades,
            n_runs=10,
        )
        assert "error" in result


class TestReturnPermutationTest:
    """Tests for return_permutation_test."""

    def test_return_permutation_produces_valid_percentiles(self):
        """Percentiles should be between 0 and 100."""
        returns = [0.05, -0.02, 0.03, 0.01, -0.01, 0.04, 0.02, -0.03, 0.06, 0.01]
        result = return_permutation_test(
            trade_returns=returns,
            starting_capital=10000.0,
            n_runs=100,
            seed=42,
        )

        assert 0.0 <= result["final_equity_percentile"] <= 100.0
        assert 0.0 <= result["drawdown_resilience_percentile"] <= 100.0
        assert result["n_runs"] == 100

    def test_return_permutation_reproducible_with_seed(self):
        """Same seed should produce identical results."""
        returns = [0.05, -0.02, 0.03, -0.01, 0.04]
        r1 = return_permutation_test(returns, 10000.0, n_runs=50, seed=99)
        r2 = return_permutation_test(returns, 10000.0, n_runs=50, seed=99)

        assert r1["final_equity_percentile"] == r2["final_equity_percentile"]
        assert r1["drawdown_resilience_percentile"] == r2["drawdown_resilience_percentile"]

    def test_return_permutation_empty_returns(self):
        """Empty returns should return error result."""
        result = return_permutation_test([], 10000.0, n_runs=10)
        assert "error" in result
        assert result["n_runs"] == 0

    def test_return_permutation_single_return(self):
        """Single trade return should work without error."""
        result = return_permutation_test([0.05], 10000.0, n_runs=50, seed=42)
        # With a single return, all permutations are identical
        assert result["n_runs"] == 50
        assert result["final_equity_percentile"] >= 0.0
