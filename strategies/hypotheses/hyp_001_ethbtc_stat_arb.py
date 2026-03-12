# Strategy: hyp_001_ethbtc_stat_arb
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Statistical Arbitrage — Mean Reversion on Cointegrated Spread
Hypothesis: hyp_001_ethbtc_stat_arb

Trades the spread between ETH/USD and BTC/USD using a dynamically estimated
hedge ratio. Enters long ETH / short BTC when ETH is cheap relative to BTC
(spread z-score < -1.5), and short ETH / long BTC when ETH is expensive
(z-score > 1.5). Exits near equilibrium or on stop.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcStatArb(BaseStrategy):

    # ── tunable parameters ────────────────────────────────────────────────────
    LOOKBACK_PERIODS  = 30      # rolling window for hedge ratio + z-score (~5 days at 4h)
    ENTRY_Z           = 1.5     # |z| threshold to enter
    EXIT_Z            = 0.3     # |z| threshold to exit (near equilibrium)
    STOP_Z            = 3.0     # |z| stop-loss — spread diverging, potential regime break
    LEG_SIZE          = 0.40    # fraction of capital per leg (total ~80% gross exposure)
    MIN_PERIODS       = 20      # minimum observations before trading

    def __init__(self):
        self._position = None   # None | 'long_eth' | 'long_btc'
        self._entry_z  = None

    # ── interface ─────────────────────────────────────────────────────────────

    def name(self) -> str:
        return "hyp_001_ethbtc_stat_arb"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        eth_df = data.get("ETH/USD:4h") or data.get("ETH/USD")
        btc_df = data.get("BTC/USD:4h") or data.get("BTC/USD")

        if eth_df is None or btc_df is None:
            return []

        if len(eth_df) < self.MIN_PERIODS or len(btc_df) < self.MIN_PERIODS:
            return []

        # align on index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]
        df = pd.DataFrame({"eth": eth_close, "btc": btc_close}).dropna()

        if len(df) < self.MIN_PERIODS:
            return []

        # rolling OLS hedge ratio: regress ETH on BTC over lookback window
        window = df.tail(self.LOOKBACK_PERIODS)
        if len(window) < self.MIN_PERIODS:
            return []

        x = window["btc"].to_numpy()
        y = window["eth"].to_numpy()
        # OLS: y = hedge * x + intercept
        x_mean, y_mean = x.mean(), y.mean()
        hedge_ratio = np.dot(x - x_mean, y - y_mean) / np.dot(x - x_mean, x - x_mean)
        intercept   = y_mean - hedge_ratio * x_mean

        # spread series over the lookback window
        spread = window["eth"] - hedge_ratio * window["btc"] - intercept
        spread_mean = spread.mean()
        spread_std  = spread.std()

        if spread_std < 1e-8:
            return []

        current_spread = float(df["eth"].iloc[-1]) - hedge_ratio * float(df["btc"].iloc[-1]) - intercept
        z = (current_spread - spread_mean) / spread_std

        signals = []

        if self._position is None:
            # ── Entry logic ──────────────────────────────────────────────────
            if z < -self.ENTRY_Z:
                # ETH cheap relative to BTC → long ETH, short BTC
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"z={z:.2f} < -{self.ENTRY_Z}: ETH cheap vs BTC. hedge={hedge_ratio:.4f}"
                ))
                signals.append(Signal(
                    action="sell",
                    pair="BTC/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"z={z:.2f}: short BTC leg of stat arb"
                ))
                self._position = "long_eth"
                self._entry_z  = z

            elif z > self.ENTRY_Z:
                # ETH expensive relative to BTC → short ETH, long BTC
                signals.append(Signal(
                    action="sell",
                    pair="ETH/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"z={z:.2f} > {self.ENTRY_Z}: ETH expensive vs BTC. hedge={hedge_ratio:.4f}"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"z={z:.2f}: long BTC leg of stat arb"
                ))
                self._position = "long_btc"
                self._entry_z  = z

        else:
            # ── Exit logic ───────────────────────────────────────────────────
            should_exit = False
            exit_reason = ""

            if abs(z) < self.EXIT_Z:
                should_exit = True
                exit_reason = f"z={z:.2f} near equilibrium (target {self.EXIT_Z})"
            elif abs(z) > self.STOP_Z:
                should_exit = True
                exit_reason = f"z={z:.2f} stop-loss triggered (>{self.STOP_Z}): spread diverging"

            if should_exit:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
                self._position = None
                self._entry_z  = None

        return signals

    def on_fill(self, fill: dict) -> None:
        pass

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "position":  self._position,
            "entry_z":   self._entry_z,
        }
