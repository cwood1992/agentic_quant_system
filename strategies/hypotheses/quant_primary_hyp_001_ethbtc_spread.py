# Strategy: quant_primary_hyp_001_ethbtc_spread
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Spread Mean Reversion Strategy
Hypothesis: hyp_001_ethbtc_spread
Cointegration-based pairs trade on ETH/USD and BTC/USD.

Hedge ratio: ~0.0484 (ETH = 0.0484 * BTC - 1303.75)
Half-life: ~11.1 x 4h periods (~44h)
Entry: z-score crosses ±1.5 sigma
Exit: z-score reverts to ±0.25 sigma (near mean)
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcSpreadMeanReversion(BaseStrategy):
    """
    Pairs trade between ETH/USD and BTC/USD using a cointegrated spread.
    Long ETH / Short BTC (synthetic) when ETH is undervalued relative to BTC.
    Implemented as a directional ETH trade since we cannot short BTC.

    Since we cannot go short, we trade directionally:
    - ETH z-score < -ENTRY_Z  → ETH undervalued vs BTC → BUY ETH
    - ETH z-score > +ENTRY_Z  → ETH overvalued vs BTC → SELL (close) ETH
    - Mean reversion to within EXIT_Z → close position
    """

    HEDGE_RATIO = 0.0484
    INTERCEPT = -1303.75
    LOOKBACK = 120       # candles for rolling spread stats (~20 days on 4h)
    ENTRY_Z = 1.5        # entry threshold (sigma)
    EXIT_Z = 0.25        # exit threshold (near mean)
    POSITION_SIZE = 0.40 # 40% of capital per trade

    def name(self) -> str:
        return "quant_primary_hyp_001_ethbtc_spread"

    def required_feeds(self) -> list:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_zscore(self, eth_prices: pd.Series, btc_prices: pd.Series) -> float:
        """Compute current z-score of the ETH/BTC spread."""
        spread = eth_prices - (self.HEDGE_RATIO * btc_prices + self.INTERCEPT)
        if len(spread) < self.LOOKBACK:
            return 0.0
        rolling = spread.iloc[-self.LOOKBACK:]
        mean = rolling.mean()
        std = rolling.std()
        if std < 1e-8:
            return 0.0
        current_spread = spread.iloc[-1]
        return float((current_spread - mean) / std)

    def on_data(self, data: dict) -> list:
        eth_df = data.get("ETH/USD:4h")
        btc_df = data.get("BTC/USD:4h")

        if eth_df is None or btc_df is None:
            return []
        if len(eth_df) < self.LOOKBACK + 5 or len(btc_df) < self.LOOKBACK + 5:
            return []

        # Align on common index
        eth_close = eth_df["close"]
        btc_close = btc_df["close"]

        # Use last LOOKBACK+10 candles, aligned
        n = min(len(eth_close), len(btc_close))
        eth_close = eth_close.iloc[-n:]
        btc_close = btc_close.iloc[-n:]

        zscore = self._compute_zscore(eth_close, btc_close)

        # Check current position state via on_cycle (tracked via instance var)
        in_long = getattr(self, "_in_long", False)

        signals = []

        if not in_long:
            # Look for entry: ETH significantly undervalued vs BTC
            if zscore < -self.ENTRY_Z:
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"ETH/BTC spread z-score={zscore:.2f} < -{self.ENTRY_Z}: ETH undervalued vs BTC"
                ))
                self._in_long = True
        else:
            # Look for exit: spread has mean-reverted
            if zscore > -self.EXIT_Z:
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC spread z-score={zscore:.2f} reverted above -{self.EXIT_Z}: closing long ETH"
                ))
                self._in_long = False

        return signals

    def on_fill(self, fill: dict) -> None:
        action = fill.get("action", "")
        if action == "buy":
            self._in_long = True
        elif action in ("sell", "close"):
            self._in_long = False

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        eth_close = None
        btc_close = None
        return {
            "strategy": self.name(),
            "in_long": getattr(self, "_in_long", False),
        }
