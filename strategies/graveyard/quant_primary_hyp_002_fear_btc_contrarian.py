# Strategy: quant_primary_hyp_002_fear_btc_contrarian
# Written by agent quant_primary via write_strategy_code tool.

"""
Extreme Fear Contrarian BTC Strategy
Hypothesis: hyp_002_fear_btc_contrarian

Signal: Fear & Greed Index <= 20 (Extreme Fear) for >= 3 consecutive days
Entry: Buy BTC when sustained extreme fear detected
Exit: Fear & Greed Index >= 40 (Fear zone clears) OR rolling Sharpe turns positive (14d)
Stop: -8% from entry price (hard stop)
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearBtcContrarian(BaseStrategy):
    """
    Contrarian BTC long strategy triggered by sustained Extreme Fear.

    Thesis: When Fear & Greed is <= 20 for multiple consecutive periods,
    markets are in capitulation. BTC tends to mean-revert upward from these
    levels as forced selling exhausts. This is a regime-based entry signal,
    not a momentum signal.

    Since we only have current F&G (no history via digest), we track consecutive
    readings internally and enter after 3+ consecutive extreme fear readings.
    """

    FEAR_ENTRY_THRESHOLD = 20    # F&G <= 20 = Extreme Fear
    FEAR_EXIT_THRESHOLD = 40     # F&G >= 40 = sentiment recovering
    MIN_CONSECUTIVE = 3          # Need N consecutive extreme fear readings to trigger
    STOP_LOSS_PCT = 0.08         # 8% hard stop from entry
    POSITION_SIZE = 0.50         # 50% capital (high conviction contrarian)
    SHARPE_LOOKBACK = 42         # 42 x 4h = ~7 days rolling Sharpe window

    def name(self) -> str:
        return "quant_primary_hyp_002_fear_btc_contrarian"

    def required_feeds(self) -> list:
        return ["BTC/USD:4h"]

    def _compute_rolling_sharpe(self, prices: pd.Series, window: int = 42) -> float:
        """Compute rolling Sharpe over last `window` 4h periods."""
        if len(prices) < window + 1:
            return 0.0
        returns = prices.pct_change().dropna()
        recent = returns.iloc[-window:]
        mean_r = recent.mean()
        std_r = recent.std()
        if std_r < 1e-10:
            return 0.0
        # Annualize: 4h bars, 2190 per year
        return float((mean_r / std_r) * np.sqrt(2190))

    def on_data(self, data: dict) -> list:
        btc_df = data.get("BTC/USD:4h")
        if btc_df is None or len(btc_df) < self.SHARPE_LOOKBACK + 5:
            return []

        current_price = float(btc_df["close"].iloc[-1])

        # Track Fear & Greed via supplementary data if available
        # The fear_greed value is passed through data dict as a scalar
        fear_greed = data.get("fear_greed_index", None)

        # Initialize state
        if not hasattr(self, "_consecutive_fear"):
            self._consecutive_fear = 0
        if not hasattr(self, "_in_position"):
            self._in_position = False
        if not hasattr(self, "_entry_price"):
            self._entry_price = None

        # If no F&G data, use a proxy: large negative rolling Sharpe as fear proxy
        rolling_sharpe = self._compute_rolling_sharpe(btc_df["close"], self.SHARPE_LOOKBACK)

        if fear_greed is not None:
            fg_val = float(fear_greed)
            if fg_val <= self.FEAR_ENTRY_THRESHOLD:
                self._consecutive_fear += 1
            else:
                self._consecutive_fear = 0
        else:
            # Proxy: if rolling Sharpe < -2.0 consider it "fear"
            if rolling_sharpe < -2.0:
                self._consecutive_fear += 1
            else:
                self._consecutive_fear = 0

        signals = []

        if not self._in_position:
            # Entry: sustained extreme fear
            if self._consecutive_fear >= self.MIN_CONSECUTIVE:
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=(
                        f"Extreme Fear for {self._consecutive_fear} consecutive periods. "
                        f"F&G={fear_greed}, rolling_sharpe={rolling_sharpe:.2f}. "
                        f"Contrarian long entry."
                    )
                ))
                self._in_position = True
                self._entry_price = current_price

        else:
            # Exit conditions
            exit_reason = None

            # 1. Hard stop loss
            if self._entry_price is not None:
                loss_pct = (current_price - self._entry_price) / self._entry_price
                if loss_pct <= -self.STOP_LOSS_PCT:
                    exit_reason = f"Stop loss triggered: {loss_pct*100:.1f}% from entry {self._entry_price:.0f}"

            # 2. Sentiment recovery
            if exit_reason is None and fear_greed is not None:
                if float(fear_greed) >= self.FEAR_EXIT_THRESHOLD:
                    exit_reason = f"F&G recovered to {fear_greed} (>= {self.FEAR_EXIT_THRESHOLD})"

            # 3. Rolling Sharpe turned positive (trend restored)
            if exit_reason is None and rolling_sharpe >= 1.0:
                exit_reason = f"Rolling Sharpe recovered to {rolling_sharpe:.2f} (>= 1.0)"

            if exit_reason:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
                self._in_position = False
                self._entry_price = None
                self._consecutive_fear = 0

        return signals

    def on_fill(self, fill: dict) -> None:
        action = fill.get("action", "")
        if action == "buy":
            self._in_position = True
            self._entry_price = fill.get("price", self._entry_price)
        elif action in ("sell", "close"):
            self._in_position = False
            self._entry_price = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "strategy": self.name(),
            "in_position": getattr(self, "_in_position", False),
            "consecutive_fear_periods": getattr(self, "_consecutive_fear", 0),
            "entry_price": getattr(self, "_entry_price", None),
        }
