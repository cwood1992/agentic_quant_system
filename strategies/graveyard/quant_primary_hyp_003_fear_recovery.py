# Strategy: quant_primary_hyp_003_fear_recovery
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Contrarian Recovery Strategy
quant_primary_hyp_003_fear_recovery

Thesis: When Fear & Greed Index enters Extreme Fear (<= 20) and then begins recovering
(consecutive days of increasing index values), the market tends to produce outsized
upside returns as sentiment normalizes. The fear overshoot creates a dislocation
between sentiment and price.

Signal: F&G <= 20 for at least 3 consecutive days, then F&G begins rising.
Entry: First day where F&G increases after sustained extreme fear.
Exit: F&G > 50 (sentiment normalized) OR 20-day time stop OR -10% price stop loss.

BTC is the primary vehicle due to highest liquidity.
"""

import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal


class FearRecoveryStrategy(BaseStrategy):
    """
    Contrarian recovery entry triggered by sustained Extreme Fear + sentiment uptick.
    Long BTC when fear has been extreme and begins turning.
    """

    # Parameters
    EXTREME_FEAR_THRESHOLD = 20     # F&G below this = Extreme Fear
    MIN_FEAR_DAYS = 3               # Minimum consecutive extreme fear days before entry
    EXIT_SENTIMENT = 50             # F&G above this = sentiment normalized, exit
    TIME_STOP_CANDLES = 120         # 120 × 4h = 20 days max hold
    STOP_LOSS_PCT = 0.10            # 10% stop loss from entry
    POSITION_SIZE = 0.80            # 80% of capital

    def __init__(self):
        self._position = False
        self._entry_price = None
        self._candles_held = 0
        self._consecutive_fear_days = 0
        self._last_fg_value = None
        self._triggered = False     # Has the fear recovery signal fired?

    def name(self) -> str:
        return "quant_primary_hyp_003_fear_recovery"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h", "fear_greed:1d"]

    def _get_latest_fg(self, fg_df: pd.DataFrame):
        """Extract latest Fear & Greed value."""
        if fg_df is None or len(fg_df) == 0:
            return None
        # F&G data: expect 'close' or 'value' column
        if "close" in fg_df.columns:
            return float(fg_df["close"].iloc[-1])
        elif "value" in fg_df.columns:
            return float(fg_df["value"].iloc[-1])
        return None

    def _count_consecutive_extreme_fear(self, fg_df: pd.DataFrame) -> int:
        """Count consecutive days at Extreme Fear from most recent backwards."""
        if fg_df is None or len(fg_df) == 0:
            return 0

        col = "close" if "close" in fg_df.columns else "value"
        if col not in fg_df.columns:
            return 0

        values = fg_df[col].values
        count = 0
        # Count from the second-to-last entry backwards (last may be today's update)
        for i in range(len(values) - 2, max(len(values) - 20, -1), -1):
            if values[i] <= self.EXTREME_FEAR_THRESHOLD:
                count += 1
            else:
                break
        return count

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        if "BTC/USD" not in data:
            return signals

        btc_df = data["BTC/USD"]
        if len(btc_df) < 10:
            return signals

        current_price = float(btc_df["close"].iloc[-1])
        fg_df = data.get("fear_greed")

        current_fg = self._get_latest_fg(fg_df) if fg_df is not None else None

        # --- Stop loss check while in position ---
        if self._position and self._entry_price is not None:
            self._candles_held += 1
            loss_pct = (current_price - self._entry_price) / self._entry_price

            # Stop loss
            if loss_pct <= -self.STOP_LOSS_PCT:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Stop loss triggered: {loss_pct:.1%} from entry {self._entry_price:.0f}"
                ))
                self._position = False
                self._entry_price = None
                self._candles_held = 0
                self._triggered = False
                return signals

            # Time stop
            if self._candles_held >= self.TIME_STOP_CANDLES:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Time stop: held {self._candles_held} candles (20 days)"
                ))
                self._position = False
                self._entry_price = None
                self._candles_held = 0
                self._triggered = False
                return signals

            # Sentiment normalized exit
            if current_fg is not None and current_fg >= self.EXIT_SENTIMENT:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Sentiment normalized: F&G={current_fg:.0f} >= {self.EXIT_SENTIMENT}"
                ))
                self._position = False
                self._entry_price = None
                self._candles_held = 0
                self._triggered = False

            return signals

        # --- Entry logic (not in position) ---
        if current_fg is None or self._position:
            return signals

        # Count sustained extreme fear days
        consecutive_fear = self._count_consecutive_extreme_fear(fg_df)

        # Check if current F&G is turning up after extreme fear period
        is_turning_up = (
            self._last_fg_value is not None
            and current_fg > self._last_fg_value
            and current_fg <= 30  # Still in fear zone, just turning
        )

        # Entry condition: sustained fear AND turning up
        if (consecutive_fear >= self.MIN_FEAR_DAYS
                and is_turning_up
                and not self._triggered):
            signals.append(Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=self.POSITION_SIZE,
                order_type="market",
                rationale=(
                    f"Fear recovery signal: F&G={current_fg:.0f} (was {self._last_fg_value:.0f}), "
                    f"{consecutive_fear} consecutive extreme fear days"
                )
            ))
            self._position = True
            self._entry_price = current_price
            self._candles_held = 0
            self._triggered = True

        # Update last F&G reading
        self._last_fg_value = current_fg

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "close":
            self._position = False
            self._entry_price = None
            self._candles_held = 0

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "position": self._position,
            "entry_price": self._entry_price,
            "candles_held": self._candles_held,
            "triggered": self._triggered,
            "strategy": self.name()
        }
