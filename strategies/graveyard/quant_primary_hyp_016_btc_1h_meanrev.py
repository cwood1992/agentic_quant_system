# Strategy: quant_primary_hyp_016_btc_1h_meanrev
# Written by agent quant_primary via write_strategy_code tool.

import pandas as pd
from strategies.base import BaseStrategy, Signal

class BTC1hMeanReversion(BaseStrategy):
    """
    BTC 1h mean-reversion strategy (long-only).
    
    Thesis: BTC 1h returns show significant negative lag-1 autocorrelation (-0.142, 
    Ljung-Box p=0.048). After a down hour, the next hour tends to reverse upward.
    
    Logic:
    - Buy when the last candle closes red (negative return) AND the magnitude 
      exceeds a threshold (avoiding noise on tiny moves)
    - Close position when the last candle closes green (positive return)
    - Position size: 40% of capital per trade (leaves room for slippage, 
      ensures above Kraken minimum with $500 capital)
    - Only one position at a time
    
    Long-only implementation: we can only capture the "buy after down, 
    sell after up" half of the mean-reversion signal.
    """
    
    def __init__(self):
        self._position_open = False
        self._entry_price = None
        self._candles_since_entry = 0
        self._min_return_threshold = -0.002  # Only buy after >= 0.2% drop
        self._max_hold_periods = 6  # Max hold 6 hours to avoid drift
    
    def name(self) -> str:
        return "quant_primary_hyp_016_btc_1h_meanrev"
    
    def required_feeds(self) -> list[str]:
        return ["BTC/USD:1h"]
    
    def on_data(self, data: dict) -> list[Signal]:
        signals = []
        
        pair = data.get("pair", "BTC/USD")
        candles = data.get("candles_so_far")
        
        if candles is None or len(candles) < 3:
            return signals
        
        # Get current and previous candle data
        current = candles.iloc[-1]
        prev = candles.iloc[-2]
        
        current_open = float(current["open"])
        current_close = float(current["close"])
        current_return = (current_close - current_open) / current_open if current_open > 0 else 0
        
        prev_open = float(prev["open"])
        prev_close = float(prev["close"])
        prev_return = (prev_close - prev_open) / prev_open if prev_open > 0 else 0
        
        # Track holding period
        if self._position_open:
            self._candles_since_entry += 1
        
        # EXIT LOGIC: Close if current candle is green OR max hold exceeded
        if self._position_open:
            if current_return > 0.001:  # Close on meaningful green candle
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Mean-rev exit: green candle ({current_return:.4f}), held {self._candles_since_entry} periods"
                ))
                self._position_open = False
                self._entry_price = None
                self._candles_since_entry = 0
                return signals
            
            if self._candles_since_entry >= self._max_hold_periods:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Mean-rev timeout: held {self._candles_since_entry} periods, forcing exit"
                ))
                self._position_open = False
                self._entry_price = None
                self._candles_since_entry = 0
                return signals
        
        # ENTRY LOGIC: Buy after a red candle exceeding threshold
        if not self._position_open:
            if current_return <= self._min_return_threshold:
                # Additional filter: check that we're not in a strong multi-candle downtrend
                # (mean-reversion works best after isolated dips, not cascading selloffs)
                if len(candles) >= 4:
                    prev2_close = float(candles.iloc[-3]["close"])
                    prev2_open = float(candles.iloc[-3]["open"])
                    prev2_return = (prev2_close - prev2_open) / prev2_open if prev2_open > 0 else 0
                    
                    # Don't enter if previous 2 candles were also red (cascading down)
                    if prev_return < self._min_return_threshold and prev2_return < self._min_return_threshold:
                        return signals  # Skip — likely trending down, not mean-reverting
                
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=0.40,
                    order_type="market",
                    rationale=f"Mean-rev entry: red candle ({current_return:.4f}), expecting 1h reversal"
                ))
                self._position_open = True
                self._entry_price = current_close
                self._candles_since_entry = 0
        
        return signals
    
    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "close":
            self._position_open = False
            self._entry_price = None
            self._candles_since_entry = 0
        elif fill.get("action") == "buy":
            self._position_open = True
            self._entry_price = fill.get("price", 0)
            self._candles_since_entry = 0
