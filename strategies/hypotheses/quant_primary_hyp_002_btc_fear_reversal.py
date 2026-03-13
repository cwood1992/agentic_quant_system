# Strategy: quant_primary_hyp_002_btc_fear_reversal
# Written by agent quant_primary via write_strategy_code tool.

"""
BTC Fear Reversal Strategy
hypothesis_id: quant_primary_hyp_002_btc_fear_reversal

Thesis: When Fear & Greed Index is at Extreme Fear (<=20) AND BTC 14-day rolling
Sharpe (daily) has been deeply negative but starts inflecting upward, this marks
a high-probability fear bottom with outsized forward returns.

Entry: F&G <= 20 AND rolling Sharpe was negative for >= 5 of last 7 days AND
       rolling Sharpe today > rolling Sharpe 3 days ago (inflection up)
Exit: +10% TP, -7% SL, F&G > 40, or Sharpe turns negative again after positive entry
Max hold: 14 days
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BtcFearReversalStrategy(BaseStrategy):
    """
    BTC Fear & Greed contrarian reversal strategy.

    Fires only at genuine fear extremes with early Sharpe momentum inflection.
    Long BTC only. Tight stop. Expected trade frequency: 1-3 per 90 days.
    """

    FEAR_THRESHOLD = 20          # F&G <= this = Extreme Fear entry zone
    FEAR_EXIT_THRESHOLD = 40     # F&G >= this = sentiment normalized, exit
    SHARPE_WINDOW = 14           # 14-day rolling Sharpe window
    SHARPE_NEG_DAYS_MIN = 5      # Min days Sharpe must have been negative (of last 7)
    SHARPE_LOOKBACK_DAYS = 7     # Window to check for sustained negative Sharpe
    SHARPE_INFLECTION_DAYS = 3   # Sharpe must be higher than N days ago
    TAKE_PROFIT_PCT = 0.10       # 10% take profit
    STOP_LOSS_PCT = 0.07         # 7% stop loss
    MAX_HOLD_DAYS = 14           # Max 14 day hold
    SIZE_PCT = 0.50              # 50% of capital

    def __init__(self):
        self._position = False
        self._entry_price = None
        self._entry_bar = None

    def name(self) -> str:
        return "quant_primary_hyp_002_btc_fear_reversal"

    def required_feeds(self) -> list:
        return ["BTC/USD:1d"]

    def _compute_rolling_sharpe(self, prices: pd.Series, window: int = 14) -> pd.Series:
        """Compute annualized rolling Sharpe ratio from daily close prices."""
        returns = prices.pct_change().dropna()
        rolling_mean = returns.rolling(window=window, min_periods=window).mean()
        rolling_std = returns.rolling(window=window, min_periods=window).std()
        # Annualize: multiply mean by 365, std by sqrt(365)
        sharpe = (rolling_mean * 365) / (rolling_std * np.sqrt(365))
        return sharpe

    def on_data(self, data: dict) -> list:
        signals = []

        btc_df = data.get("BTC/USD:1d")
        if btc_df is None or len(btc_df) < self.SHARPE_WINDOW + self.SHARPE_LOOKBACK_DAYS + 5:
            return signals

        close_prices = btc_df["close"]
        current_price = close_prices.iloc[-1]
        current_bar = len(btc_df)

        # Compute rolling Sharpe series
        sharpe_series = self._compute_rolling_sharpe(close_prices, self.SHARPE_WINDOW)
        sharpe_series = sharpe_series.dropna()

        if len(sharpe_series) < self.SHARPE_LOOKBACK_DAYS + self.SHARPE_INFLECTION_DAYS:
            return signals

        # Get Fear & Greed — try to access from supplementary data
        # The data dict may contain fear_greed_index as a supplementary feed
        fear_greed = data.get("fear_greed_index")
        if fear_greed is not None:
            if isinstance(fear_greed, pd.DataFrame) and not fear_greed.empty:
                current_fg = float(fear_greed["value"].iloc[-1])
            elif isinstance(fear_greed, (int, float)):
                current_fg = float(fear_greed)
            elif hasattr(fear_greed, "iloc"):
                current_fg = float(fear_greed.iloc[-1])
            else:
                # Fear & Greed not available as historical series — cannot backtest properly
                # Use a proxy: if BTC rolling Sharpe is very deeply negative (<-5), treat as extreme fear
                current_fg = 10 if sharpe_series.iloc[-1] < -5 else 50
        else:
            # No fear greed data — use Sharpe proxy
            current_fg = 10 if sharpe_series.iloc[-1] < -5 else 50

        # === EXIT LOGIC ===
        if self._position:
            assert self._entry_price is not None
            assert self._entry_bar is not None

            pnl_pct = (current_price - self._entry_price) / self._entry_price
            bars_held = current_bar - self._entry_bar
            current_sharpe = sharpe_series.iloc[-1] if len(sharpe_series) > 0 else 0.0

            # Exit conditions
            take_profit = pnl_pct >= self.TAKE_PROFIT_PCT
            stop_loss = pnl_pct <= -self.STOP_LOSS_PCT
            max_hold = bars_held >= self.MAX_HOLD_DAYS
            sentiment_normal = current_fg >= self.FEAR_EXIT_THRESHOLD
            sharpe_turned_neg = current_sharpe < 0 and pnl_pct > 0  # Momentum dying

            if take_profit or stop_loss or max_hold or sentiment_normal or sharpe_turned_neg:
                reason = (
                    "take_profit" if take_profit else
                    "stop_loss" if stop_loss else
                    "max_hold_exceeded" if max_hold else
                    "sentiment_normalized" if sentiment_normal else
                    "sharpe_turned_negative"
                )
                rationale = (
                    f"Exit BTC long: {reason}. "
                    f"PnL={pnl_pct*100:.1f}%, bars_held={bars_held}, "
                    f"F&G={current_fg:.0f}, Sharpe={current_sharpe:.2f}"
                )
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=rationale
                ))
                self._position = False
                self._entry_price = None
                self._entry_bar = None
            return signals

        # === ENTRY LOGIC ===
        if not self._position:
            # Condition 1: Extreme Fear
            extreme_fear = current_fg <= self.FEAR_THRESHOLD

            # Condition 2: Sharpe deeply negative for sustained period
            recent_sharpe_vals = sharpe_series.iloc[-self.SHARPE_LOOKBACK_DAYS:]
            neg_days = (recent_sharpe_vals < 0).sum()
            sustained_negative = neg_days >= self.SHARPE_NEG_DAYS_MIN

            # Condition 3: Sharpe inflecting upward
            current_sharpe = sharpe_series.iloc[-1]
            past_sharpe = sharpe_series.iloc[-self.SHARPE_INFLECTION_DAYS - 1]
            sharpe_inflecting = current_sharpe > past_sharpe and current_sharpe > -2.0

            if extreme_fear and sustained_negative and sharpe_inflecting:
                rationale = (
                    f"Enter BTC long (fear reversal): "
                    f"F&G={current_fg:.0f} (Extreme Fear), "
                    f"Sharpe negative {neg_days}/{self.SHARPE_LOOKBACK_DAYS} days, "
                    f"Sharpe inflecting: {past_sharpe:.2f} -> {current_sharpe:.2f}. "
                    f"TP={self.TAKE_PROFIT_PCT*100:.0f}%, SL={self.STOP_LOSS_PCT*100:.0f}%"
                )
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=rationale
                ))
                self._position = True
                self._entry_price = current_price
                self._entry_bar = current_bar

        return signals

    def on_fill(self, fill: dict) -> None:
        """Update entry price on actual fill."""
        if fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            # Update entry price to actual fill price if available
            if "fill_price" in fill:
                self._entry_price = fill["fill_price"]

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """Return current strategy state for digest."""
        return {
            "position": "long_btc" if self._position else "flat",
            "entry_price": self._entry_price,
            "pnl_pct": (
                (portfolio_state.get("btc_price", self._entry_price or 0) - self._entry_price)
                / self._entry_price
                if self._position and self._entry_price
                else None
            ),
        }
