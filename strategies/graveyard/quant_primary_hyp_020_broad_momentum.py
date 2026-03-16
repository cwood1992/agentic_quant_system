# Strategy: quant_primary_hyp_020_broad_momentum
# Written by agent quant_primary via write_strategy_code tool.

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BroadMomentumStrategy(BaseStrategy):
    """
    Multi-asset momentum strategy using rolling Sharpe as signal.
    
    Goes long the asset with the highest rolling Sharpe ratio when it exceeds
    a threshold. Uses a 14-day (~84 4h-bars) rolling window. Position sizing
    is fixed at 40% of capital. Exits when rolling Sharpe drops below exit
    threshold or another asset becomes dominant.
    
    This is a rotational momentum strategy: always holds at most one position
    in the highest-momentum asset.
    """
    
    def __init__(self):
        self.sharpe_window = 84  # 14 days of 4h bars
        self.entry_sharpe = 1.5  # Minimum rolling Sharpe to enter
        self.exit_sharpe = 0.0   # Exit when Sharpe drops to zero
        self.position_size = 0.40
        self.current_pair = None
        self.pairs = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]
    
    def name(self) -> str:
        return "quant_primary_hyp_020_broad_momentum"
    
    def required_feeds(self) -> list[str]:
        return [f"{p}:4h" for p in self.pairs]
    
    def _rolling_sharpe(self, close: pd.Series, window: int) -> float:
        """Compute annualized rolling Sharpe of returns."""
        if len(close) < window + 1:
            return 0.0
        returns = close.pct_change().dropna()
        if len(returns) < window:
            return 0.0
        recent = returns.iloc[-window:]
        mean_ret = recent.mean()
        std_ret = recent.std()
        if std_ret == 0 or pd.isna(std_ret):
            return 0.0
        # Annualize: 6 bars/day * 365 days = 2190 bars/year
        annualization = np.sqrt(2190)
        return (mean_ret / std_ret) * annualization
    
    def on_data(self, data: dict) -> list[Signal]:
        signals = []
        
        # Compute rolling Sharpe for each available pair
        sharpe_scores = {}
        for pair in self.pairs:
            feed_key = f"{pair}:4h"
            if feed_key in data and len(data[feed_key]) >= self.sharpe_window + 1:
                close = data[feed_key]["close"]
                sharpe = self._rolling_sharpe(close, self.sharpe_window)
                sharpe_scores[pair] = sharpe
        
        if not sharpe_scores:
            return signals
        
        # Find best momentum asset
        best_pair = max(sharpe_scores, key=sharpe_scores.get)
        best_sharpe = sharpe_scores[best_pair]
        
        # Current position Sharpe (if we have one)
        current_sharpe = sharpe_scores.get(self.current_pair, -999)
        
        # Exit logic: current position Sharpe below exit threshold
        if self.current_pair and current_sharpe < self.exit_sharpe:
            signals.append(Signal(
                action="close", pair=self.current_pair, size_pct=1.0,
                order_type="market",
                rationale=f"Momentum faded: {self.current_pair} Sharpe={current_sharpe:.2f} < {self.exit_sharpe}"
            ))
            self.current_pair = None
        
        # Rotation logic: switch to better asset if it's significantly better
        if self.current_pair and best_pair != self.current_pair:
            # Only rotate if new asset is meaningfully better (>1.0 Sharpe advantage)
            if best_sharpe > current_sharpe + 1.0 and best_sharpe >= self.entry_sharpe:
                signals.append(Signal(
                    action="close", pair=self.current_pair, size_pct=1.0,
                    order_type="market",
                    rationale=f"Rotating from {self.current_pair} (Sharpe={current_sharpe:.2f}) to {best_pair} (Sharpe={best_sharpe:.2f})"
                ))
                signals.append(Signal(
                    action="buy", pair=best_pair, size_pct=self.position_size,
                    order_type="market",
                    rationale=f"Best momentum: {best_pair} Sharpe={best_sharpe:.2f}"
                ))
                self.current_pair = best_pair
        
        # Entry logic: no position and best asset exceeds threshold
        if self.current_pair is None and best_sharpe >= self.entry_sharpe:
            signals.append(Signal(
                action="buy", pair=best_pair, size_pct=self.position_size,
                order_type="market",
                rationale=f"Entering momentum: {best_pair} Sharpe={best_sharpe:.2f} (threshold={self.entry_sharpe})"
            ))
            self.current_pair = best_pair
        
        return signals
    
    def on_fill(self, fill: dict) -> None:
        pass
    
    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "current_pair": self.current_pair,
            "strategy": "Broad momentum rotation"
        }
