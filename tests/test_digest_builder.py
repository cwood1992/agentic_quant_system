"""Tests for the digest builder module."""

import json
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# Mock ccxt before importing modules that depend on it, to avoid
# environment-specific import errors in the ccxt package.
if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = MagicMock()

from database.schema import create_all_tables, get_db
from digest.builder import DigestBuilder


@pytest.fixture
def db(tmp_path):
    """Create a temporary database with all tables."""
    db_path = str(tmp_path / "test_digest.db")
    create_all_tables(db_path)
    return db_path


@pytest.fixture
def quant_config():
    """Return a quant agent config."""
    return {
        "role": "quant",
        "strategy_namespace": "quant_alpha",
        "capital_allocation_pct": 0.5,
        "monitored_pairs": ["BTC/USDT", "ETH/USDT"],
    }


@pytest.fixture
def pm_config():
    """Return a portfolio manager agent config."""
    return {
        "role": "portfolio_manager",
        "strategy_namespace": "pm",
        "capital_allocation_pct": 1.0,
        "monitored_pairs": ["BTC/USDT", "ETH/USDT"],
    }


class TestPerAgentScoping:
    """Test that digest sections are scoped to the correct agent."""

    def test_quant_sees_only_own_positions(self, db, quant_config):
        """Quant agent should only see its own positions in portfolio section."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        # Insert positions for two different agents
        conn.execute(
            """
            INSERT INTO trades
                (timestamp, agent_id, strategy_id, pair, action, size_usd,
                 price, order_type, fill_price, paper, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, "quant_alpha", "strat_a", "BTC/USDT", "buy", 500.0,
             50000.0, "market", 50000.0, 1, "filled"),
        )
        conn.execute(
            """
            INSERT INTO trades
                (timestamp, agent_id, strategy_id, pair, action, size_usd,
                 price, order_type, fill_price, paper, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, "quant_beta", "strat_b", "ETH/USDT", "buy", 300.0,
             2500.0, "market", 2500.0, 1, "filled"),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("quant_alpha", quant_config, db)
        portfolio = builder.build_portfolio_section("quant_alpha")

        assert "BTC/USDT" in portfolio
        assert "strat_a" in portfolio
        # Should NOT see the other agent's position
        assert "quant_beta" not in portfolio
        assert "strat_b" not in portfolio

    def test_pm_sees_all_positions(self, db, pm_config):
        """Portfolio manager should see positions from all agents."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO trades
                (timestamp, agent_id, strategy_id, pair, action, size_usd,
                 price, order_type, fill_price, paper, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, "quant_alpha", "strat_a", "BTC/USDT", "buy", 500.0,
             50000.0, "market", 50000.0, 1, "filled"),
        )
        conn.execute(
            """
            INSERT INTO trades
                (timestamp, agent_id, strategy_id, pair, action, size_usd,
                 price, order_type, fill_price, paper, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, "quant_beta", "strat_b", "ETH/USDT", "buy", 300.0,
             2500.0, "market", 2500.0, 1, "filled"),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("pm_agent", pm_config, db)
        portfolio = builder.build_portfolio_section("pm_agent")

        # PM should see both agents' positions
        assert "quant_alpha" in portfolio
        assert "quant_beta" in portfolio
        assert "BTC/USDT" in portfolio
        assert "ETH/USDT" in portfolio

    def test_quant_sees_own_strategies(self, db, quant_config):
        """Quant agent should only see strategies in its own namespace."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO strategy_registry
                (strategy_id, agent_id, namespace, stage, created_at, updated_at, config)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("strat_a", "quant_alpha", "quant_alpha", "live", now, now, "{}"),
        )
        conn.execute(
            """
            INSERT INTO strategy_registry
                (strategy_id, agent_id, namespace, stage, created_at, updated_at, config)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("strat_b", "quant_beta", "quant_beta", "live", now, now, "{}"),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("quant_alpha", quant_config, db)
        strategies = builder.build_strategy_sections("quant_alpha", "quant_alpha")

        assert "strat_a" in strategies
        assert "strat_b" not in strategies


class TestEmptySectionCollapsing:
    """Test that empty sections are collapsed to single-line headers."""

    def test_empty_portfolio_collapsed(self, db, quant_config):
        """Empty portfolio section should show (empty) marker."""
        builder = DigestBuilder("quant_alpha", quant_config, db)
        portfolio = builder.build_portfolio_section("quant_alpha")

        assert "(empty)" in portfolio
        assert "PORTFOLIO STATE" in portfolio

    def test_empty_agent_messages_collapsed(self, db, quant_config):
        """Empty agent messages section should show (empty) marker."""
        builder = DigestBuilder("quant_alpha", quant_config, db)
        messages = builder.build_agent_messages_section("quant_alpha")

        assert "(empty)" in messages
        assert "AGENT MESSAGES" in messages

    def test_empty_risk_gate_collapsed(self, db, quant_config):
        """Empty risk gate log should show (empty) marker."""
        builder = DigestBuilder("quant_alpha", quant_config, db)
        risk_log = builder.build_risk_gate_log_section("quant_alpha")

        assert "(empty)" in risk_log
        assert "RISK GATE LOG" in risk_log

    def test_collapse_method_with_content(self):
        """Non-empty content should not be collapsed."""
        result = DigestBuilder._collapse_if_empty("TEST", "some content here")
        assert "(empty)" not in result
        assert "TEST" in result
        assert "some content here" in result

    def test_collapse_method_with_whitespace_only(self):
        """Whitespace-only content should be collapsed."""
        result = DigestBuilder._collapse_if_empty("TEST", "   \n  ")
        assert "(empty)" in result


class TestAgentMessagesShown:
    """Test that agent messages appear correctly in the digest."""

    def test_agent_messages_shown(self, db, quant_config):
        """Unread messages addressed to the agent should appear."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO agent_messages
                (created_at, from_agent, to_agent, message_type, priority, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "pm_agent",
                "quant_alpha",
                "directive",
                "high",
                json.dumps({"content": "Reduce BTC exposure by 20%"}),
                "pending",
            ),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("quant_alpha", quant_config, db)
        messages = builder.build_agent_messages_section("quant_alpha")

        assert "(empty)" not in messages
        assert "pm_agent" in messages
        assert "Reduce BTC exposure by 20%" in messages
        assert "high" in messages

    def test_broadcast_messages_shown(self, db, quant_config):
        """Messages addressed to 'all' should appear for any agent."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO agent_messages
                (created_at, from_agent, to_agent, message_type, priority, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "system",
                "all",
                "announcement",
                "normal",
                json.dumps({"content": "System maintenance at midnight"}),
                "pending",
            ),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("quant_alpha", quant_config, db)
        messages = builder.build_agent_messages_section("quant_alpha")

        assert "System maintenance at midnight" in messages

    def test_read_messages_not_shown(self, db, quant_config):
        """Messages with status='read' should not appear."""
        conn = get_db(db)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            INSERT INTO agent_messages
                (created_at, from_agent, to_agent, message_type, priority, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "pm_agent",
                "quant_alpha",
                "directive",
                "normal",
                json.dumps({"content": "Old message"}),
                "read",
            ),
        )
        conn.commit()
        conn.close()

        builder = DigestBuilder("quant_alpha", quant_config, db)
        messages = builder.build_agent_messages_section("quant_alpha")

        assert "(empty)" in messages
        assert "Old message" not in messages
