# Strategy: quant_primary_hyp_019_ethbtc_longonly_coint
# Written by agent quant_primary via write_strategy_code tool.

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class ETHBTCLongOnlyCoint(BaseStrategy):
    """
    Long-only cointegration strategy for ETH/BTC spread.
    
    Since margin/shorting is unavailable on Kraken US, we express the 
    mean-reversion thesis through allocation rotation:
    - When spread z-score < -1.5 (ETH cheap vs BTC): go long ETH
    - When spread z-score > +1.5 (ETH expensive vs BTC): go long BTC  
    - When |z| < 0.5: hold current position (hysteresis band)
    - When |z| between 0.5 and 1.5: close position, go to cash
    
    Hedge ratio and spread parameters estimated from rolling 90-day window.
    Half-life ~10 periods (40h) means trades should resolve in 2-5 days.
    """
    
    def __init__(self):
        self.hedge_ratio = 0.048307  # From cointegration analysis
        self.intercept = -1301.826425
        self.spread_mean = 0.0
        self.spread_std = 47.935856
        self.entry_z = 1.5
        self.exit_z = 0.5
        self.position_size = 0.45  # 45% of capital per position
        self.current_position = None  # 'ETH', 'BTC', or None
        self.lookback = 540  # ~90 days of 4h bars
        
    def name(self) -> str:
        return "quant_primary_hyp_019_ethbtc_longonly_coint"
    
    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]
    
    def _compute_spread_zscore(self, eth_prices: pd.Series, btc_prices: pd.Series) -> float:
        """Compute z-score of the cointegration spread."""
        if len(eth_prices) < 30 or len(btc_prices) < 30:
            return 0.0
        
        # Use rolling window for dynamic hedge ratio if enough data
        if len(eth_prices) >= self.lookback:
            window_eth = eth_prices.iloc[-self.lookback:]
            window_btc = btc_prices.iloc[-self.lookback:]
        else:
            window_eth = eth_prices
            window_btc = btc_prices
        
        # OLS hedge ratio: ETH = hedge_ratio * BTC + intercept + spread
        try:
            X = window_btc.values
            Y = window_eth.values
            X_mean = X.mean()
            Y_mean = Y.mean()
            hedge = np.sum((X - X_mean) * (Y - Y_mean)) / np.sum((X - X_mean) ** 2)
            intercept = Y_mean - hedge * X_mean
            
            # Compute spread using full available history
            spread = eth_prices - hedge * btc_prices - intercept
            
            # Z-score of latest spread value vs rolling window
            spread_window = spread.iloc[-self.lookback:] if len(spread) >= self.lookback else spread
            z = (spread.iloc[-1] - spread_window.mean()) / (spread_window.std() + 1e-10)
            return z
        except Exception:
            return 0.0
    
    def on_data(self, data: dict) -> list[Signal]:
        signals = []
        
        # We need both feeds
        if "ETH/USD:4h" not in data or "BTC/USD:4h" not in data:
            return signals
        
        eth_df = data["ETH/USD:4h"]
        btc_df = data["BTC/USD:4h"]
        
        if len(eth_df) < 30 or len(btc_df) < 30:
            return signals
        
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]
        
        # Align by taking minimum length
        min_len = min(len(eth_close), len(btc_close))
        eth_close = eth_close.iloc[-min_len:]
        btc_close = btc_close.iloc[-min_len:]
        
        z = self._compute_spread_zscore(eth_close, btc_close)
        
        # Decision logic with hysteresis
        if z < -self.entry_z:
            # Spread is very negative: ETH is cheap relative to BTC -> buy ETH
            if self.current_position == 'BTC':
                signals.append(Signal(
                    action="close", pair="BTC/USD", size_pct=1.0,
                    order_type="market",
                    rationale=f"Closing BTC to rotate to ETH. Spread z={z:.2f}"
                ))
            if self.current_position != 'ETH':
                signals.append(Signal(
                    action="buy", pair="ETH/USD", size_pct=self.position_size,
                    order_type="market",
                    rationale=f"ETH cheap vs BTC (z={z:.2f} < -{self.entry_z}). Long ETH for mean reversion."
                ))
                self.current_position = 'ETH'
                
        elif z > self.entry_z:
            # Spread is very positive: ETH expensive relative to BTC -> buy BTC
            if self.current_position == 'ETH':
                signals.append(Signal(
                    action="close", pair="ETH/USD", size_pct=1.0,
                    order_type="market",
                    rationale=f"Closing ETH to rotate to BTC. Spread z={z:.2f}"
                ))
            if self.current_position != 'BTC':
                signals.append(Signal(
                    action="buy", pair="BTC/USD", size_pct=self.position_size,
                    order_type="market",
                    rationale=f"BTC cheap vs ETH (z={z:.2f} > +{self.entry_z}). Long BTC for mean reversion."
                ))
                self.current_position = 'BTC'
                
        elif abs(z) < self.exit_z:
            # Spread near mean: close any position
            if self.current_position == 'ETH':
                signals.append(Signal(
                    action="close", pair="ETH/USD", size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread reverted to mean (z={z:.2f}). Taking profit."
                ))
                self.current_position = None
            elif self.current_position == 'BTC':
                signals.append(Signal(
                    action="close", pair="BTC/USD", size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread reverted to mean (z={z:.2f}). Taking profit."
                ))
                self.current_position = None
        
        # Between exit_z and entry_z: hold current position (hysteresis)
        return signals
    
    def on_fill(self, fill: dict) -> None:
        pass
    
    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "current_position": self.current_position,
            "strategy": "ETH/BTC long-only cointegration rotation"
        }
