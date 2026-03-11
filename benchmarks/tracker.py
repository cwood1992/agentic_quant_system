"""Benchmark tracker for the agentic quant trading system.

Maintains counterfactual benchmarks (hodl, DCA, equal-weight rebalance)
so that strategy performance can be compared against passive alternatives.
"""

import json
from datetime import datetime, timezone

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("benchmarks.tracker")

# Default benchmarks seeded on first run
DEFAULT_BENCHMARKS = {
    "hodl_btc": {
        "type": "hodl",
        "asset": "BTC/USD",
        "seed_capital": 500.0,
    },
    "hodl_eth": {
        "type": "hodl",
        "asset": "ETH/USD",
        "seed_capital": 500.0,
    },
    "dca_btc": {
        "type": "dca",
        "asset": "BTC/USD",
        "seed_capital": 500.0,
        "total_weeks": 52,
    },
    "equal_weight_rebal": {
        "type": "equal_weight",
        "assets": ["BTC/USD", "ETH/USD"],
        "seed_capital": 500.0,
        "rebalance_interval_weeks": 1,
    },
}


class BenchmarkTracker:
    """Tracks counterfactual benchmark performance against the database.

    Args:
        db_path: Path to the SQLite database.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._seed_defaults()

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def _seed_defaults(self) -> None:
        """Seed default benchmarks into system_state if not already present."""
        conn = get_db(self.db_path)
        now = datetime.now(timezone.utc).isoformat()

        try:
            for bench_id, bench_cfg in DEFAULT_BENCHMARKS.items():
                key = f"benchmark_{bench_id}"
                existing = conn.execute(
                    "SELECT id FROM system_state WHERE key = ?", (key,)
                ).fetchone()

                if existing is None:
                    value = {
                        **bench_cfg,
                        "initial_price": None,
                        "current_value": bench_cfg["seed_capital"],
                        "history": [],
                    }
                    conn.execute(
                        "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
                        (key, json.dumps(value), now),
                    )
                    logger.info("Seeded benchmark: %s", bench_id)

            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_benchmark(self, benchmark_id: str) -> dict | None:
        """Load benchmark data from system_state."""
        conn = get_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?",
                (f"benchmark_{benchmark_id}",),
            ).fetchone()
            if row:
                return json.loads(row["value"])
            return None
        finally:
            conn.close()

    def _save_benchmark(self, benchmark_id: str, data: dict) -> None:
        """Persist benchmark data to system_state."""
        conn = get_db(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                "UPDATE system_state SET value = ?, updated_at = ? WHERE key = ?",
                (json.dumps(data), now, f"benchmark_{benchmark_id}"),
            )
            conn.commit()
        finally:
            conn.close()

    def _append_history(self, data: dict, value: float) -> None:
        """Append a timestamped value to the benchmark history list."""
        now = datetime.now(timezone.utc).isoformat()
        history = data.get("history", [])
        history.append({"timestamp": now, "value": value})
        # Keep last 1000 entries
        if len(history) > 1000:
            history = history[-1000:]
        data["history"] = history

    # ------------------------------------------------------------------
    # Update methods
    # ------------------------------------------------------------------

    def update_hodl(self, benchmark_id: str, current_price: float) -> dict | None:
        """Update a hodl benchmark with the current asset price.

        current_value = seed_capital * (current_price / initial_price)

        If initial_price is not yet set, it is recorded from current_price.

        Args:
            benchmark_id: Benchmark identifier (e.g. "hodl_btc").
            current_price: Current price of the benchmark asset.

        Returns:
            Updated benchmark dict, or None if benchmark not found.
        """
        data = self._get_benchmark(benchmark_id)
        if data is None:
            return None

        if data.get("initial_price") is None:
            data["initial_price"] = current_price

        seed_capital = data["seed_capital"]
        initial_price = data["initial_price"]
        current_value = seed_capital * (current_price / initial_price)
        data["current_value"] = current_value

        self._append_history(data, current_value)
        self._save_benchmark(benchmark_id, data)

        return data

    def update_dca(
        self, benchmark_id: str, current_price: float, elapsed_weeks: int
    ) -> dict | None:
        """Update a DCA benchmark simulating weekly purchases.

        Each week, (seed_capital / total_weeks) is used to buy at current_price.
        The total value is the sum of all weekly purchases valued at current_price.

        Args:
            benchmark_id: Benchmark identifier (e.g. "dca_btc").
            current_price: Current price of the asset.
            elapsed_weeks: Number of weeks elapsed since start.

        Returns:
            Updated benchmark dict, or None if benchmark not found.
        """
        data = self._get_benchmark(benchmark_id)
        if data is None:
            return None

        seed_capital = data["seed_capital"]
        total_weeks = data.get("total_weeks", 52)
        weeks_to_use = min(elapsed_weeks, total_weeks)

        weekly_amount = seed_capital / total_weeks

        # Track purchase history
        purchases = data.get("purchases", [])

        # Add new weekly purchases up to elapsed_weeks
        while len(purchases) < weeks_to_use:
            # Use current_price as a simplification for simulated weekly buys
            purchases.append({
                "week": len(purchases) + 1,
                "price": current_price,
                "amount_usd": weekly_amount,
                "units": weekly_amount / current_price,
            })

        data["purchases"] = purchases

        # Total units accumulated
        total_units = sum(p["units"] for p in purchases)
        # Unspent capital
        spent = weekly_amount * weeks_to_use
        unspent = seed_capital - spent

        current_value = (total_units * current_price) + unspent
        data["current_value"] = current_value

        self._append_history(data, current_value)
        self._save_benchmark(benchmark_id, data)

        return data

    def update_equal_weight(
        self, benchmark_id: str, btc_price: float, eth_price: float
    ) -> dict | None:
        """Update an equal-weight rebalance benchmark.

        Simulates a 50/50 split between BTC and ETH with weekly rebalancing.

        Args:
            benchmark_id: Benchmark identifier.
            btc_price: Current BTC price.
            eth_price: Current ETH price.

        Returns:
            Updated benchmark dict, or None if not found.
        """
        data = self._get_benchmark(benchmark_id)
        if data is None:
            return None

        if data.get("initial_price") is None:
            data["initial_price"] = {"BTC/USD": btc_price, "ETH/USD": eth_price}
            half = data["seed_capital"] / 2.0
            data["btc_units"] = half / btc_price
            data["eth_units"] = half / eth_price

        btc_value = data["btc_units"] * btc_price
        eth_value = data["eth_units"] * eth_price
        total_value = btc_value + eth_value

        # Rebalance to 50/50
        half = total_value / 2.0
        data["btc_units"] = half / btc_price
        data["eth_units"] = half / eth_price
        data["current_value"] = total_value

        self._append_history(data, total_value)
        self._save_benchmark(benchmark_id, data)

        return data

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_benchmark_performance(self, benchmark_id: str) -> dict | None:
        """Get performance metrics for a benchmark.

        Returns:
            Dict with return_24h, return_7d, return_30d, current_value,
            initial_value. None if benchmark not found.
        """
        data = self._get_benchmark(benchmark_id)
        if data is None:
            return None

        seed_capital = data["seed_capital"]
        current_value = data.get("current_value", seed_capital)
        history = data.get("history", [])

        def _return_from_n_ago(n_entries: int) -> float | None:
            """Compute return from n entries ago to now."""
            if len(history) < n_entries + 1:
                return None
            past_value = history[-(n_entries + 1)]["value"]
            if past_value == 0:
                return None
            return (current_value - past_value) / past_value

        return {
            "benchmark_id": benchmark_id,
            "current_value": current_value,
            "initial_value": seed_capital,
            "total_return": (
                (current_value - seed_capital) / seed_capital
                if seed_capital > 0
                else 0.0
            ),
            "return_24h": _return_from_n_ago(1),
            "return_7d": _return_from_n_ago(7),
            "return_30d": _return_from_n_ago(30),
        }

    # ------------------------------------------------------------------
    # Instruction processing
    # ------------------------------------------------------------------

    def process_benchmark_action(self, action: dict) -> dict:
        """Handle a benchmark instruction from an agent.

        Supported actions:
          - add: Add a new custom benchmark.
          - remove: Remove a benchmark.
          - modify: Update benchmark configuration.

        Args:
            action: Dict with 'action' key and relevant parameters.

        Returns:
            Result dict with 'success' (bool) and optional 'error'.
        """
        action_type = action.get("action")

        if action_type == "add":
            return self._add_benchmark(action)
        elif action_type == "remove":
            return self._remove_benchmark(action)
        elif action_type == "modify":
            return self._modify_benchmark(action)
        else:
            return {"success": False, "error": f"Unknown action: {action_type}"}

    def _add_benchmark(self, action: dict) -> dict:
        """Add a new custom benchmark."""
        bench_id = action.get("benchmark_id")
        if not bench_id:
            return {"success": False, "error": "Missing benchmark_id"}

        existing = self._get_benchmark(bench_id)
        if existing is not None:
            return {"success": False, "error": f"Benchmark {bench_id} already exists"}

        conn = get_db(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            value = {
                "type": action.get("type", "custom"),
                "seed_capital": action.get("seed_capital", 500.0),
                "initial_price": None,
                "current_value": action.get("seed_capital", 500.0),
                "history": [],
                "config": action.get("config", {}),
            }
            conn.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
                (f"benchmark_{bench_id}", json.dumps(value), now),
            )
            conn.commit()
            return {"success": True, "benchmark_id": bench_id}
        finally:
            conn.close()

    def _remove_benchmark(self, action: dict) -> dict:
        """Remove a benchmark."""
        bench_id = action.get("benchmark_id")
        if not bench_id:
            return {"success": False, "error": "Missing benchmark_id"}

        conn = get_db(self.db_path)
        try:
            result = conn.execute(
                "DELETE FROM system_state WHERE key = ?",
                (f"benchmark_{bench_id}",),
            )
            conn.commit()
            if result.rowcount > 0:
                return {"success": True, "benchmark_id": bench_id}
            return {"success": False, "error": f"Benchmark {bench_id} not found"}
        finally:
            conn.close()

    def _modify_benchmark(self, action: dict) -> dict:
        """Modify an existing benchmark's configuration."""
        bench_id = action.get("benchmark_id")
        if not bench_id:
            return {"success": False, "error": "Missing benchmark_id"}

        data = self._get_benchmark(bench_id)
        if data is None:
            return {"success": False, "error": f"Benchmark {bench_id} not found"}

        updates = action.get("updates", {})
        for k, v in updates.items():
            if k not in ("history", "current_value"):  # protect computed fields
                data[k] = v

        self._save_benchmark(bench_id, data)
        return {"success": True, "benchmark_id": bench_id}
