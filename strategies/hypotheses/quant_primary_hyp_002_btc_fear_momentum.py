# Strategy: quant_primary_hyp_002_btc_fear_momentum
# Written by agent quant_primary via write_strategy_code tool.

"""
BTC Extreme Fear Contrarian Momentum
hypothesis_id: quant_primary_hyp_002_btc_fear_momentum

Strategy: When Fear & Greed Index is in Extreme Fear territory (<= 20) AND
BTC price is stabilizing (not in freefall — positive or flat 24h return on 4h candles),
enter a long position expecting mean reversion from sentiment extremes.
Exit when Fear & Greed rises above 40 or price target is hit (+5% from entry).

Background: F&G has been at 15-18 for multiple consecutive days while BTC stabilized
around $69-70k. Historically, sustained extreme fear with price stabilization 
precedes upward mean reversion in sentiment (and price).
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BtcFearMomentum(BaseStrategy):

    def __init__(self):
        self.fear_entry_threshold = 20      # Extreme Fear: F&G <= 20
        self.fear_exit_threshold = 40       # Exit when fear subsides
        self.profit_target_pct = 0.05       # 5% profit target
        self.stop_loss_pct = 0.04           # 4% stop loss
        self.stabilization_window = 6       # 6 x 4h = 24h lookback for stability check
        self.size = 0.70                    # 70% of capital
        self.position = None
        self.entry_price = None

    def name(self) -> str:
        return "quant_primary_hyp_002_btc_fear_momentum"

    def required_feeds(self) -> list:
        return ["BTC/USD:4h"]

    def _get_fear_greed(self, data: dict):
        """Extract Fear & Greed value from supplementary feed data."""
        fg = data.get("fear_greed_index")
        if fg is not None:
            if isinstance(fg, (int, float)):
                return float(fg)
            if isinstance(fg, dict):
                return float(fg.get("value", fg.get("score", 50)))
        return None

    def on_data(self, data: dict) -> list:
        btc_df = data.get("BTC/USD:4h")
        if btc_df is None or len(btc_df) < self.stabilization_window + 1:
            return []

        fear_greed = self._get_fear_greed(data)
        current_price = btc_df["close"].iloc[-1]

        signals = []

        # Price stabilization check: 24h return (6 x 4h periods) is >= -1%
        price_24h_ago = btc_df["close"].iloc[-self.stabilization_window - 1]
        price_change_24h = (current_price - price_24h_ago) / price_24h_ago

        # Entry logic
        if self.position is None:
            if (fear_greed is not None
                    and fear_greed <= self.fear_entry_threshold
                    and price_change_24h >= -0.01):  # Not in freefall
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.size,
                    order_type="market",
                    rationale=(
                        f"Extreme Fear entry: F&G={fear_greed}, "
                        f"24h_chg={price_change_24h:.2%}, price stabilizing"
                    )
                ))
                self.position = "long"
                self.entry_price = current_price

        elif self.position == "long" and self.entry_price is not None:
            pnl_pct = (current_price - self.entry_price) / self.entry_price

            exit_reason = None

            # Profit target hit
            if pnl_pct >= self.profit_target_pct:
                exit_reason = f"Profit target hit: {pnl_pct:.2%}"

            # Stop loss hit
            elif pnl_pct <= -self.stop_loss_pct:
                exit_reason = f"Stop loss hit: {pnl_pct:.2%}"

            # Fear subsided — sentiment normalized
            elif fear_greed is not None and fear_greed >= self.fear_exit_threshold:
                exit_reason = f"Fear subsided: F&G={fear_greed}"

            if exit_reason:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
                self.position = None
                self.entry_price = None

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            self.entry_price = fill.get("fill_price", self.entry_price)

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "position": self.position or "flat",
            "entry_price": self.entry_price
        }
