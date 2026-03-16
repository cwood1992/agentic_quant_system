# Strategy: quant_primary_hyp_014_btc_trend_sma
# Written by agent quant_primary via write_strategy_code tool.

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BTCTrendSMA(BaseStrategy):
    """
    BTC trend-following strategy using SMA crossover with volatility filter.
    
    Logic:
    - Uses 20-period and 60-period SMA on 4h candles (~3.3 day and ~10 day)
    - Buy when fast SMA crosses above slow SMA AND current vol is below 2x average vol
    - Sell when fast SMA crosses below slow SMA
    - Position size: 80% of capital (single position, long only)
    
    Rationale: Simple trend capture. In a trending market (which rolling Sharpe 
    suggests we may be entering), SMA crossover captures the bulk of moves. 
    Vol filter avoids entering during spikes that tend to mean-revert.
    """

    def __init__(self):
        self.fast_period = 20   # ~3.3 days at 4h
        self.slow_period = 60   # ~10 days at 4h
        self.vol_period = 60    # volatility lookback
        self.vol_multiplier = 2.0  # max vol threshold vs average
        self.position_size = 0.80
        self.in_position = False

    def name(self) -> str:
        return "quant_primary_hyp_014_btc_trend_sma"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h"]

    def on_data(self, data: dict) -> list[Signal]:
        signals = []
        pair = "BTC/USD"
        
        if pair not in data:
            return signals
            
        df = data[pair]
        
        if len(df) < self.slow_period + 5:
            return signals
        
        close = df["close"]
        
        # Compute SMAs
        fast_sma = close.rolling(window=self.fast_period).mean()
        slow_sma = close.rolling(window=self.slow_period).mean()
        
        # Compute volatility filter
        returns = close.pct_change()
        current_vol = returns.rolling(window=self.vol_period).std()
        avg_vol = current_vol.mean()
        
        # Current values
        curr_fast = fast_sma.iloc[-1]
        curr_slow = slow_sma.iloc[-1]
        prev_fast = fast_sma.iloc[-2]
        prev_slow = slow_sma.iloc[-2]
        curr_vol_val = current_vol.iloc[-1]
        
        if pd.isna(curr_fast) or pd.isna(curr_slow) or pd.isna(curr_vol_val):
            return signals
        
        # Crossover detection
        fast_crossed_above = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
        fast_crossed_below = (prev_fast >= prev_slow) and (curr_fast < curr_slow)
        fast_above_slow = curr_fast > curr_slow
        
        # Vol filter: only enter if vol is reasonable
        vol_ok = curr_vol_val < (avg_vol * self.vol_multiplier)
        
        if not self.in_position:
            # Entry: fast crosses above slow with acceptable vol
            if fast_crossed_above and vol_ok:
                self.in_position = True
                signals.append(Signal(
                    action="buy",
                    pair=pair,
                    size_pct=self.position_size,
                    order_type="market",
                    rationale=f"SMA crossover BUY: fast({self.fast_period})={curr_fast:.0f} > slow({self.slow_period})={curr_slow:.0f}, vol={curr_vol_val:.4f}"
                ))
        else:
            # Exit: fast crosses below slow
            if fast_crossed_below:
                self.in_position = False
                signals.append(Signal(
                    action="close",
                    pair=pair,
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"SMA crossover SELL: fast({self.fast_period})={curr_fast:.0f} < slow({self.slow_period})={curr_slow:.0f}"
                ))
        
        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy":
            self.in_position = True
        elif fill.get("action") in ("sell", "close"):
            self.in_position = False

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self.in_position,
            "strategy": "SMA_20_60_vol_filter"
        }
