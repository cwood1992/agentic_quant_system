# Strategy: quant_primary_hyp_002_ethbtc_rotation
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Long-Only Spread Rotation Strategy
quant_primary_hyp_002_ethbtc_rotation

Thesis: ETH and BTC are cointegrated (ADF p=0.0007, hedge ratio=0.0484, half-life=~1.8 days).
When the spread (ETH - hedge*BTC) is significantly below its mean (z-score < -1.5),
ETH is cheap relative to BTC -> go long ETH.
When spread is significantly above its mean (z-score > +1.5),
BTC is cheap relative to ETH -> go long BTC.
When spread is near neutral, hold existing position or cash.

This is a synthetic pairs rotation — long-only version of stat arb.
Capital is concentrated in the relatively undervalued asset.
"""

import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal


class EthBtcRotationStrategy(BaseStrategy):
    """
    Long-only ETH/BTC spread rotation based on cointegration.
    Entry when z-score crosses threshold, exit when spread reverts.
    """

    # Strategy parameters
    LOOKBACK = 90          # candles for rolling regression (90 × 4h = ~15 days)
    ENTRY_Z = 1.5          # z-score threshold to initiate position
    EXIT_Z = 0.3           # z-score threshold to close (near mean)
    POSITION_SIZE = 0.80   # fraction of capital per position

    def __init__(self):
        self._position = None  # 'ETH' | 'BTC' | None
        self._spread_history = []

    def name(self) -> str:
        return "quant_primary_hyp_002_ethbtc_rotation"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_spread_z(self, eth_prices: pd.Series, btc_prices: pd.Series):
        """OLS hedge ratio on rolling window, returns current z-score."""
        if len(eth_prices) < self.LOOKBACK:
            return None, None

        eth_w = eth_prices.iloc[-self.LOOKBACK:]
        btc_w = btc_prices.iloc[-self.LOOKBACK:]

        # OLS: ETH = alpha + beta*BTC + eps
        btc_arr = btc_w.values
        eth_arr = eth_w.values
        X = np.column_stack([np.ones(len(btc_arr)), btc_arr])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, eth_arr, rcond=None)
        except np.linalg.LinAlgError:
            return None, None

        alpha, beta = coeffs[0], coeffs[1]
        spread = eth_arr - (alpha + beta * btc_arr)

        spread_mean = spread.mean()
        spread_std = spread.std()
        if spread_std < 1e-10:
            return None, None

        current_z = (spread[-1] - spread_mean) / spread_std
        return current_z, beta

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        # Require both feeds
        if "ETH/USD" not in data or "BTC/USD" not in data:
            return signals

        eth_df = data["ETH/USD"]
        btc_df = data["BTC/USD"]

        if len(eth_df) < self.LOOKBACK + 5 or len(btc_df) < self.LOOKBACK + 5:
            return signals

        eth_prices = eth_df["close"]
        btc_prices = btc_df["close"]

        z_score, hedge_ratio = self._compute_spread_z(eth_prices, btc_prices)

        if z_score is None:
            return signals

        # --- Entry logic ---
        # Spread below mean (ETH cheap relative to BTC) -> long ETH
        if z_score < -self.ENTRY_Z:
            if self._position != "ETH":
                if self._position == "BTC":
                    signals.append(Signal(
                        action="close",
                        pair="BTC/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=f"Rotating from BTC to ETH: spread z={z_score:.2f} < -{self.ENTRY_Z}"
                    ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"ETH cheap vs BTC: spread z={z_score:.2f}, hedge={hedge_ratio:.4f}"
                ))
                self._position = "ETH"

        # Spread above mean (BTC cheap relative to ETH) -> long BTC
        elif z_score > self.ENTRY_Z:
            if self._position != "BTC":
                if self._position == "ETH":
                    signals.append(Signal(
                        action="close",
                        pair="ETH/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=f"Rotating from ETH to BTC: spread z={z_score:.2f} > {self.ENTRY_Z}"
                    ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"BTC cheap vs ETH: spread z={z_score:.2f}, hedge={hedge_ratio:.4f}"
                ))
                self._position = "BTC"

        # --- Exit logic: spread has reverted ---
        elif abs(z_score) < self.EXIT_Z:
            if self._position == "ETH":
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread reverted to neutral: z={z_score:.2f}, closing ETH long"
                ))
                self._position = None
            elif self._position == "BTC":
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread reverted to neutral: z={z_score:.2f}, closing BTC long"
                ))
                self._position = None

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track position state from fills."""
        if fill.get("action") == "close":
            self._position = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "current_position": self._position,
            "strategy": self.name()
        }
