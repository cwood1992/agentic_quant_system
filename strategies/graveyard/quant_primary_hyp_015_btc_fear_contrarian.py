# Strategy: quant_primary_hyp_015_btc_fear_contrarian
# Written by agent quant_primary via write_strategy_code tool.

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BTCFearContrarian(BaseStrategy):
    """
    BTC contrarian strategy based on Fear & Greed Index.
    
    Logic:
    - Buy BTC when Fear & Greed Index drops below 20 (Extreme Fear) AND
      BTC 4h return is positive (price stabilizing/bouncing)
    - Sell when Fear & Greed rises above 50 (neutral) OR
      when BTC drops more than 5% from entry (stop loss)
    - Only one position at a time
    - 70% of capital per position
    
    Edge thesis: Extreme fear historically correlates with local bottoms.
    The confirmation filter (positive recent return) avoids catching falling knives.
    """

    def __init__(self):
        self.fear_threshold = 20      # enter below this
        self.greed_exit = 50          # exit above this
        self.position_size = 0.70
        self.in_position = False
        self.entry_price = None
        self.stop_loss_pct = 0.05     # 5% stop loss
        self.bounce_lookback = 6      # 6 x 4h = 24h lookback for bounce confirmation

    def name(self) -> str:
        return "quant_primary_hyp_015_btc_fear_contrarian"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h"]

    def on_data(self, data: dict) -> list[Signal]:
        signals = []
        pair = "BTC/USD"
        
        if pair not in data:
            return signals
            
        df = data[pair]
        
        if len(df) < self.bounce_lookback + 2:
            return signals
        
        close = df["close"]
        current_price = close.iloc[-1]
        
        # Get Fear & Greed index if available
        fear_greed = None
        if "fear_greed" in df.columns:
            fg_vals = df["fear_greed"].dropna()
            if len(fg_vals) > 0:
                fear_greed = fg_vals.iloc[-1]
        
        # Also check supplementary data format
        if fear_greed is None and "fear_greed_index" in data:
            fg_data = data["fear_greed_index"]
            if isinstance(fg_data, (int, float)):
                fear_greed = fg_data
            elif isinstance(fg_data, pd.DataFrame) and len(fg_data) > 0:
                fear_greed = fg_data.iloc[-1].get("value", None)
        
        # If no F&G data, can't trade this strategy
        if fear_greed is None:
            return signals
        
        # Bounce confirmation: is price higher than 24h ago?
        price_24h_ago = close.iloc[-self.bounce_lookback] if len(close) >= self.bounce_lookback else close.iloc[0]
        bounce_confirmed = current_price > price_24h_ago
        
        # Recent 4h return positive
        recent_return = (close.iloc[-1] / close.iloc[-2]) - 1.0 if len(close) >= 2 else 0
        
        if not self.in_position:
            # Entry: extreme fear + bounce confirmation
            if fear_greed < self.fear_threshold and bounce_confirmed and recent_return > 0:
                self.in_position = True
                self.entry_price = current_price
                signals.append(Signal(
                    action="buy",
                    pair=pair,
                    size_pct=self.position_size,
                    order_type="market",
                    rationale=f"Fear contrarian BUY: F&G={fear_greed:.0f}, price bounce confirmed ({current_price:.0f} > {price_24h_ago:.0f}), recent_ret={recent_return:.4f}"
                ))
        else:
            # Exit conditions
            exit_reason = None
            
            # Stop loss
            if self.entry_price and current_price < self.entry_price * (1 - self.stop_loss_pct):
                exit_reason = f"STOP LOSS: price={current_price:.0f}, entry={self.entry_price:.0f}, loss={((current_price/self.entry_price)-1)*100:.1f}%"
            
            # Fear & Greed recovery to neutral
            elif fear_greed >= self.greed_exit:
                exit_reason = f"F&G RECOVERY: F&G={fear_greed:.0f} >= {self.greed_exit}, taking profit"
            
            if exit_reason:
                self.in_position = False
                self.entry_price = None
                signals.append(Signal(
                    action="close",
                    pair=pair,
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Fear contrarian EXIT: {exit_reason}"
                ))
        
        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy":
            self.in_position = True
            self.entry_price = fill.get("price", self.entry_price)
        elif fill.get("action") in ("sell", "close"):
            self.in_position = False
            self.entry_price = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self.in_position,
            "entry_price": self.entry_price,
            "strategy": "fear_contrarian_F&G<20_exit>50"
        }
