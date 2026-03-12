"""Strategy: hyp_002_btc_fear_reversal

Written by agent quant_primary via write_strategy_code tool.
"""

"""
BTC Fear Reversal Strategy
Hypothesis: hyp_002_btc_fear_reversal

Enters BTC long when three conditions align:
  1. Fear & Greed Index <= 20 (Extreme Fear)
  2. Rolling 14-day Sharpe ratio (4h resolution) < -10 (genuine drawdown)
  3. Current 4h candle closes above the prior 4h high (momentum confirmation)

Exit conditions (whichever fires first):
  a. 14-day rolling Sharpe crosses above 0 (sentiment normalizing)
  b. Fear & Greed rises above 35
  c. BTC drops 8% below entry price (stop loss)
  d. Max hold 40 x 4h candles (~10 days)
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class BtcFearReversal(BaseStrategy):

    # ── tunable parameters ────────────────────────────────────────────────────
    FG_ENTRY_THRESHOLD    = 20      # Fear & Greed <= this to qualify
    FG_EXIT_THRESHOLD     = 35      # Fear & Greed >= this to exit
    SHARPE_ENTRY_THRESHOLD = -10.0  # 14d rolling Sharpe < this to qualify
    SHARPE_EXIT_THRESHOLD  =  0.0   # Sharpe crosses above this to exit
    SHARPE_WINDOW_PERIODS  = 84     # 14 days * 6 periods/day (4h candles)
    STOP_LOSS_PCT          = 0.08   # 8% stop below entry
    MAX_HOLD_PERIODS       = 40     # ~10 days at 4h
    ANNUALIZE_FACTOR       = np.sqrt(6 * 365)  # 4h candles → annualized
    POSITION_SIZE          = 0.80   # 80% of capital

    def __init__(self):
        self._position       = False
        self._entry_price    = None
        self._entry_period   = None
        self._periods_held   = 0

    # ── interface ─────────────────────────────────────────────────────────────

    def name(self) -> str:
        return "hyp_002_btc_fear_reversal"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h"]

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        btc_df = data.get("BTC/USD:4h") or data.get("BTC/USD")

        if btc_df is None or len(btc_df) < self.SHARPE_WINDOW_PERIODS + 2:
            return []

        closes = btc_df["close"]
        highs  = btc_df["high"]

        # ── Compute 14-day rolling Sharpe (annualized) ────────────────────────
        returns = closes.pct_change().dropna()
        if len(returns) < self.SHARPE_WINDOW_PERIODS:
            return []

        recent_returns = returns.iloc[-self.SHARPE_WINDOW_PERIODS:]
        ret_mean = recent_returns.mean()
        ret_std  = recent_returns.std()
        rolling_sharpe = (ret_mean / ret_std * self.ANNUALIZE_FACTOR) if ret_std > 1e-9 else 0.0

        # ── Fear & Greed from supplementary feed ─────────────────────────────
        # The fear_greed value is injected into the data dict as a scalar or
        # series. We look for it in a few possible key formats.
        fg_value = None
        for key in ("fear_greed_index", "fear_greed", "FG"):
            if key in data:
                raw = data[key]
                if isinstance(raw, (int, float)):
                    fg_value = float(raw)
                elif isinstance(raw, pd.Series):
                    fg_value = float(raw.iloc[-1])
                elif isinstance(raw, pd.DataFrame):
                    col = raw.columns[0]
                    fg_value = float(raw[col].iloc[-1])
                break

        current_close    = float(closes.iloc[-1])
        prior_high       = float(highs.iloc[-2])   # prior candle's high

        signals = []

        if not self._position:
            # ── Entry check ───────────────────────────────────────────────────
            fg_ok       = (fg_value is not None) and (fg_value <= self.FG_ENTRY_THRESHOLD)
            sharpe_ok   = rolling_sharpe < self.SHARPE_ENTRY_THRESHOLD
            momentum_ok = current_close > prior_high   # first green candle breakout

            if fg_ok and sharpe_ok and momentum_ok:
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=(
                        f"FG={fg_value:.0f}<=20, Sharpe14d={rolling_sharpe:.1f}<-10, "
                        f"close={current_close:.0f}>prior_high={prior_high:.0f}: "
                        f"fear reversal entry"
                    )
                ))
                self._position     = True
                self._entry_price  = current_close
                self._periods_held = 0

        else:
            # ── Exit check ────────────────────────────────────────────────────
            self._periods_held += 1
            exit_reason = None

            # a. Sharpe normalized
            if rolling_sharpe >= self.SHARPE_EXIT_THRESHOLD:
                exit_reason = f"Sharpe14d={rolling_sharpe:.1f} >= 0: sentiment normalizing"

            # b. Fear exits extreme zone
            elif fg_value is not None and fg_value >= self.FG_EXIT_THRESHOLD:
                exit_reason = f"FG={fg_value:.0f} >= {self.FG_EXIT_THRESHOLD}: fear easing"

            # c. Stop loss
            elif self._entry_price and current_close < self._entry_price * (1 - self.STOP_LOSS_PCT):
                drop_pct = (current_close / self._entry_price - 1) * 100
                exit_reason = f"Stop loss: BTC dropped {drop_pct:.1f}% from entry {self._entry_price:.0f}"

            # d. Max hold
            elif self._periods_held >= self.MAX_HOLD_PERIODS:
                exit_reason = f"Max hold reached ({self.MAX_HOLD_PERIODS} periods)"

            if exit_reason:
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
                self._position     = False
                self._entry_price  = None
                self._periods_held = 0

        return signals

    def on_fill(self, fill: dict) -> None:
        if fill.get("action") == "buy" and fill.get("pair") == "BTC/USD":
            self._entry_price = fill.get("fill_price", self._entry_price)

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "in_position":   self._position,
            "entry_price":   self._entry_price,
            "periods_held":  self._periods_held,
        }
