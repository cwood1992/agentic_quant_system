# Strategy: quant_primary_hyp_003_ethbtc_rotation
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Long-Only Rotation Strategy (hyp_003)
Entry: z-score of ETH/USD vs BTC/USD cointegration spread
  z > +2.0: ETH overvalued vs BTC → hold BTC
  z < -2.0: BTC overvalued vs ETH → hold ETH
  |z| < 0.5: neutral band → hold current position (no churn)

Half-life ~41h (10.29 x 4h periods). Mean reversion expected within 2-4 days.
Long-only: always 100% in either BTC or ETH or cash (transitional).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.base import BaseStrategy, Signal
import pandas as pd
import numpy as np


class ETHBTCRotationStrategy(BaseStrategy):
    """
    Long-only ETH/BTC rotation based on cointegration spread z-score.
    When ETH is expensive relative to BTC (z > +2), hold BTC.
    When BTC is expensive relative to ETH (z < -2), hold ETH.
    Neutral band (|z| < 0.5) preserves current position.
    """

    HEDGE_RATIO = 0.0351       # ETH/BTC hedge ratio (from 90d cointegration)
    INTERCEPT = -1297.411      # Cointegration regression intercept
    ENTRY_THRESHOLD = 2.0      # z-score magnitude to trigger rotation
    EXIT_BAND = 0.5            # neutral band — no action if |z| < this
    LOOKBACK = 120             # periods for rolling mean/std of spread (120 x 4h = 20 days)
    MIN_PERIODS = 60           # minimum periods before trading

    def name(self) -> str:
        return "quant_primary_hyp_003_ethbtc_rotation"

    def required_feeds(self) -> list:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def on_data(self, data: dict) -> list:
        eth_df = data.get("ETH/USD:4h") or data.get("ETH/USD")
        btc_df = data.get("BTC/USD:4h") or data.get("BTC/USD")

        if eth_df is None or btc_df is None:
            return []

        if len(eth_df) < self.MIN_PERIODS or len(btc_df) < self.MIN_PERIODS:
            return []

        # Align by index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]

        # Compute cointegration spread
        spread = eth_close - self.HEDGE_RATIO * btc_close - self.INTERCEPT

        # Rolling z-score
        lookback = min(self.LOOKBACK, len(spread))
        spread_tail = spread.iloc[-lookback:]
        roll_mean = spread_tail.mean()
        roll_std = spread_tail.std()

        if roll_std < 1e-8:
            return []

        current_z = (spread.iloc[-1] - roll_mean) / roll_std

        signals = []

        if current_z > self.ENTRY_THRESHOLD:
            # ETH overvalued vs BTC — buy BTC, close ETH if held
            signals.append(Signal(
                action="close",
                pair="ETH/USD",
                size_pct=1.0,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f} > +{self.ENTRY_THRESHOLD}: ETH expensive, rotating to BTC"
            ))
            signals.append(Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=0.95,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f} > +{self.ENTRY_THRESHOLD}: BTC undervalued vs ETH, entering long"
            ))

        elif current_z < -self.ENTRY_THRESHOLD:
            # BTC overvalued vs ETH — buy ETH, close BTC if held
            signals.append(Signal(
                action="close",
                pair="BTC/USD",
                size_pct=1.0,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f} < -{self.ENTRY_THRESHOLD}: BTC expensive, rotating to ETH"
            ))
            signals.append(Signal(
                action="buy",
                pair="ETH/USD",
                size_pct=0.95,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f} < -{self.ENTRY_THRESHOLD}: ETH undervalued vs BTC, entering long"
            ))

        elif abs(current_z) < self.EXIT_BAND:
            # Spread has mean-reverted — close all positions, go to cash
            signals.append(Signal(
                action="close",
                pair="ETH/USD",
                size_pct=1.0,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f}: spread mean-reverted, taking profit"
            ))
            signals.append(Signal(
                action="close",
                pair="BTC/USD",
                size_pct=1.0,
                order_type="market",
                rationale=f"ETH/BTC z={current_z:.2f}: spread mean-reverted, taking profit"
            ))

        # Neutral zone (0.5 < |z| < 2.0): hold current position

        return signals

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "strategy": self.name(),
            "hedge_ratio": self.HEDGE_RATIO,
            "entry_threshold": self.ENTRY_THRESHOLD,
            "exit_band": self.EXIT_BAND,
        }
