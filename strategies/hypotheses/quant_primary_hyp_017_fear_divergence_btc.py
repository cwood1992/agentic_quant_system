# Strategy: quant_primary_hyp_017_fear_divergence_btc
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed Divergence Momentum Strategy
hypothesis_id: quant_primary_hyp_017_fear_divergence_btc

Thesis: When Fear & Greed Index is in Extreme Fear (<20) while BTC shows
positive 7-day momentum (>3%), this indicates a sentiment-price divergence
consistent with early recovery regime. Go long BTC; exit when fear resolves
(F&G > 40) or hard stop at -8% from entry.

Long-only. Single position. Daily cadence.
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearDivergenceBTC(BaseStrategy):
    """
    Entry: F&G < 20 AND BTC 7d return > 3% AND not already in position
    Exit:  F&G > 40 (fear resolved) OR price < entry * 0.92 (hard stop)
           OR price > entry * 1.20 (take profit)
    """

    # --- tunable parameters -------------------------------------------------
    FEAR_ENTRY_THRESHOLD = 20       # F&G below this = extreme fear
    FEAR_EXIT_THRESHOLD  = 40       # F&G above this = fear resolved, exit
    MOMENTUM_MIN_7D      = 0.03     # 7-day BTC return must be > 3% to enter
    STOP_LOSS_PCT        = 0.08     # exit if price falls 8% below entry
    TAKE_PROFIT_PCT      = 0.20     # exit if price rises 20% above entry
    POSITION_SIZE        = 0.90     # 90% of capital (leaves buffer for fees)
    MIN_CANDLES          = 8        # minimum candles needed before trading
    # ------------------------------------------------------------------------

    def __init__(self):
        self._in_position = False
        self._entry_price: float = 0.0
        self._last_fear_greed: float = 50.0  # neutral default

    def name(self) -> str:
        return "quant_primary_hyp_017_fear_divergence_btc"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:1d", "fear_greed_index:1d"]

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        btc_df = data.get("BTC/USD:1d")
        fg_df  = data.get("fear_greed_index:1d")

        if btc_df is None or len(btc_df) < self.MIN_CANDLES:
            return []

        # --- parse Fear & Greed -------------------------------------------
        fear_greed = self._last_fear_greed  # carry last known value
        if fg_df is not None and len(fg_df) > 0:
            # column name may be 'value' or 'close'
            for col in ("value", "close", "fear_greed"):
                if col in fg_df.columns:
                    latest_fg = fg_df[col].dropna()
                    if len(latest_fg) > 0:
                        fear_greed = float(latest_fg.iloc[-1])
                        self._last_fear_greed = fear_greed
                    break

        # --- BTC price metrics --------------------------------------------
        closes = btc_df["close"].dropna()
        current_price = float(closes.iloc[-1])

        # 7-day momentum: need at least 8 candles (daily bars)
        if len(closes) >= 8:
            price_7d_ago = float(closes.iloc[-8])
            momentum_7d  = (current_price - price_7d_ago) / price_7d_ago
        else:
            momentum_7d = 0.0

        signals = []

        # --- exit logic (check first to avoid whipsaws) -------------------
        if self._in_position:
            stop_price   = self._entry_price * (1.0 - self.STOP_LOSS_PCT)
            target_price = self._entry_price * (1.0 + self.TAKE_PROFIT_PCT)

            should_exit = False
            exit_reason = ""

            if current_price <= stop_price:
                should_exit = True
                exit_reason = (
                    f"STOP LOSS: price {current_price:.2f} <= "
                    f"stop {stop_price:.2f} (entry {self._entry_price:.2f})"
                )
            elif current_price >= target_price:
                should_exit = True
                exit_reason = (
                    f"TAKE PROFIT: price {current_price:.2f} >= "
                    f"target {target_price:.2f} (entry {self._entry_price:.2f})"
                )
            elif fear_greed >= self.FEAR_EXIT_THRESHOLD:
                should_exit = True
                exit_reason = (
                    f"FEAR RESOLVED: F&G={fear_greed:.0f} >= "
                    f"{self.FEAR_EXIT_THRESHOLD} (fear resolved)"
                )

            if should_exit:
                self._in_position = False
                self._entry_price = 0.0
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason,
                ))

        # --- entry logic --------------------------------------------------
        else:
            extreme_fear    = fear_greed < self.FEAR_ENTRY_THRESHOLD
            positive_momentum = momentum_7d > self.MOMENTUM_MIN_7D

            if extreme_fear and positive_momentum:
                self._in_position  = True
                self._entry_price  = current_price
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=(
                        f"ENTRY: F&G={fear_greed:.0f} (Extreme Fear) + "
                        f"7d momentum={momentum_7d*100:.1f}% "
                        f"@ {current_price:.2f}"
                    ),
                ))

        return signals

    def on_fill(self, fill: dict) -> None:
        """Track actual fill price for stop/target calculations."""
        if fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            fill_price = fill.get("fill_price")
            if fill_price:
                self._entry_price = float(fill_price)
        elif fill.get("action") == "close":
            self._in_position = False
            self._entry_price = 0.0

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position":  self._in_position,
            "entry_price":  self._entry_price,
            "last_fear_greed": self._last_fear_greed,
        }
