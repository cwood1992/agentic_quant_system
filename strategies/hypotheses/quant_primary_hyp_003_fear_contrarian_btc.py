# Strategy: quant_primary_hyp_003_fear_contrarian_btc
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Contrarian BTC Strategy
Hypothesis: quant_primary_hyp_003_fear_contrarian_btc

Enter long BTC when:
  1. Fear & Greed Index < 25 (Extreme Fear zone)
  2. 14-day rolling Sharpe of BTC daily returns is positive (trend recovering)

Exit when:
  1. Fear & Greed Index > 45 (sentiment normalizing), OR
  2. 14-day rolling Sharpe turns negative (trend deteriorating), OR
  3. BTC price drops > 8% from entry (stop-loss)

Counterfactual: simple HODL BTC from same entry date
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearContrarianBTC(BaseStrategy):

    def __init__(self):
        self._in_position = False
        self._entry_price = None
        self._stop_loss_pct = 0.08  # 8% stop loss

    def name(self) -> str:
        return "quant_primary_hyp_003_fear_contrarian_btc"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:1d", "fear_greed_index:1d"]

    def _compute_rolling_sharpe(self, prices: pd.Series, window: int = 14) -> float:
        """Compute annualized rolling Sharpe over the last `window` daily returns."""
        if len(prices) < window + 1:
            return None
        returns = prices.pct_change().dropna()
        recent_returns = returns.iloc[-window:]
        if recent_returns.std() == 0:
            return 0.0
        # Annualize: sqrt(365)
        sharpe = (recent_returns.mean() / recent_returns.std()) * np.sqrt(365)
        return float(sharpe)

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        btc_data = data.get("BTC/USD:1d")
        fg_data = data.get("fear_greed_index:1d")

        if btc_data is None or fg_data is None:
            return signals

        if len(btc_data) < 16:  # need enough history
            return signals

        # Get current Fear & Greed value
        # fg_data is a DataFrame; latest value in 'close' or 'value' column
        fg_col = None
        for col in ["value", "close", "fear_greed_value"]:
            if col in fg_data.columns:
                fg_col = col
                break
        if fg_col is None:
            return signals

        current_fg = float(fg_data[fg_col].iloc[-1])

        # Compute rolling Sharpe
        btc_prices = btc_data["close"]
        rolling_sharpe = self._compute_rolling_sharpe(btc_prices, window=14)
        if rolling_sharpe is None:
            return signals

        current_price = float(btc_data["close"].iloc[-1])

        # --- Exit logic (check first) ---
        if self._in_position and self._entry_price is not None:
            drawdown = (current_price - self._entry_price) / self._entry_price

            # Stop loss
            if drawdown <= -self._stop_loss_pct:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Stop loss triggered: {drawdown:.1%} drawdown from entry ${self._entry_price:.0f}"
                ))
                self._in_position = False
                self._entry_price = None
                return signals

            # F&G normalized or trend deteriorating
            if current_fg > 45 or rolling_sharpe < 0:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=(
                        f"Exit: F&G={current_fg:.0f} (>45={current_fg > 45}), "
                        f"14d_sharpe={rolling_sharpe:.2f}"
                    )
                ))
                self._in_position = False
                self._entry_price = None
            return signals

        # --- Entry logic ---
        if not self._in_position:
            if current_fg < 25 and rolling_sharpe > 0:
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=0.90,  # 90% of capital, keep 10% buffer
                    order_type="market",
                    rationale=(
                        f"Entry: F&G={current_fg:.0f} (extreme fear), "
                        f"14d_sharpe={rolling_sharpe:.2f} (positive/recovering)"
                    )
                ))
                self._in_position = True
                self._entry_price = current_price

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            self._entry_price = fill.get("fill_price", self._entry_price)
            self._in_position = True
        elif fill.get("action") in ("close", "sell"):
            self._in_position = False
            self._entry_price = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
        }
