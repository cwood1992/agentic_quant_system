# Strategy: quant_primary_hyp_001_fear_greed_btc_dip
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Extreme Fear Contrarian Strategy
quant_primary_hyp_001_fear_greed_btc_dip

Thesis: Sustained extreme fear (F&G < 20) with short-term positive BTC momentum
signals exhaustion of weak-hand selling. Enter long BTC, exit when fear normalises
(F&G > 40) or trailing stop triggered.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearGreedBtcDipStrategy(BaseStrategy):

    # ---- configuration ----
    ENTRY_FG_MAX = 20          # F&G must be at or below this to enter
    EXIT_FG_MIN = 40           # Exit when F&G recovers above this
    TRAIL_STOP_PCT = 0.08      # 8% trailing stop from entry
    WEEKLY_LOOKBACK = 42       # 42 × 4h = 7 days of candles for weekly return check
    WEEKLY_RETURN_MIN = 0.0    # BTC 7d return must be ≥ 0 (positive momentum filter)
    SIZE_PCT = 0.80            # 80% of capital per position

    def __init__(self):
        self._in_position = False
        self._entry_price: float | None = None
        self._highest_price: float | None = None   # for trailing stop
        self._entry_fg: float | None = None

    def name(self) -> str:
        return "quant_primary_hyp_001_fear_greed_btc_dip"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h", "fear_greed_index:1d"]

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        signals: list[Signal] = []

        # --- guard: need both feeds ---
        if "BTC/USD:4h" not in data or "fear_greed_index:1d" not in data:
            return signals

        btc = data["BTC/USD:4h"]
        fg = data["fear_greed_index:1d"]

        if btc.empty or fg.empty:
            return signals

        # --- current values ---
        current_price = float(btc["close"].iloc[-1])
        current_fg = float(fg["close"].iloc[-1])  # F&G stored as close value

        # --- trailing stop update ---
        if self._in_position and self._highest_price is not None:
            if current_price > self._highest_price:
                self._highest_price = current_price

        # ---- EXIT logic (check before entry) ----
        if self._in_position:
            # F&G recovery exit
            if current_fg >= self.EXIT_FG_MIN:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"F&G recovered to {current_fg:.0f} >= {self.EXIT_FG_MIN} — fear normalised, close position"
                ))
                self._in_position = False
                self._entry_price = None
                self._highest_price = None
                self._entry_fg = None
                return signals

            # Trailing stop exit
            if self._entry_price is not None and self._highest_price is not None:
                trail_level = self._highest_price * (1.0 - self.TRAIL_STOP_PCT)
                if current_price <= trail_level:
                    signals.append(Signal(
                        action="close",
                        pair="BTC/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=(
                            f"Trailing stop hit: price {current_price:.2f} <= "
                            f"trail level {trail_level:.2f} "
                            f"(high {self._highest_price:.2f}, -{self.TRAIL_STOP_PCT*100:.0f}%)"
                        )
                    ))
                    self._in_position = False
                    self._entry_price = None
                    self._highest_price = None
                    self._entry_fg = None
                return signals  # already in position; no new entry

        # ---- ENTRY logic ----
        if not self._in_position:
            # Condition 1: F&G in extreme fear zone
            if current_fg > self.ENTRY_FG_MAX:
                return signals

            # Condition 2: positive 7d BTC price momentum
            if len(btc) < self.WEEKLY_LOOKBACK + 1:
                return signals  # not enough history

            price_7d_ago = float(btc["close"].iloc[-(self.WEEKLY_LOOKBACK + 1)])
            weekly_return = (current_price - price_7d_ago) / price_7d_ago

            if weekly_return < self.WEEKLY_RETURN_MIN:
                return signals  # negative weekly momentum — wait

            # Both conditions met — enter long
            signals.append(Signal(
                action="buy",
                pair="BTC/USD",
                size_pct=self.SIZE_PCT,
                order_type="market",
                rationale=(
                    f"Extreme fear entry: F&G={current_fg:.0f} (threshold {self.ENTRY_FG_MAX}), "
                    f"7d BTC return={weekly_return*100:.2f}% (positive momentum confirmed)"
                )
            ))
            self._in_position = True
            self._entry_price = current_price
            self._highest_price = current_price
            self._entry_fg = current_fg

        return signals

    def on_fill(self, fill: dict) -> None:
        """Update state on confirmed fills."""
        if fill.get("action") in ("close",) and fill.get("pair") == "BTC/USD":
            self._in_position = False
            self._entry_price = None
            self._highest_price = None
            self._entry_fg = None
        elif fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            self._in_position = True
            self._entry_price = fill.get("fill_price", self._entry_price)
            self._highest_price = self._entry_price

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "highest_price_since_entry": self._highest_price,
            "trail_stop_level": (
                round(self._highest_price * (1 - self.TRAIL_STOP_PCT), 2)
                if self._highest_price else None
            ),
            "entry_fg": self._entry_fg,
        }
