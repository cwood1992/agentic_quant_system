# Strategy: quant_primary_hyp_006_btc_simple_test
# Written by agent quant_primary via write_strategy_code tool.

"""
quant_primary_hyp_006_btc_simple_test

Ultra-simple BTC momentum strategy. Single asset, single feed.
Primary purpose: DIAGNOSTIC — confirm the backtest engine can execute ANY
strategy and produce at least 1 trade before we debug more complex logic.

Logic:
- If no position: buy BTC on candle 2 (gives one warmup candle)
- If in position: sell when price drops 5% from entry OR holds for 20 candles
- This guarantees multiple trades over 90 days
"""

import pandas as pd
from strategies.base import BaseStrategy, Signal


class Quant006BTCSimpleTest(BaseStrategy):

    def __init__(self):
        self._position = False
        self._entry_price = None
        self._entry_candle = 0
        self._candle_count = 0

    def name(self) -> str:
        return "quant_primary_hyp_006_btc_simple_test"

    def required_feeds(self) -> list[str]:
        # Try base pair name only (no timeframe suffix) — engine may not want ":4h"
        return ["BTC/USD"]

    def _get_close(self, df: pd.DataFrame) -> float | None:
        """Extract close price from DataFrame regardless of column naming."""
        if df is None or len(df) == 0:
            return None
        for col in ["close", "Close", "CLOSE", "c", "price"]:
            if col in df.columns:
                val = df[col].iloc[-1]
                if pd.notna(val):
                    return float(val)
        # Try positional — OHLCV = [0:open, 1:high, 2:low, 3:close, 4:volume]
        if len(df.columns) >= 4:
            val = df.iloc[-1, 3]
            if pd.notna(val):
                return float(val)
        return None

    def _get_data(self, data: dict) -> pd.DataFrame | None:
        """Try all known key formats for BTC/USD."""
        candidates = [
            "BTC/USD", "BTC/USD:4h", "BTC/USD:1h", "BTC/USD:1d",
            "BTCUSD", "BTC_USD", "btc/usd", "btcusd",
            "BTC/USD:daily", "BTC/USD:hourly",
        ]
        for key in candidates:
            if key in data:
                return data[key]
        # Last resort: take the first value if only one entry
        if len(data) == 1:
            return next(iter(data.values()))
        # Try any key containing BTC
        for key in data:
            if "BTC" in key.upper() or "btc" in key.lower():
                return data[key]
        return None

    def on_data(self, data: dict) -> list[Signal]:
        self._candle_count += 1

        # Candle 1: emit a 1% heartbeat buy regardless of data — proves on_data is being called
        if self._candle_count == 1:
            return [Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=0.01,
                order_type="market",
                rationale="HEARTBEAT candle 1 — proves on_data() is called"
            )]

        df = self._get_data(data)
        close = self._get_close(df) if df is not None else None

        # Candle 2: buy with 90% of capital regardless of data content
        if self._candle_count == 2:
            self._position = True
            self._entry_price = close if close else 70000.0  # fallback price
            self._entry_candle = self._candle_count
            return [Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=0.90,
                order_type="market",
                rationale=f"DIAGNOSTIC buy candle 2 — close={close}"
            )]

        if close is None:
            return []

        signals = []

        if self._position:
            # Exit if: -5% stop loss, +10% take profit, or after 20 candles
            candles_held = self._candle_count - self._entry_candle
            pct_change = (close - self._entry_price) / self._entry_price if self._entry_price else 0

            if pct_change <= -0.05 or pct_change >= 0.10 or candles_held >= 20:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Exit: pct_change={pct_change:.3f}, candles_held={candles_held}"
                ))
                self._position = False
                self._entry_price = None
                self._entry_candle = 0
        else:
            # Re-enter: always buy after being flat for 2+ candles
            signals.append(Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=0.90,
                order_type="market",
                rationale=f"Re-entry after flat period, close={close:.2f}"
            ))
            self._position = True
            self._entry_price = close
            self._entry_candle = self._candle_count

        return signals
