# Strategy: quant_primary_hyp_014_ethbtc_spreadlong
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Cointegration Spread — Long-Only Mean Reversion
Strategy ID: quant_primary_hyp_014_ethbtc_spreadlong

Thesis: ETH/USD and BTC/USD are cointegrated (ADF p=0.000245, half-life ~40h).
When ETH is cheap relative to BTC on the cointegrated spread (z < -2.0),
buy ETH and hold until the spread reverts toward its mean (z > -0.5).
Long-only — no shorting. Only trade the ETH-is-cheap side.

Parameters:
  - hedge_ratio: 0.0483 (OLS coefficient, ETH_price ~ hedge_ratio * BTC_price + intercept)
  - intercept: -1303.6
  - entry_z: -2.0 (buy ETH when spread z-score below this)
  - exit_z: -0.5 (exit when spread has partially reverted)
  - size_pct: 0.40 (40% of capital per trade)
  - lookback: 90 days of 4h candles for spread statistics
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcSpreadLongStrategy(BaseStrategy):
    """
    Long-only cointegration spread reversion: go long ETH when it is
    statistically cheap relative to BTC (z-score < entry_z), exit at exit_z.
    """

    HEDGE_RATIO = 0.0483
    INTERCEPT = -1303.6
    ENTRY_Z = -2.0
    EXIT_Z = -0.5
    SIZE_PCT = 0.40
    MIN_CANDLES = 84  # ~14 days of 4h candles minimum for spread stats
    LOOKBACK = 540    # ~90 days of 4h candles for spread mean/std

    def __init__(self):
        self._in_position = False

    def name(self) -> str:
        return "quant_primary_hyp_014_ethbtc_spreadlong"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_zscore(self, eth_df: pd.DataFrame, btc_df: pd.DataFrame) -> float | None:
        """Compute current z-score of the cointegration spread."""
        # Align on timestamp
        eth_close = eth_df["close"].tail(self.LOOKBACK)
        btc_close = btc_df["close"].tail(self.LOOKBACK)

        # Ensure same length
        n = min(len(eth_close), len(btc_close))
        if n < self.MIN_CANDLES:
            return None

        eth_close = eth_close.iloc[-n:].values
        btc_close = btc_close.iloc[-n:].values

        # Spread = ETH - hedge_ratio * BTC - intercept
        spread = eth_close - self.HEDGE_RATIO * btc_close - self.INTERCEPT

        spread_mean = spread.mean()
        spread_std = spread.std()
        if spread_std == 0:
            return None

        current_spread = spread[-1]
        z = (current_spread - spread_mean) / spread_std
        return float(z)

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return []
        if len(eth_df) < self.MIN_CANDLES or len(btc_df) < self.MIN_CANDLES:
            return []

        z = self._compute_zscore(eth_df, btc_df)
        if z is None:
            return []

        signals = []

        if not self._in_position:
            # Entry: ETH is cheap relative to BTC
            if z < self.ENTRY_Z:
                self._in_position = True
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z:.2f} < entry threshold {self.ENTRY_Z}. "
                               f"ETH cheap vs BTC — expecting mean reversion. "
                               f"Half-life ~40h, ADF p=0.000245."
                ))
        else:
            # Exit: spread has reverted sufficiently
            if z > self.EXIT_Z:
                self._in_position = False
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z={z:.2f} > exit threshold {self.EXIT_Z}. "
                               f"Spread reversion complete — closing position."
                ))

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track position state from fills."""
        action = fill.get("action", "")
        if action == "buy":
            self._in_position = True
        elif action in ("sell", "close"):
            self._in_position = False

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "strategy": self.name(),
            "in_position": self._in_position,
            "hedge_ratio": self.HEDGE_RATIO,
            "entry_z_threshold": self.ENTRY_Z,
            "exit_z_threshold": self.EXIT_Z,
        }
