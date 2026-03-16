# Strategy: quant_primary_hyp_012_fear_greed_btc_contrarian
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Contrarian BTC Long Strategy
Hypothesis: quant_primary_hyp_012_fear_greed_btc_contrarian

Entry: Fear & Greed Index < 20 (Extreme Fear) AND 7-day BTC return > 0 (price recovering)
Exit: Fear & Greed > 40 OR position drawdown > 10%
Regime: Post-capitulation recovery
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearGreedBTCContrarianStrategy(BaseStrategy):
    """
    Long BTC when Extreme Fear sentiment diverges from recovering price action.
    The thesis: Extreme Fear readings (< 20) combined with positive 7d price momentum
    indicate capitulation has occurred and forward returns are asymmetrically positive.
    """

    # Configuration
    FEAR_ENTRY_THRESHOLD = 20       # F&G must be below this to enter
    FEAR_EXIT_THRESHOLD = 40        # F&G above this signals sentiment normalisation → exit
    SEVEN_DAY_CANDLES = 42          # 7 days × 6 × 4h candles
    STOP_LOSS_PCT = 0.10            # 10% drawdown from entry → exit
    POSITION_SIZE = 0.45            # 45% of capital per position

    def __init__(self):
        self._in_position = False
        self._entry_price = None
        self._entry_candle_idx = None

    def name(self) -> str:
        return "quant_primary_hyp_012_fear_greed_btc_contrarian"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h", "fear_greed_index:1d"]

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        btc_candles = data.get("BTC/USD:4h")
        fg_data = data.get("fear_greed_index:1d")

        if btc_candles is None or fg_data is None:
            return signals

        if len(btc_candles) < self.SEVEN_DAY_CANDLES + 1:
            return signals

        if len(fg_data) < 2:
            return signals

        # Current values
        current_close = float(btc_candles["close"].iloc[-1])
        close_7d_ago = float(btc_candles["close"].iloc[-self.SEVEN_DAY_CANDLES])
        seven_day_return = (current_close - close_7d_ago) / close_7d_ago

        # Fear & Greed — use most recent reading
        current_fg = float(fg_data["value"].iloc[-1])

        if not self._in_position:
            # Entry condition: Extreme Fear + price recovery
            if current_fg < self.FEAR_ENTRY_THRESHOLD and seven_day_return > 0:
                self._in_position = True
                self._entry_price = current_close
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=(
                        f"F&G={current_fg:.1f} (Extreme Fear), "
                        f"7d_return={seven_day_return:.2%} (positive recovery). "
                        f"Contrarian long entry."
                    )
                ))
        else:
            # Exit conditions
            drawdown = (current_close - self._entry_price) / self._entry_price
            stop_hit = drawdown < -self.STOP_LOSS_PCT
            fear_normalised = current_fg >= self.FEAR_EXIT_THRESHOLD

            if stop_hit or fear_normalised:
                self._in_position = False
                reason = "stop_loss" if stop_hit else "fear_normalised"
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=(
                        f"Exit triggered: {reason}. "
                        f"F&G={current_fg:.1f}, drawdown={drawdown:.2%}"
                    )
                ))

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy":
            self._entry_price = fill.get("fill_price", self._entry_price)
        elif fill.get("action") == "close":
            self._in_position = False
            self._entry_price = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
        }
