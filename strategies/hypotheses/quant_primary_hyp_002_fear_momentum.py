# Strategy: quant_primary_hyp_002_fear_momentum
# Written by agent quant_primary via write_strategy_code tool.

"""
quant_primary_hyp_002_fear_momentum.py

Fear Divergence Momentum Strategy
----------------------------------
Thesis: When Fear & Greed is in Extreme Fear (<20) but short-term price
momentum is positive (7d return > threshold), markets are in a sentiment-
price divergence regime that historically precedes continued recovery.
We enter long BTC/ETH equally weighted and hold until either:
  - Fear & Greed normalizes (>45, "Neutral" territory), OR
  - Price momentum reverses (7d return goes negative), OR
  - Max hold period of 30 days is reached

This is a regime-capture strategy, not a frequent-signal strategy.
Expected: 2-5 trades per 90-day backtest period.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.base import BaseStrategy, Signal
import pandas as pd
import numpy as np


class FearDivergenceMomentum(BaseStrategy):
    """
    Fear Divergence Momentum: Enter when F&G < 20 and 7d momentum positive.
    Exit when F&G > 45 or momentum reverses.
    """

    # ------------------------------------------------------------------ #
    #  Configuration                                                       #
    # ------------------------------------------------------------------ #
    FEAR_ENTRY_THRESHOLD = 20       # F&G must be below this to enter
    FEAR_EXIT_THRESHOLD = 45        # F&G above this triggers exit
    MOMENTUM_WINDOW_DAYS = 7        # lookback for momentum check
    MOMENTUM_MIN_RETURN = 0.02      # 7d return must exceed 2% to confirm entry
    MOMENTUM_STOP_RETURN = -0.05    # 7d return below -5% triggers stop-loss exit
    MAX_HOLD_CANDLES = 30 * 6       # 30 days * 6 four-hour candles/day
    SIZE_PER_LEG = 0.45             # 45% per leg (BTC + ETH = 90% deployed)

    def __init__(self):
        self._position_open = False
        self._hold_counter = 0
        self._entry_price_btc = None
        self._entry_price_eth = None
        self._last_fg = None

    def name(self) -> str:
        return "quant_primary_hyp_002_fear_momentum"

    def required_feeds(self) -> list[str]:
        return [
            "BTC/USD:4h",
            "ETH/USD:4h",
            "fear_greed_index:1d",
        ]

    def _get_fear_greed(self, data: dict) -> float | None:
        """Extract latest Fear & Greed value from data dict."""
        fg_key = "fear_greed_index:1d"
        if fg_key not in data:
            return None
        fg_df = data[fg_key]
        if fg_df is None or len(fg_df) == 0:
            return None
        # Column may be 'value' or 'close'
        for col in ["value", "close", "fear_greed"]:
            if col in fg_df.columns:
                val = fg_df[col].iloc[-1]
                if pd.notna(val):
                    return float(val)
        return None

    def _compute_7d_return(self, df: pd.DataFrame) -> float | None:
        """Compute 7-day return from 4h candles (42 candles = 7 days)."""
        candles_7d = 7 * 6  # 6 four-hour candles per day
        if len(df) < candles_7d + 1:
            return None
        close_now = df["close"].iloc[-1]
        close_7d_ago = df["close"].iloc[-(candles_7d + 1)]
        if close_7d_ago == 0:
            return None
        return (close_now - close_7d_ago) / close_7d_ago

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        # -- Retrieve candle data --
        btc_key = "BTC/USD:4h"
        eth_key = "ETH/USD:4h"

        if btc_key not in data or eth_key not in data:
            return signals

        btc_df = data[btc_key]
        eth_df = data[eth_key]

        if btc_df is None or eth_df is None:
            return signals
        if len(btc_df) < 43 or len(eth_df) < 43:
            return signals

        # -- Get current Fear & Greed --
        fg_value = self._get_fear_greed(data)

        # -- Compute momentum --
        btc_7d_return = self._compute_7d_return(btc_df)
        eth_7d_return = self._compute_7d_return(eth_df)

        if btc_7d_return is None or eth_7d_return is None:
            return signals

        avg_7d_return = (btc_7d_return + eth_7d_return) / 2.0

        # -- Position management --
        if self._position_open:
            self._hold_counter += 1

            should_exit = False
            exit_reason = ""

            # Exit condition 1: Fear normalizes
            if fg_value is not None and fg_value > self.FEAR_EXIT_THRESHOLD:
                should_exit = True
                exit_reason = f"F&G normalized to {fg_value:.0f} (>{self.FEAR_EXIT_THRESHOLD})"

            # Exit condition 2: Momentum reverses hard
            elif avg_7d_return < self.MOMENTUM_STOP_RETURN:
                should_exit = True
                exit_reason = f"7d avg return {avg_7d_return:.1%} < stop {self.MOMENTUM_STOP_RETURN:.1%}"

            # Exit condition 3: Max hold period
            elif self._hold_counter >= self.MAX_HOLD_CANDLES:
                should_exit = True
                exit_reason = f"Max hold period reached ({self.MAX_HOLD_CANDLES} candles)"

            if should_exit:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Exit BTC: {exit_reason}"
                ))
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Exit ETH: {exit_reason}"
                ))
                self._position_open = False
                self._hold_counter = 0
                self._entry_price_btc = None
                self._entry_price_eth = None

        else:
            # -- Entry logic --
            entry_conditions_met = (
                fg_value is not None
                and fg_value < self.FEAR_ENTRY_THRESHOLD
                and avg_7d_return > self.MOMENTUM_MIN_RETURN
            )

            if entry_conditions_met:
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=(
                        f"Fear divergence entry: F&G={fg_value:.0f} (<{self.FEAR_ENTRY_THRESHOLD}), "
                        f"7d avg return={avg_7d_return:.1%} (>{self.MOMENTUM_MIN_RETURN:.0%})"
                    )
                ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PER_LEG,
                    order_type="market",
                    rationale=(
                        f"Fear divergence entry: F&G={fg_value:.0f} (<{self.FEAR_ENTRY_THRESHOLD}), "
                        f"7d avg return={avg_7d_return:.1%} (>{self.MOMENTUM_MIN_RETURN:.0%})"
                    )
                ))
                self._position_open = True
                self._hold_counter = 0
                self._entry_price_btc = btc_df["close"].iloc[-1]
                self._entry_price_eth = eth_df["close"].iloc[-1]

        return signals

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "position_open": self._position_open,
            "hold_counter": self._hold_counter,
            "last_fg": self._last_fg,
        }
