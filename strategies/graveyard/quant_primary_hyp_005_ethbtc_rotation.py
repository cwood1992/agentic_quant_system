# Strategy: quant_primary_hyp_005_ethbtc_rotation
# Written by agent quant_primary via write_strategy_code tool.

"""
quant_primary_hyp_005_ethbtc_rotation
ETH/BTC ratio mean-reversion — v5 (maximally defensive, multi-key diagnostic)

This version attempts every plausible key format for the data dict to diagnose
the persistent 0-trade failure across hyp_001 through hyp_004. It also uses
simple close-price access with column-name fallbacks.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcRotationV5(BaseStrategy):
    """
    Rotate between ETH and BTC based on their price ratio z-score.

    Entry:  When ratio z-score < -2.0  → buy ETH (ETH is cheap relative to BTC)
    Exit:   When |z-score| < 0.5       → close ETH, buy BTC (default hold)

    This version uses a maximally defensive data-access pattern to work regardless
    of whether the backtest engine passes data keyed by 'ETH/USD:4h', 'ETH/USD',
    'ETH/USD_4h', etc., and regardless of column capitalisation.
    """

    WINDOW = 60          # rolling window for ratio z-score (~10 days at 4h)
    ENTRY_Z = -2.0       # buy ETH when ratio z-score is below this
    EXIT_Z_ABS = 0.5     # close ETH when |z-score| falls inside this band
    SIZE_PCT = 0.95      # 95% of capital per position

    # All key formats we will attempt, in priority order
    ETH_KEY_CANDIDATES = [
        "ETH/USD:4h", "ETH/USD", "ETH/USD_4h", "ETHUSD:4h", "ETHUSD", "eth/usd:4h", "eth/usd"
    ]
    BTC_KEY_CANDIDATES = [
        "BTC/USD:4h", "BTC/USD", "BTC/USD_4h", "BTCUSD:4h", "BTCUSD", "btc/usd:4h", "btc/usd"
    ]

    def __init__(self):
        self._position = "btc"   # default hold: BTC
        self._ratios = []
        self._candle_count = 0
        self._data_keys_found = None   # cache discovered keys after first successful access

    def name(self) -> str:
        return "quant_primary_hyp_005_ethbtc_rotation"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_close(df: pd.DataFrame) -> float | None:
        """Return latest close price regardless of column capitalisation."""
        for col in ["close", "Close", "CLOSE", "c", "C"]:
            if col in df.columns:
                return float(df[col].iloc[-1])
        # If nothing matched, try the last numeric column
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if numeric_cols:
            return float(df[numeric_cols[-1]].iloc[-1])
        return None

    @staticmethod
    def _find_key(data: dict, candidates: list[str]):
        """Return first matching key from candidates, or None."""
        for k in candidates:
            if k in data:
                return k
        # Fallback: case-insensitive partial match
        data_keys_lower = {dk.lower(): dk for dk in data.keys()}
        for c in candidates:
            if c.lower() in data_keys_lower:
                return data_keys_lower[c.lower()]
        return None

    def _extract_prices(self, data: dict):
        """
        Try every known key format to extract ETH and BTC close prices.
        Returns (eth_price, btc_price) or (None, None) on failure.
        Caches discovered keys after first successful extraction.
        """
        if self._data_keys_found is not None:
            eth_key, btc_key = self._data_keys_found
        else:
            eth_key = self._find_key(data, self.ETH_KEY_CANDIDATES)
            btc_key = self._find_key(data, self.BTC_KEY_CANDIDATES)
            if eth_key and btc_key:
                self._data_keys_found = (eth_key, btc_key)

        if not eth_key or not btc_key:
            return None, None

        eth_df = data.get(eth_key)
        btc_df = data.get(btc_key)

        if eth_df is None or btc_df is None:
            return None, None

        if not isinstance(eth_df, pd.DataFrame) or len(eth_df) == 0:
            return None, None
        if not isinstance(btc_df, pd.DataFrame) or len(btc_df) == 0:
            return None, None

        eth_price = self._get_close(eth_df)
        btc_price = self._get_close(btc_df)

        if eth_price is None or btc_price is None:
            return None, None
        if btc_price <= 0:
            return None, None

        return eth_price, btc_price

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def on_data(self, data: dict) -> list[Signal]:
        self._candle_count += 1

        eth_price, btc_price = self._extract_prices(data)

        # If we still can't find prices, emit a sentinel buy to prove on_data fires
        # (only on candle 1, to make 0-trade failure detectable vs total silence)
        if eth_price is None or btc_price is None:
            if self._candle_count == 1:
                # Attempt to buy BTC as a heartbeat signal — proves strategy is alive
                # Uses a tiny size so it doesn't matter which key failed
                # Avoids confusion: only fire once, on first candle
                return [Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=0.01,
                    order_type="market",
                    rationale=f"DIAGNOSTIC: on_data fired but no price data found. data keys={list(data.keys())[:10]}"
                )]
            return []

        # Compute ratio and rolling z-score
        ratio = eth_price / btc_price
        self._ratios.append(ratio)

        # Trim to window
        if len(self._ratios) > self.WINDOW * 3:
            self._ratios = self._ratios[-self.WINDOW * 3:]

        # Need at least WINDOW observations to compute z-score
        if len(self._ratios) < self.WINDOW:
            return []

        window_data = self._ratios[-self.WINDOW:]
        mean = float(np.mean(window_data))
        std = float(np.std(window_data))

        if std < 1e-10:
            return []

        z = (ratio - mean) / std

        signals = []

        # State machine: BTC is default, ETH only when ratio is low
        if self._position == "btc":
            if z < self.ENTRY_Z:
                # ETH is cheap relative to BTC — rotate into ETH
                signals.append(Signal(
                    action="sell",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC ratio z={z:.2f} < {self.ENTRY_Z} — rotating from BTC to ETH"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=f"ETH/BTC ratio z={z:.2f} — entering ETH long"
                ))
                self._position = "eth"

        elif self._position == "eth":
            if abs(z) < self.EXIT_Z_ABS:
                # Spread has normalized — rotate back to BTC
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC ratio z={z:.2f} — spread normalized, rotating back to BTC"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=f"Re-entering BTC after ETH trade completed"
                ))
                self._position = "btc"
            elif z > 2.0:
                # Ratio has overshot the other way — stop out
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"ETH/BTC ratio z={z:.2f} — adverse overshoot, stopping out"
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.SIZE_PCT,
                    order_type="market",
                    rationale=f"Re-entering BTC after ETH stop-out"
                ))
                self._position = "btc"

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track position state from fills."""
        action = fill.get("action", "")
        pair = fill.get("pair", "")
        if action == "buy" and "ETH" in pair:
            self._position = "eth"
        elif action in ("sell", "close") and "ETH" in pair:
            self._position = "btc"
        elif action == "buy" and "BTC" in pair:
            self._position = "btc"
