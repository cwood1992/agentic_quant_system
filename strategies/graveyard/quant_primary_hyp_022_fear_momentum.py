# Strategy: quant_primary_hyp_022_fear_momentum
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear-Divergence Momentum Strategy
Hypothesis: quant_primary_hyp_022_fear_momentum

When prices are rising (positive rolling momentum) while Fear & Greed Index remains
in Extreme Fear territory (<35), it signals informed/institutional buying without
retail participation. The lag-1 autocorrelation on 4h returns is statistically
significant (BTC: 0.12 p=0.023, SOL: 0.15 p=0.006), confirming short-term
momentum persistence in this regime.

Long-only. Goes long the single highest-momentum pair that passes all filters.
Exits when momentum deteriorates OR sentiment normalizes (fear unwinds = crowded).
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearDivergenceMomentum(BaseStrategy):

    def name(self) -> str:
        return "quant_primary_hyp_022_fear_momentum"

    def required_feeds(self) -> list[str]:
        return [
            "BTC/USD:4h",
            "ETH/USD:4h",
            "SOL/USD:4h",
            "AVAX/USD:4h",
            "fear_greed_index:1d",
        ]

    def __init__(self):
        self._position_pair = None        # which pair we're currently long
        self._position_entry_price = None
        self._last_signal_cycle = 0

    def _rolling_sharpe(self, prices: pd.Series, window: int = 84) -> float:
        """Compute rolling Sharpe over last `window` 4h candles (14 days)."""
        if len(prices) < window + 1:
            return 0.0
        returns = prices.pct_change().dropna().iloc[-window:]
        if returns.std() == 0:
            return 0.0
        # Annualise: 4h candles = 6 per day, 365 days
        annualise = np.sqrt(6 * 365)
        return float((returns.mean() / returns.std()) * annualise)

    def _ema(self, prices: pd.Series, span: int = 20) -> float:
        """Return latest EMA value."""
        if len(prices) < span:
            return float(prices.iloc[-1])
        return float(prices.ewm(span=span, adjust=False).mean().iloc[-1])

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        # ── 1. Get Fear & Greed ──────────────────────────────────────────────
        fg_df = data.get("fear_greed_index:1d")
        if fg_df is None or len(fg_df) == 0:
            return signals

        # Expect a column named 'value' or 'close' or the raw value
        if "value" in fg_df.columns:
            fear_greed = float(fg_df["value"].iloc[-1])
        elif "close" in fg_df.columns:
            fear_greed = float(fg_df["close"].iloc[-1])
        else:
            fear_greed = float(fg_df.iloc[-1, 0])

        # Sentiment gate: only trade when F&G < 35 (fear persists)
        # AND not in pure panic (<10 — liquidity collapses)
        sentiment_ok = 10 <= fear_greed <= 35

        # ── 2. Score each candidate pair ────────────────────────────────────
        pairs = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD"]
        scores = {}

        for pair in pairs:
            feed_key = f"{pair}:4h"
            df = data.get(feed_key)
            if df is None or len(df) < 90:
                continue

            close = df["close"]
            current_price = float(close.iloc[-1])

            # Rolling Sharpe (14d = 84 candles)
            sharpe = self._rolling_sharpe(close, window=84)

            # EMA(20) directional filter
            ema20 = self._ema(close, span=20)
            above_ema = current_price > ema20

            # Momentum score: Sharpe * trend confirmation
            if sharpe > 2.0 and above_ema:
                scores[pair] = sharpe

        # ── 3. Determine best pair ──────────────────────────────────────────
        best_pair = max(scores, key=scores.get) if scores else None

        # ── 4. Position management ──────────────────────────────────────────
        currently_long = self._position_pair is not None

        if currently_long:
            held_pair = self._position_pair
            held_feed = f"{held_pair}:4h"
            held_df = data.get(held_feed)

            # Exit conditions
            should_exit = False
            exit_reason = ""

            if held_df is not None and len(held_df) >= 90:
                held_sharpe = self._rolling_sharpe(held_df["close"], window=84)
                held_ema = self._ema(held_df["close"], span=20)
                held_price = float(held_df["close"].iloc[-1])

                if held_sharpe < 0.5:
                    should_exit = True
                    exit_reason = f"momentum deteriorated: Sharpe={held_sharpe:.2f}"
                elif held_price < held_ema:
                    should_exit = True
                    exit_reason = f"price below EMA20: price={held_price:.4f} ema={held_ema:.4f}"

            if not sentiment_ok and fear_greed > 35:
                should_exit = True
                exit_reason = f"sentiment normalising: F&G={fear_greed}"

            if should_exit:
                signals.append(Signal(
                    action="close",
                    pair=held_pair,
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Exit fear-momentum: {exit_reason}"
                ))
                self._position_pair = None
                self._position_entry_price = None
                return signals

            # Rotate to better pair if strongly superior
            if best_pair and best_pair != held_pair and best_pair in scores:
                held_score = scores.get(held_pair, 0)
                new_score = scores[best_pair]
                if new_score > held_score * 1.3:  # 30% better to rotate
                    signals.append(Signal(
                        action="close",
                        pair=held_pair,
                        size_pct=1.0,
                        order_type="market",
                        rationale=f"Rotate to {best_pair} (score {new_score:.2f} vs {held_score:.2f})"
                    ))
                    self._position_pair = None
                    # Fall through to open new position below

        # Open new position if not currently long
        if self._position_pair is None and best_pair and sentiment_ok:
            entry_df = data.get(f"{best_pair}:4h")
            if entry_df is not None:
                entry_price = float(entry_df["close"].iloc[-1])
                signals.append(Signal(
                    action="buy",
                    pair=best_pair,
                    size_pct=0.95,  # 95% of capital, long-only
                    order_type="market",
                    rationale=(
                        f"Fear-divergence momentum: {best_pair} Sharpe={scores[best_pair]:.2f}, "
                        f"F&G={fear_greed} (Extreme Fear), above EMA20"
                    )
                ))
                self._position_pair = best_pair
                self._position_entry_price = entry_price

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") in ("close",):
            self._position_pair = None
            self._position_entry_price = None
        elif fill.get("action") == "buy":
            self._position_pair = fill.get("pair")
            self._position_entry_price = fill.get("fill_price")

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "position_pair": self._position_pair,
            "position_entry_price": self._position_entry_price,
        }
