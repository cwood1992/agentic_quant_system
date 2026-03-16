# Strategy: quant_primary_hyp_020_fear_divergence_momentum
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Divergence Momentum Strategy
hypothesis_id: quant_primary_hyp_020_fear_divergence_momentum

Thesis: When Fear & Greed Index is in Extreme Fear (<=20) AND price has already
begun recovering (positive rolling momentum), the market is 'climbing the wall of
worry'. This divergence between sentiment and price is a bullish continuation signal.

Entry: F&G <= 20 AND both BTC and ETH have positive 7-day returns AND rolling
       14-day Sharpe > 1.0 for at least one of BTC/ETH
Exit:  F&G rises above 40 (fear regime ends) OR rolling Sharpe drops below 0 for
       both pairs (momentum exhaustion) OR trailing stop -12% from entry
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearDivergenceMomentumStrategy(BaseStrategy):
    """
    Enters a long basket of BTC and ETH when:
    1. Fear & Greed Index <= 20 (Extreme Fear)
    2. BTC 7-day return > 0 AND ETH 7-day return > 0 (price diverging upward)
    3. Rolling 14-day Sharpe > 1.0 for at least one pair (momentum confirmed)

    Exits when:
    1. F&G rises above 40 (fear regime ends — thesis realized)
    2. Rolling Sharpe < 0 for both pairs (momentum exhausted)
    3. Trailing stop: -12% from entry price on either position

    Sizing: 50% BTC / 50% ETH of available capital when triggered.
    """

    def __init__(self):
        self._in_position = False
        self._entry_prices = {}
        self._trailing_high = {}
        self._SHARPE_WINDOW = 14  # days
        self._SHARPE_MIN = 1.0
        self._FG_ENTRY_MAX = 20
        self._FG_EXIT_MIN = 40
        self._MOMENTUM_LOOKBACK = 7  # days in candles (1d)
        self._TRAILING_STOP = 0.12  # 12% trailing stop
        self._position_size = 0.48  # 48% each = 96% total (leaves buffer for fees)

    def name(self) -> str:
        return "quant_primary_hyp_020_fear_divergence_momentum"

    def required_feeds(self) -> list[str]:
        return [
            "BTC/USD:1d",
            "ETH/USD:1d",
            "fear_greed_index:1d",
        ]

    def _compute_rolling_sharpe(self, series: pd.Series, window: int) -> float:
        """Compute annualized Sharpe ratio over rolling window of daily returns."""
        if len(series) < window + 1:
            return 0.0
        returns = series.pct_change().dropna().tail(window)
        if returns.std() == 0:
            return 0.0
        sharpe = (returns.mean() / returns.std()) * np.sqrt(252)
        return float(sharpe)

    def _compute_7d_return(self, series: pd.Series) -> float:
        """7-day return using close prices."""
        closes = series.dropna()
        if len(closes) < 8:
            return 0.0
        return float((closes.iloc[-1] / closes.iloc[-8]) - 1)

    def _get_fear_greed(self, data: dict) -> float:
        """Extract latest Fear & Greed value from feed."""
        if "fear_greed_index:1d" not in data:
            return 50.0  # neutral if unavailable
        fg_df = data["fear_greed_index:1d"]
        if fg_df is None or len(fg_df) == 0:
            return 50.0
        # value column expected
        if "value" in fg_df.columns:
            return float(fg_df["value"].iloc[-1])
        elif "close" in fg_df.columns:
            return float(fg_df["close"].iloc[-1])
        return 50.0

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        # Extract data
        btc_df = data.get("BTC/USD:1d")
        eth_df = data.get("ETH/USD:1d")

        if btc_df is None or eth_df is None:
            return signals
        if len(btc_df) < 20 or len(eth_df) < 20:
            return signals

        btc_close = btc_df["close"]
        eth_close = eth_df["close"]

        fear_greed = self._get_fear_greed(data)

        # Compute signals
        btc_sharpe = self._compute_rolling_sharpe(btc_close, self._SHARPE_WINDOW)
        eth_sharpe = self._compute_rolling_sharpe(eth_close, self._SHARPE_WINDOW)
        btc_7d_return = self._compute_7d_return(btc_close)
        eth_7d_return = self._compute_7d_return(eth_close)

        current_btc = float(btc_close.iloc[-1])
        current_eth = float(eth_close.iloc[-1])

        # Update trailing highs if in position
        if self._in_position:
            if "BTC/USD" in self._trailing_high:
                self._trailing_high["BTC/USD"] = max(
                    self._trailing_high["BTC/USD"], current_btc
                )
            if "ETH/USD" in self._trailing_high:
                self._trailing_high["ETH/USD"] = max(
                    self._trailing_high["ETH/USD"], current_eth
                )

        # === EXIT LOGIC ===
        if self._in_position:
            # Condition 1: F&G exits fear regime — thesis realized
            fg_exit = fear_greed >= self._FG_EXIT_MIN

            # Condition 2: Momentum exhaustion — both Sharpes negative
            momentum_exhausted = btc_sharpe < 0 and eth_sharpe < 0

            # Condition 3: Trailing stop on either position
            btc_trailing_breach = (
                "BTC/USD" in self._trailing_high
                and current_btc
                < self._trailing_high["BTC/USD"] * (1 - self._TRAILING_STOP)
            )
            eth_trailing_breach = (
                "ETH/USD" in self._trailing_high
                and current_eth
                < self._trailing_high["ETH/USD"] * (1 - self._TRAILING_STOP)
            )
            trailing_stop_hit = btc_trailing_breach or eth_trailing_breach

            if fg_exit or momentum_exhausted or trailing_stop_hit:
                reason_parts = []
                if fg_exit:
                    reason_parts.append(f"F&G={fear_greed:.0f} >= {self._FG_EXIT_MIN} (thesis realized)")
                if momentum_exhausted:
                    reason_parts.append(f"Momentum exhausted: BTC_sharpe={btc_sharpe:.2f}, ETH_sharpe={eth_sharpe:.2f}")
                if trailing_stop_hit:
                    reason_parts.append(f"Trailing stop hit: BTC_ts={btc_trailing_breach}, ETH_ts={eth_trailing_breach}")
                rationale = " | ".join(reason_parts)

                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"EXIT: {rationale}",
                ))
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"EXIT: {rationale}",
                ))
                self._in_position = False
                self._entry_prices = {}
                self._trailing_high = {}

        # === ENTRY LOGIC ===
        elif not self._in_position:
            # All three conditions must hold
            extreme_fear = fear_greed <= self._FG_ENTRY_MAX
            price_diverging = btc_7d_return > 0 and eth_7d_return > 0
            momentum_confirmed = btc_sharpe > self._SHARPE_MIN or eth_sharpe > self._SHARPE_MIN

            if extreme_fear and price_diverging and momentum_confirmed:
                rationale = (
                    f"ENTRY: F&G={fear_greed:.0f} (Extreme Fear), "
                    f"BTC_7d={btc_7d_return:.1%}, ETH_7d={eth_7d_return:.1%}, "
                    f"BTC_sharpe={btc_sharpe:.2f}, ETH_sharpe={eth_sharpe:.2f}"
                )
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self._position_size,
                    order_type="market",
                    rationale=rationale,
                ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self._position_size,
                    order_type="market",
                    rationale=rationale,
                ))
                self._in_position = True
                self._entry_prices = {"BTC/USD": current_btc, "ETH/USD": current_eth}
                self._trailing_high = {"BTC/USD": current_btc, "ETH/USD": current_eth}

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track fills for position management."""
        pass

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """Report strategy state for digest."""
        return {
            "in_position": self._in_position,
            "entry_prices": self._entry_prices,
            "trailing_high": self._trailing_high,
        }
