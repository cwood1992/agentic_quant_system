# Strategy: quant_primary_hyp_001_ethbtc_pair_trade
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Cointegration Pair Trade Strategy
hypothesis_id: quant_primary_hyp_001_ethbtc_pair_trade

Thesis: ETH and BTC are cointegrated (ADF p=0.0008, hedge_ratio=0.0484, half_life=~43h).
When the spread z-score deviates beyond ±1.5 std, it mean-reverts.
Long ETH / Short BTC when z < -1.5. Short ETH / Long BTC when z > +1.5.
Close at z crossing 0. Hard stop at ±3.0.

Note: Short leg requires margin trading. If margin unavailable, only long-ETH leg fires.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcPairTradeStrategy(BaseStrategy):
    """
    ETH/BTC cointegration spread mean-reversion strategy.

    Entry: z-score of (ETH - hedge_ratio*BTC - intercept) crosses ±1.5
    Exit: z-score crosses 0 (mean reversion complete), or ±3.0 (stop loss)
    Position sizing: 40% capital per leg
    """

    HEDGE_RATIO = 0.0484
    INTERCEPT = -1304.561234
    ZSCORE_ENTRY = 1.5
    ZSCORE_EXIT = 0.0
    ZSCORE_STOP = 3.0
    ZSCORE_WINDOW = 120   # 120 × 4h = 20 days for rolling z-score
    MIN_OBSERVATIONS = 60  # Need at least 60 bars to compute reliable z-score
    SIZE_PER_LEG = 0.40    # 40% of capital per leg

    def __init__(self):
        self._position = None  # None, 'long_eth_short_btc', 'short_eth_long_btc'
        self._entry_z = None

    def name(self) -> str:
        return "quant_primary_hyp_001_ethbtc_pair_trade"

    def required_feeds(self) -> list:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_zscore(self, eth_prices: pd.Series, btc_prices: pd.Series) -> pd.Series:
        """Compute rolling z-score of the cointegration spread."""
        spread = eth_prices - (self.HEDGE_RATIO * btc_prices) - self.INTERCEPT
        window = min(self.ZSCORE_WINDOW, len(spread))
        rolling_mean = spread.rolling(window=window, min_periods=self.MIN_OBSERVATIONS).mean()
        rolling_std = spread.rolling(window=window, min_periods=self.MIN_OBSERVATIONS).std()
        zscore = (spread - rolling_mean) / rolling_std.replace(0, np.nan)
        return zscore

    def on_data(self, data: dict) -> list:
        signals = []

        # Extract price series
        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return signals

        if len(eth_df) < self.MIN_OBSERVATIONS or len(btc_df) < self.MIN_OBSERVATIONS:
            return signals

        # Align on common index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]
        common_idx = eth_close.index.intersection(btc_close.index)

        if len(common_idx) < self.MIN_OBSERVATIONS:
            return signals

        eth_aligned = eth_close.loc[common_idx]
        btc_aligned = btc_close.loc[common_idx]

        zscore_series = self._compute_zscore(eth_aligned, btc_aligned)

        if zscore_series.empty or pd.isna(zscore_series.iloc[-1]):
            return signals

        current_z = zscore_series.iloc[-1]
        prev_z = zscore_series.iloc[-2] if len(zscore_series) >= 2 else current_z

        # === EXIT LOGIC (check before entry) ===
        if self._position == "long_eth_short_btc":
            # Close when z crosses back above 0 (mean reversion) or hits stop
            crossed_zero = (prev_z < 0.0 and current_z >= 0.0) or (prev_z > 0.0 and current_z < 0.0)
            hit_stop = current_z <= -self.ZSCORE_STOP

            if crossed_zero or hit_stop:
                rationale = (
                    f"Exit long_eth_short_btc: z={current_z:.2f} "
                    f"({'mean-reverted to zero' if crossed_zero else 'STOP LOSS triggered'})"
                )
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=rationale
                ))
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=rationale
                ))
                self._position = None
                self._entry_z = None
                return signals

        elif self._position == "short_eth_long_btc":
            # Close when z crosses back below 0 or hits stop
            crossed_zero = (prev_z > 0.0 and current_z <= 0.0) or (prev_z < 0.0 and current_z > 0.0)
            hit_stop = current_z >= self.ZSCORE_STOP

            if crossed_zero or hit_stop:
                rationale = (
                    f"Exit short_eth_long_btc: z={current_z:.2f} "
                    f"({'mean-reverted to zero' if crossed_zero else 'STOP LOSS triggered'})"
                )
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=rationale
                ))
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=rationale
                ))
                self._position = None
                self._entry_z = None
                return signals

        # === ENTRY LOGIC (only when flat) ===
        if self._position is None:

            if current_z < -self.ZSCORE_ENTRY:
                # ETH underpriced vs BTC: buy ETH, sell BTC
                rationale = (
                    f"Enter long_eth_short_btc: z={current_z:.2f} < -{self.ZSCORE_ENTRY}. "
                    f"ETH underpriced vs BTC by {abs(current_z):.1f} std devs. "
                    f"Spread={eth_aligned.iloc[-1]:.2f} - {self.HEDGE_RATIO}*{btc_aligned.iloc[-1]:.2f} - {abs(self.INTERCEPT):.2f}"
                )
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=rationale
                ))
                # Short BTC (requires margin — will be a no-op if margin not available)
                signals.append(Signal(
                    action="sell",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=rationale + " [SHORT LEG — requires margin]"
                ))
                self._position = "long_eth_short_btc"
                self._entry_z = current_z

            elif current_z > self.ZSCORE_ENTRY:
                # BTC underpriced vs ETH: buy BTC, sell ETH
                rationale = (
                    f"Enter short_eth_long_btc: z={current_z:.2f} > +{self.ZSCORE_ENTRY}. "
                    f"ETH overpriced vs BTC by {current_z:.1f} std devs."
                )
                signals.append(Signal(
                    action="sell",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=rationale + " [SHORT LEG — requires margin]"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=rationale
                ))
                self._position = "short_eth_long_btc"
                self._entry_z = current_z

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track fills for position state reconciliation."""
        pass

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """Return current spread state for digest."""
        return {
            "position": self._position or "flat",
            "entry_z": self._entry_z,
            "hedge_ratio": self.HEDGE_RATIO,
            "zscore_window_periods": self.ZSCORE_WINDOW,
        }
