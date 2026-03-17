# Strategy: quant_primary_hyp_004_ethbtc_rotation
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Long-Only Rotation Strategy v4
Hypothesis: quant_primary_hyp_004_ethbtc_rotation

Edge: ETH/BTC price ratio is cointegrated (ADF p=0.000571, half-life ~41h).
When ETH dramatically outperforms BTC (spread z-score > +2.0), reversion is
expected — hold BTC. When ETH dramatically underperforms (z-score < -2.0),
hold ETH. Otherwise, hold BTC (default long-only bias).

Signal: Rolling z-score of (ETH_price / BTC_price) ratio over 60-period window.
Entry: |z| > 2.0 triggers rotation. Exit: |z| < 0.5 (reversion complete).

Key fix from hyp_003: Uses simple rolling ratio z-score, not OLS regression.
Warmup: 60 candles (10 days at 4h). Signals begin firing after warmup.
"""

import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal


class ETHBTCRotationV4(BaseStrategy):
    """Long-only ETH/BTC rotation based on spread z-score mean reversion."""

    ENTRY_ZSCORE = 2.0    # Enter rotation when |z| exceeds this
    EXIT_ZSCORE = 0.5     # Exit (revert to BTC) when |z| falls below this
    LOOKBACK = 60         # Rolling window for z-score (60 × 4h = 10 days)
    MIN_CANDLES = 65      # Minimum candles before generating any signal

    def name(self) -> str:
        return "quant_primary_hyp_004_ethbtc_rotation"

    def required_feeds(self) -> list:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def on_data(self, data: dict) -> list:
        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return []

        if len(eth_df) < self.MIN_CANDLES or len(btc_df) < self.MIN_CANDLES:
            return []

        # Align on common index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]

        # Compute ETH/BTC ratio (stationary series)
        ratio = eth_close / btc_close

        # Rolling z-score
        roll_mean = ratio.rolling(window=self.LOOKBACK).mean()
        roll_std = ratio.rolling(window=self.LOOKBACK).std()

        # Current z-score (last complete value)
        current_ratio = ratio.iloc[-1]
        current_mean = roll_mean.iloc[-1]
        current_std = roll_std.iloc[-1]

        if pd.isna(current_mean) or pd.isna(current_std) or current_std < 1e-10:
            return []

        z = (current_ratio - current_mean) / current_std

        # State from persistent storage (fall back to instance variable)
        if not hasattr(self, "_position_pair"):
            self._position_pair = "BTC"  # Default: hold BTC

        signals = []

        if self._position_pair == "BTC":
            # Currently holding BTC — check if ETH has become cheap (z < -2.0)
            if z < -self.ENTRY_ZSCORE:
                # ETH underperforming dramatically → buy ETH (expect reversion up)
                signals.append(Signal(
                    action="sell",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC z={z:.2f} < -{self.ENTRY_ZSCORE} — rotating to ETH for reversion"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC z={z:.2f} — ETH undervalued vs BTC, expect reversion"
                ))
                self._position_pair = "ETH"

        elif self._position_pair == "ETH":
            # Currently holding ETH — exit conditions:
            # (a) z reverts toward zero (|z| < EXIT_ZSCORE)
            # (b) z pushes further negative (stop-loss style, z < -4.0)
            if abs(z) < self.EXIT_ZSCORE or z < -4.0:
                signals.append(Signal(
                    action="sell",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC z={z:.2f} — reversion complete or stop, rotating back to BTC"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC z={z:.2f} — returning to BTC"
                ))
                self._position_pair = "BTC"

        # Also handle the case where we START with z > +2.0 (ETH overvalued):
        # Default position is BTC, which is already the correct side when ETH is expensive.
        # For completeness, if we somehow start in a neutral state with z > +2.0,
        # we stay in BTC (already there by default).

        return signals

    def on_fill(self, fill: dict) -> None:
        """Update position tracking on fill."""
        if fill.get("pair") == "ETH/USD" and fill.get("action") == "buy":
            self._position_pair = "ETH"
        elif fill.get("pair") == "BTC/USD" and fill.get("action") == "buy":
            self._position_pair = "BTC"

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """Report current signal state."""
        return {
            "position_pair": getattr(self, "_position_pair", "BTC"),
            "strategy": self.name()
        }
