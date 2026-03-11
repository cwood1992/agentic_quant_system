"""Shared pytest fixtures for the agentic quant trading system test suite."""

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from database.schema import create_all_tables, get_db


@pytest.fixture
def db(tmp_path):
    """Create a temporary SQLite database with all tables.

    Yields the path to the database file.  Cleans up automatically
    via tmp_path fixture.
    """
    db_path = str(tmp_path / "test.db")
    create_all_tables(db_path)
    yield db_path


@pytest.fixture
def mock_exchange():
    """Return a MagicMock ccxt exchange with configurable responses.

    Pre-configured with sensible defaults for fetch_ohlcv, fetch_ticker,
    fetch_balance, and create_order.
    """
    exchange = MagicMock()

    # fetch_ticker returns a realistic ticker
    exchange.fetch_ticker.return_value = {
        "symbol": "BTC/USD",
        "last": 50000.0,
        "bid": 49990.0,
        "ask": 50010.0,
        "high": 51000.0,
        "low": 49000.0,
        "volume": 1234.56,
        "timestamp": 1700000000000,
    }

    # fetch_balance returns a realistic balance
    exchange.fetch_balance.return_value = {
        "total": {"USD": 10000.0, "BTC": 0.1, "ETH": 2.0},
        "free": {"USD": 8000.0, "BTC": 0.05, "ETH": 1.5},
        "used": {"USD": 2000.0, "BTC": 0.05, "ETH": 0.5},
    }

    # fetch_ohlcv returns a list of candles
    exchange.fetch_ohlcv.return_value = [
        [1700000000000, 49000.0, 50500.0, 48500.0, 50000.0, 100.0],
        [1700003600000, 50000.0, 51000.0, 49500.0, 50500.0, 120.0],
        [1700007200000, 50500.0, 51500.0, 50000.0, 51000.0, 110.0],
    ]

    # create_order returns a realistic order result
    exchange.create_order.return_value = {
        "id": "order-123",
        "status": "closed",
        "filled": 0.01,
        "price": 50000.0,
        "cost": 500.0,
        "fee": {"cost": 0.5, "currency": "USD"},
    }

    return exchange


@pytest.fixture
def sample_config():
    """Return a complete configuration dict for testing."""
    return {
        "system": {
            "mode": "paper",
            "db_path": ":memory:",
            "log_dir": "logs",
            "data_dir": "data",
        },
        "exchange": {
            "api_key": "test-api-key",
            "api_secret": "test-api-secret",
            "sandbox": True,
        },
        "agents": {
            "quant_primary": {
                "enabled": True,
                "role": "quant",
                "brief_path": "briefs/BRIEF_QUANT.md",
                "model": "claude-sonnet-4-6",
                "cadence_hours": 4,
                "capital_allocation_pct": 0.5,
                "max_positions": 5,
                "tools": [
                    "get_ohlcv",
                    "get_positions",
                    "backtest",
                    "get_benchmark",
                ],
            },
            "quant_secondary": {
                "enabled": True,
                "role": "quant",
                "brief_path": "briefs/BRIEF_QUANT.md",
                "model": "claude-sonnet-4-6",
                "cadence_hours": 8,
                "capital_allocation_pct": 0.3,
                "max_positions": 3,
                "tools": ["get_ohlcv", "get_positions"],
            },
        },
        "executor": {
            "mode": "paper",
            "slippage_bps": 10,
            "fee_rate": 0.001,
        },
        "claude": {
            "api_key": "test-claude-key",
            "default_model": "claude-sonnet-4-6",
            "max_output_tokens": 8000,
            "monthly_budget_usd": 50,
        },
    }


@pytest.fixture
def sample_positions():
    """Return a realistic list of open position dicts."""
    return [
        {
            "pair": "BTC/USD",
            "strategy_id": "quant_primary_momentum",
            "agent_id": "quant_primary",
            "entry_price": 48000.0,
            "size_usd": 500.0,
            "current_price": 50000.0,
            "unrealized_pnl": 20.83,
        },
        {
            "pair": "ETH/USD",
            "strategy_id": "quant_primary_mean_rev",
            "agent_id": "quant_primary",
            "entry_price": 2400.0,
            "size_usd": 300.0,
            "current_price": 2500.0,
            "unrealized_pnl": 12.50,
        },
        {
            "pair": "SOL/USD",
            "strategy_id": "quant_secondary_trend",
            "agent_id": "quant_secondary",
            "entry_price": 100.0,
            "size_usd": 200.0,
            "current_price": 110.0,
            "unrealized_pnl": 20.00,
        },
    ]
