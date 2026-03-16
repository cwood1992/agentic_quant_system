# Strategy: quant_primary_hyp_021_ethbtc_statarb
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Long-Only Stat Arb
Hypothesis: quant_primary_hyp_021_ethbtc_statarb

Edge: ETH and BTC are cointegrated (ADF p=0.000249, half-life ~10 4h bars).
When ETH is cheap relative to BTC (spread z < -1.5), buy ETH expecting
mean reversion. Exit when spread reverts to 0 or crosses +0.5.

Long-only constraint: We buy ETH when cheap vs BTC.
We do NOT short ETH when expensive — Kraken margin not available.

Hedge ratio: ETH_price = 0.0483 * BTC_price + intercept
Spread = ETH_price - 0.0483 * BTC_price
Z-score = (spread - spread_mean) / spread_std
"""

import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal


class EthBtcLongOnlyStatArb(BaseStrategy):

    # Cointegration parameters (estimated from 90d 4h data, cycle 21)
    HEDGE_RATIO = 0.04828
    LOOKBACK = 120          # periods for rolling mean/std (~20 days at 4h)
    ENTRY_Z = -1.5          # enter long ETH when z < -1.5
    EXIT_Z = 0.3            # exit when z reverts past +0.3
    STOP_Z = -3.5           # hard stop if spread diverges further
    SIZE_PCT = 0.70         # 70% of capital per trade

    def __init__(self):
        self._in_position = False
        self._entry_z = None

    def name(self) -> str:
        return "quant_primary_hyp_021_ethbtc_statarb"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_zscore(self, eth_prices: pd.Series, btc_prices: pd.Series) -> float:
        """Compute rolling z-score of ETH/BTC spread."""
        if len(eth_prices) < self.LOOKBACK:
            return np.nan
        spread = eth_prices - self.HEDGE_RATIO * btc_prices
        # Use rolling window for mean/std — adapts to level changes
        recent = spread.iloc[-self.LOOKBACK:]
        mean = recent.mean()
        std = recent.std()
        if std < 1e-8:
            return np.nan
        current_spread = spread.iloc[-1]
        return (current_spread - mean) / std

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return signals
        if len(eth_df) < self.LOOKBACK or len(btc_df) < self.LOOKBACK:
            return signals

        # Align on common index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]

        # Align lengths
        min_len = min(len(eth_close), len(btc_close))
        eth_close = eth_close.iloc[-min_len:]
        btc_close = btc_close.iloc[-min_len:]

        z = self._compute_zscore(eth_close, btc_close)

        if np.isnan(z):
            return signals

        if not self._in_position:
            # Entry: ETH is cheap vs BTC
            if z < self.ENTRY_Z:
                self._in_position = True
                self._entry_z = z
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z:.2f} < entry threshold {self.ENTRY_Z}. ETH cheap vs BTC, expect mean reversion."
                ))
        else:
            # Exit conditions
            exit_reason = None
            if z >= self.EXIT_Z:
                exit_reason = f"Spread reverted: z={z:.2f} >= {self.EXIT_Z}"
            elif z <= self.STOP_Z:
                exit_reason = f"Stop loss: z={z:.2f} <= {self.STOP_Z} (diverging further)"

            if exit_reason:
                self._in_position = False
                self._entry_z = None
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))

        return signals

    def on_fill(self, fill: dict) -> None:
        # Track position state from fills for robustness
        if fill.get("action") == "buy":
            self._in_position = True
        elif fill.get("action") in ("close", "sell"):
            self._in_position = False
            self._entry_z = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self._in_position,
            "entry_z": self._entry_z,
        }
