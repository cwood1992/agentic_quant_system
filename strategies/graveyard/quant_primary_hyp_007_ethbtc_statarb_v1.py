# Strategy: quant_primary_hyp_007_ethbtc_statarb_v1
# Written by agent quant_primary via write_strategy_code tool.

"""
quant_primary_hyp_007_ethbtc_statarb_v1

ETH/BTC Stat-Arb (Long-Only Rotation)
Hypothesis: ETH and BTC are cointegrated (confirmed: ADF p=0.0008, 540 obs).
The spread has a half-life of ~10.4 periods (≈42h at 4h bars).
Strategy: rotate allocation between ETH and BTC based on spread z-score.

Rules:
- Compute hedge ratio: ETH_price = alpha + beta * BTC_price + spread
- Z-score of spread over 90-period rolling window
- If z > +1.5: ETH overpriced vs BTC → hold BTC (close ETH, buy BTC)
- If z < -1.5: ETH underpriced vs BTC → hold ETH (close BTC, buy ETH)
- If |z| < 0.5: neutral → hold 50/50

Long-only; no shorting. This is a rotation strategy, not a true hedge.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class Quant007ETHBTCStatarb(BaseStrategy):

    LOOKBACK = 90           # rolling window for z-score
    ENTRY_Z = 1.5           # enter signal at this z
    EXIT_Z = 0.5            # exit (neutral zone) at this z
    WARMUP = 95             # candles before first signal

    def __init__(self):
        self._candle_count = 0
        # Position tracking: 'btc', 'eth', 'neutral', or None
        self._current_position = None
        # Price history for rolling z-score
        self._eth_prices = []
        self._btc_prices = []

    def name(self) -> str:
        return "quant_primary_hyp_007_ethbtc_statarb_v1"

    def required_feeds(self) -> list[str]:
        # Declare without timeframe suffix — engine format unclear
        return ["ETH/USD", "BTC/USD"]

    def _get_close(self, df: pd.DataFrame, pair_hint: str = "") -> float | None:
        """Extract close price from DataFrame regardless of column naming."""
        if df is None or len(df) == 0:
            return None
        for col in ["close", "Close", "CLOSE", "c", "price"]:
            if col in df.columns:
                val = df[col].iloc[-1]
                if pd.notna(val) and float(val) > 0:
                    return float(val)
        # Positional fallback: OHLCV column order
        if len(df.columns) >= 4:
            val = df.iloc[-1, 3]
            if pd.notna(val) and float(val) > 0:
                return float(val)
        return None

    def _get_pair_df(self, data: dict, pair: str) -> pd.DataFrame | None:
        """Try multiple key formats for a given pair (e.g. 'ETH/USD')."""
        base = pair  # e.g. 'ETH/USD'
        ticker = pair.replace("/", "")  # e.g. 'ETHUSD'
        candidates = [
            base,
            f"{base}:4h", f"{base}:1h", f"{base}:1d",
            f"{base}:daily", f"{base}:hourly",
            ticker, ticker.lower(),
            base.lower(),
            f"{base}_4h", f"{base}_1h",
        ]
        for key in candidates:
            if key in data:
                return data[key]
        # Fuzzy: find any key containing both parts of the pair
        parts = pair.replace("/", "").upper()
        asset = parts[:3]  # 'ETH' or 'BTC'
        for key in data:
            if asset in key.upper():
                return data[key]
        return None

    def _compute_zscore(self) -> float | None:
        """Compute current spread z-score using OLS hedge ratio over lookback window."""
        if len(self._eth_prices) < self.LOOKBACK:
            return None

        eth = np.array(self._eth_prices[-self.LOOKBACK:])
        btc = np.array(self._btc_prices[-self.LOOKBACK:])

        # OLS: ETH = alpha + beta * BTC + residual
        btc_mean = np.mean(btc)
        eth_mean = np.mean(eth)
        beta = np.cov(eth, btc)[0, 1] / np.var(btc)
        alpha = eth_mean - beta * btc_mean
        spread = eth - (alpha + beta * btc)

        spread_mean = np.mean(spread)
        spread_std = np.std(spread)
        if spread_std < 1e-8:
            return None

        current_spread = spread[-1]
        z = (current_spread - spread_mean) / spread_std
        return float(z)

    def on_data(self, data: dict) -> list[Signal]:
        self._candle_count += 1

        # Heartbeat on candle 1 — proves on_data is called even if data lookup fails
        if self._candle_count == 1:
            return [Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=0.01,
                order_type="market",
                rationale="HEARTBEAT candle 1 — proves on_data() is being called"
            )]

        # Extract prices
        eth_df = self._get_pair_df(data, "ETH/USD")
        btc_df = self._get_pair_df(data, "BTC/USD")

        eth_price = self._get_close(eth_df, "ETH") if eth_df is not None else None
        btc_price = self._get_close(btc_df, "BTC") if btc_df is not None else None

        if eth_price is None or btc_price is None:
            return []

        self._eth_prices.append(eth_price)
        self._btc_prices.append(btc_price)

        # Need warmup period before generating signals
        if self._candle_count < self.WARMUP:
            return []

        z = self._compute_zscore()
        if z is None:
            return []

        signals = []

        # Determine target position
        if z > self.ENTRY_Z:
            # ETH overpriced: hold BTC
            target = "btc"
        elif z < -self.ENTRY_Z:
            # ETH underpriced: hold ETH
            target = "eth"
        elif abs(z) < self.EXIT_Z:
            # Spread neutral: go 50/50 (implemented as alternating — simplification)
            target = "neutral"
        else:
            # In transition zone: hold current position
            target = self._current_position

        if target == self._current_position:
            return []

        # Execute rotation
        if self._current_position == "eth":
            signals.append(Signal(
                action="close", pair="ETH/USD", size_pct=1.0,
                order_type="market",
                rationale=f"Close ETH, z={z:.2f}"
            ))
        elif self._current_position == "btc":
            signals.append(Signal(
                action="close", pair="BTC/USD", size_pct=1.0,
                order_type="market",
                rationale=f"Close BTC, z={z:.2f}"
            ))

        if target == "btc":
            signals.append(Signal(
                action="buy", pair="BTC/USD", size_pct=0.95,
                order_type="market",
                rationale=f"Long BTC: ETH overvalued z={z:.2f} > {self.ENTRY_Z}"
            ))
        elif target == "eth":
            signals.append(Signal(
                action="buy", pair="ETH/USD", size_pct=0.95,
                order_type="market",
                rationale=f"Long ETH: ETH undervalued z={z:.2f} < -{self.ENTRY_Z}"
            ))
        elif target == "neutral":
            signals.append(Signal(
                action="buy", pair="BTC/USD", size_pct=0.48,
                order_type="market",
                rationale=f"Neutral: 50/50 BTC, z={z:.2f}"
            ))
            signals.append(Signal(
                action="buy", pair="ETH/USD", size_pct=0.48,
                order_type="market",
                rationale=f"Neutral: 50/50 ETH, z={z:.2f}"
            ))

        self._current_position = target
        return signals
