"""Tests for the paper executor (executor/paper.py)."""

import json

from database.schema import get_db
from executor.paper import PaperExecutor
from risk.limits import MINIMUM_ORDER_USD
from strategies.base import Signal


class TestPaperExecutor:
    """Tests for PaperExecutor."""

    def _seed_price(self, db_path, pair, price):
        """Insert a price into ohlcv_cache so the executor can look it up."""
        conn = get_db(db_path)
        conn.execute(
            """INSERT INTO ohlcv_cache (pair, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, '1h', '2026-01-01T00:00:00', ?, ?, ?, ?, 100.0)""",
            (pair, price, price, price, price),
        )
        conn.commit()
        conn.close()

    def test_rejects_below_minimum_order(self, db):
        """Orders below $5 minimum are rejected; orders at $5 are accepted."""
        executor = PaperExecutor(db_path=db, config={})
        self._seed_price(db, "BTC/USD", 50000.0)

        # $4 order should be rejected (size_pct * capital < $5)
        small_signal = Signal(
            action="buy",
            pair="BTC/USD",
            size_pct=0.004,  # 0.4% of $1000 = $4
            order_type="market",
        )
        result = executor.execute_signal(
            signal=small_signal,
            agent_id="quant_primary",
            strategy_id="test_strat",
            agent_capital=1000.0,
        )
        assert result["status"] == "rejected"
        assert result["reason"] == "below_minimum_order"

        # $5 order should be accepted
        ok_signal = Signal(
            action="buy",
            pair="BTC/USD",
            size_pct=0.005,  # 0.5% of $1000 = $5
            order_type="market",
        )
        result = executor.execute_signal(
            signal=ok_signal,
            agent_id="quant_primary",
            strategy_id="test_strat",
            agent_capital=1000.0,
        )
        assert result["status"] == "filled"

    def test_slippage_applied_correctly(self, db):
        """Slippage moves the fill price adversely for the trader."""
        slippage = 0.002  # 0.2%
        executor = PaperExecutor(db_path=db, config={}, slippage=slippage)
        price = 50000.0
        self._seed_price(db, "BTC/USD", price)

        # Buy order: fill price should be higher than market
        buy_signal = Signal(
            action="buy",
            pair="BTC/USD",
            size_pct=0.1,
            order_type="market",
        )
        result = executor.execute_signal(
            signal=buy_signal,
            agent_id="quant_primary",
            strategy_id="test_strat",
            agent_capital=1000.0,
        )
        assert result["status"] == "filled"
        expected_buy_fill = price * (1 + slippage)
        assert abs(result["fill_price"] - expected_buy_fill) < 0.01

        # For sell: fill price should be lower than market
        # Insert a higher price first so we can close
        self._seed_price(db, "ETH/USD", 3000.0)
        sell_signal = Signal(
            action="sell",
            pair="ETH/USD",
            size_pct=0.1,
            order_type="market",
        )
        result = executor.execute_signal(
            signal=sell_signal,
            agent_id="quant_primary",
            strategy_id="test_strat",
            agent_capital=1000.0,
        )
        assert result["status"] == "filled"
        expected_sell_fill = 3000.0 * (1 - slippage)
        assert abs(result["fill_price"] - expected_sell_fill) < 0.01

    def test_paper_flag_set(self, db):
        """Paper trades are recorded with paper=1 in the database."""
        executor = PaperExecutor(db_path=db, config={})
        self._seed_price(db, "BTC/USD", 50000.0)

        signal = Signal(
            action="buy",
            pair="BTC/USD",
            size_pct=0.1,
            order_type="market",
        )
        executor.execute_signal(
            signal=signal,
            agent_id="quant_primary",
            strategy_id="test_strat",
            agent_capital=1000.0,
        )

        conn = get_db(db)
        row = conn.execute(
            "SELECT paper FROM trades WHERE agent_id = 'quant_primary' LIMIT 1"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["paper"] == 1
