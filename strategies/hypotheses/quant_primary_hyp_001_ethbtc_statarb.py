# Strategy: quant_primary_hyp_001_ethbtc_statarb
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Statistical Arbitrage via Cointegration
hypothesis_id: quant_primary_hyp_001_ethbtc_statarb

Strategy: ETH and BTC are cointegrated. Trade the spread when it deviates
significantly from its rolling mean. Hedge ratio ~0.0484 (ETH price = 0.0484 * BTC price + intercept).
Half-life ~11 x 4h periods (~1.85 days). Entry at 1.5 std, exit at 0.5 std.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcStatArb(BaseStrategy):

    def __init__(self):
        # Hedge ratio from cointegration regression (ETH_price ~ hedge_ratio * BTC_price)
        self.hedge_ratio = 0.0484
        self.intercept = -1303.39
        # Rolling window for spread mean/std: ~3x half-life = 33 periods
        self.lookback = 33
        # Entry threshold: 1.5 standard deviations
        self.entry_z = 1.5
        # Exit threshold: 0.5 standard deviations (mean reversion partial)
        self.exit_z = 0.5
        # Position state
        self.position = None  # None, 'long_spread', 'short_spread'
        self.size_per_leg = 0.35  # 35% of capital per leg (70% total)

    def name(self) -> str:
        return "quant_primary_hyp_001_ethbtc_statarb"

    def required_feeds(self) -> list:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_spread(self, eth_prices: pd.Series, btc_prices: pd.Series) -> pd.Series:
        """Spread = ETH_price - (hedge_ratio * BTC_price + intercept)"""
        return eth_prices - (self.hedge_ratio * btc_prices + self.intercept)

    def on_data(self, data: dict) -> list:
        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return []

        if len(eth_df) < self.lookback + 2 or len(btc_df) < self.lookback + 2:
            return []

        # Align on common timestamps
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]

        # Compute spread series
        spread = self._compute_spread(eth_close, btc_close)

        # Rolling z-score over lookback window
        roll_mean = spread.rolling(self.lookback).mean()
        roll_std = spread.rolling(self.lookback).std()

        if roll_std.iloc[-1] == 0 or pd.isna(roll_std.iloc[-1]):
            return []

        z_score = (spread.iloc[-1] - roll_mean.iloc[-1]) / roll_std.iloc[-1]

        signals = []

        if self.position is None:
            # Spread too high: ETH expensive relative to BTC → short ETH, long BTC (short spread)
            if z_score > self.entry_z:
                signals.append(Signal(
                    action="sell",
                    pair="ETH/USD",
                    size_pct=self.size_per_leg,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} > {self.entry_z} — shorting ETH leg"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.size_per_leg,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} > {self.entry_z} — longing BTC leg"
                ))
                self.position = "short_spread"

            # Spread too low: ETH cheap relative to BTC → long ETH, short BTC (long spread)
            elif z_score < -self.entry_z:
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.size_per_leg,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} < -{self.entry_z} — longing ETH leg"
                ))
                signals.append(Signal(
                    action="sell",
                    pair="BTC/USD",
                    size_pct=self.size_per_leg,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} < -{self.entry_z} — shorting BTC leg"
                ))
                self.position = "long_spread"

        elif self.position == "short_spread":
            # Exit when spread reverts toward mean
            if z_score < self.exit_z:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} — closing short ETH leg"
                ))
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} — closing long BTC leg"
                ))
                self.position = None

        elif self.position == "long_spread":
            # Exit when spread reverts toward mean
            if z_score > -self.exit_z:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} — closing long ETH leg"
                ))
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z_score:.2f} — closing short BTC leg"
                ))
                self.position = None

        return signals

    def on_fill(self, fill: dict) -> None:
        pass

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {"position": self.position or "flat"}
