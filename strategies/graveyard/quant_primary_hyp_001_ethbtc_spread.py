# Strategy: quant_primary_hyp_001_ethbtc_spread
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Spread Mean-Reversion Strategy
Hypothesis: hyp_001_ethbtc_spread
Thesis: ETH and BTC are cointegrated (ADF p=0.0009). The spread mean-reverts
with half-life ~44.5h. Z-score entry/exit on the spread captures this reversion.

Hedge ratio: 0.0484 (ETH_price = 0.0484 * BTC_price + intercept)
Spread = ETH_price - hedge_ratio * BTC_price
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class ETHBTCSpreadMeanReversion(BaseStrategy):
    """
    Pairs trade: long ETH / short BTC (or vice versa) when the cointegrated
    spread deviates beyond a z-score threshold.

    Entry:  |z| > entry_threshold
    Exit:   |z| < exit_threshold  OR  |z| > stop_threshold (stop loss)

    Position sizing: equal capital to each leg of the pair trade.
    We simulate the pair trade via a single signal on ETH (the non-base leg),
    since BTC is the reference and its direction is implied by the spread.

    In practice, this strategy goes:
      - Long ETH when spread is too low (ETH cheap vs BTC)
      - Short ETH when spread is too high (ETH expensive vs BTC)
    We hedge by simultaneously taking the opposite position in BTC.

    Because we cannot easily short on Kraken spot, we implement as:
      - Long ETH (buy ETH/USD) when z < -entry_threshold  [ETH undervalued vs BTC]
      - Long BTC (buy BTC/USD) when z > +entry_threshold  [BTC undervalued vs ETH]
      - Close when |z| < exit_threshold
    This is a long-only proxy for the pair trade.
    """

    HEDGE_RATIO = 0.0484       # from cointegration analysis (90d, 4h)
    INTERCEPT = -1303.46       # spread intercept
    LOOKBACK = 168             # periods for z-score normalization (~28 days at 4h)
    ENTRY_Z = 1.5              # z-score to enter
    EXIT_Z = 0.5               # z-score to exit (near mean)
    STOP_Z = 3.0               # z-score to stop out (spread diverging)
    LEG_SIZE = 0.35            # fraction of capital per leg (2 legs = 70% deployed)

    def __init__(self):
        self._position = None      # None, 'long_eth', or 'long_btc'
        self._spread_history = []

    def name(self) -> str:
        return "quant_primary_hyp_001_ethbtc_spread"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_spread(self, eth_price: float, btc_price: float) -> float:
        return eth_price - self.HEDGE_RATIO * btc_price - self.INTERCEPT

    def on_data(self, data: dict) -> list[Signal]:
        # Require both feeds
        if "ETH/USD" not in data or "BTC/USD" not in data:
            return []

        eth_df = data["ETH/USD"]
        btc_df = data["BTC/USD"]

        if len(eth_df) < self.LOOKBACK or len(btc_df) < self.LOOKBACK:
            return []

        eth_price = float(eth_df["close"].iloc[-1])
        btc_price = float(btc_df["close"].iloc[-1])

        # Compute spread history for z-score
        n = min(len(eth_df), len(btc_df), self.LOOKBACK)
        eth_closes = eth_df["close"].iloc[-n:].values.astype(float)
        btc_closes = btc_df["close"].iloc[-n:].values.astype(float)
        spreads = eth_closes - self.HEDGE_RATIO * btc_closes - self.INTERCEPT

        spread_mean = np.mean(spreads)
        spread_std = np.std(spreads)

        if spread_std < 1e-8:
            return []

        current_spread = self._compute_spread(eth_price, btc_price)
        z_score = (current_spread - spread_mean) / spread_std

        signals = []

        # Exit logic
        if self._position == 'long_eth':
            if abs(z_score) < self.EXIT_Z:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread mean-reverted: z={z_score:.2f} < exit_z={self.EXIT_Z}"
                ))
                self._position = None
            elif z_score > self.STOP_Z:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Stop loss: z={z_score:.2f} > stop_z={self.STOP_Z}"
                ))
                self._position = None

        elif self._position == 'long_btc':
            if abs(z_score) < self.EXIT_Z:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread mean-reverted: z={z_score:.2f} < exit_z={self.EXIT_Z}"
                ))
                self._position = None
            elif z_score < -self.STOP_Z:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Stop loss: z={z_score:.2f} < -stop_z={self.STOP_Z}"
                ))
                self._position = None

        # Entry logic (only if no position)
        if self._position is None:
            if z_score < -self.ENTRY_Z:
                # ETH cheap vs BTC — long ETH
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"Spread below mean: z={z_score:.2f}, long ETH (undervalued vs BTC)"
                ))
                self._position = 'long_eth'
            elif z_score > self.ENTRY_Z:
                # BTC cheap vs ETH — long BTC
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.LEG_SIZE,
                    order_type="market",
                    rationale=f"Spread above mean: z={z_score:.2f}, long BTC (undervalued vs ETH)"
                ))
                self._position = 'long_btc'

        return signals

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "current_position": self._position,
            "strategy": self.name(),
        }
