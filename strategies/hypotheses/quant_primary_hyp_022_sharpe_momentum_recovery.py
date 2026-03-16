# Strategy: quant_primary_hyp_022_sharpe_momentum_recovery
# Written by agent quant_primary via write_strategy_code tool.

"""
Sharpe Momentum Recovery Strategy
Hypothesis: quant_primary_hyp_022_sharpe_momentum_recovery

Edge: After sustained drawdown periods (negative rolling Sharpe), 
when rolling Sharpe crosses from negative to positive and price 
momentum is confirming, a regime transition is underway. Enter 
long BTC/ETH on the regime flip signal; exit when momentum stalls.

Context: Fear & Greed at 15 (Extreme Fear) while price rallies 7-10% 
in 7 days reflects institutional accumulation before retail sentiment 
catches up. The divergence between F&G and price action historically 
precedes continued upside as sentiment eventually catches up.

Signal: 14-day rolling Sharpe crosses above 0 (from negative territory)
        AND 3-day price return > 2%
        AND 7-day price return > 5%

Exit: Rolling Sharpe drops back below -0.5 OR 7-day return < -5%
"""

import numpy as np
import pandas as pd
from strategies.base import BaseStrategy, Signal


class SharpeMomentumRecovery(BaseStrategy):

    SHARPE_WINDOW = 56      # 14 days at 4h = 56 bars
    SHARPE_ENTRY = 0.0      # Sharpe crosses above this
    SHARPE_EXIT = -0.5      # Exit if Sharpe drops here
    RETURN_3D_MIN = 0.02    # 3-day return > 2% for entry confirmation
    RETURN_7D_MIN = 0.05    # 7-day return > 5% for entry confirmation
    RETURN_7D_STOP = -0.05  # 7-day return < -5% triggers exit
    SIZE_PCT_BTC = 0.45     # 45% BTC
    SIZE_PCT_ETH = 0.45     # 45% ETH

    def __init__(self):
        self._btc_position = False
        self._eth_position = False

    def name(self) -> str:
        return "quant_primary_hyp_022_sharpe_momentum_recovery"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h", "ETH/USD:4h"]

    def _rolling_sharpe(self, close: pd.Series, window: int) -> float:
        """Annualized rolling Sharpe over window bars."""
        if len(close) < window + 1:
            return np.nan
        returns = close.pct_change().dropna().iloc[-window:]
        if returns.std() < 1e-10:
            return np.nan
        # Annualize: 4h bars => 6 per day => 2190 per year
        bars_per_year = 2190
        sharpe = (returns.mean() / returns.std()) * np.sqrt(bars_per_year)
        return sharpe

    def _return_over_n_bars(self, close: pd.Series, n_bars: int) -> float:
        """Return over last n_bars periods."""
        if len(close) < n_bars + 1:
            return np.nan
        return (close.iloc[-1] / close.iloc[-n_bars - 1]) - 1.0

    def _prev_sharpe(self, close: pd.Series, window: int) -> float:
        """Sharpe for the window ending one bar ago."""
        if len(close) < window + 2:
            return np.nan
        prev_close = close.iloc[:-1]
        returns = prev_close.pct_change().dropna().iloc[-window:]
        if returns.std() < 1e-10:
            return np.nan
        bars_per_year = 2190
        return (returns.mean() / returns.std()) * np.sqrt(bars_per_year)

    def on_data(self, data: dict) -> list[Signal]:
        signals = []

        btc_df = data.get("BTC/USD:4h")
        eth_df = data.get("ETH/USD:4h")

        if btc_df is None or eth_df is None:
            return signals

        btc_close = btc_df["close"]
        eth_close = eth_df["close"]

        # Compute signals for BTC
        btc_sharpe = self._rolling_sharpe(btc_close, self.SHARPE_WINDOW)
        btc_prev_sharpe = self._prev_sharpe(btc_close, self.SHARPE_WINDOW)
        btc_ret_3d = self._return_over_n_bars(btc_close, 18)   # 3 days * 6 bars
        btc_ret_7d = self._return_over_n_bars(btc_close, 42)   # 7 days * 6 bars

        # Compute signals for ETH
        eth_sharpe = self._rolling_sharpe(eth_close, self.SHARPE_WINDOW)
        eth_prev_sharpe = self._prev_sharpe(eth_close, self.SHARPE_WINDOW)
        eth_ret_3d = self._return_over_n_bars(eth_close, 18)
        eth_ret_7d = self._return_over_n_bars(eth_close, 42)

        # --- BTC entry logic ---
        if not self._btc_position:
            if (not np.isnan(btc_sharpe) and not np.isnan(btc_prev_sharpe)
                    and not np.isnan(btc_ret_3d) and not np.isnan(btc_ret_7d)):
                # Sharpe crossover: was negative, now positive
                sharpe_cross = (btc_prev_sharpe < self.SHARPE_ENTRY
                                and btc_sharpe >= self.SHARPE_ENTRY)
                momentum_confirm = (btc_ret_3d >= self.RETURN_3D_MIN
                                    and btc_ret_7d >= self.RETURN_7D_MIN)
                if sharpe_cross and momentum_confirm:
                    self._btc_position = True
                    signals.append(Signal(
                        action="buy",
                        pair="BTC/USD",
                        size_pct=self.SIZE_PCT_BTC,
                        order_type="market",
                        rationale=(f"BTC Sharpe crossed {btc_prev_sharpe:.2f}→{btc_sharpe:.2f}. "
                                   f"3d={btc_ret_3d:.1%}, 7d={btc_ret_7d:.1%}. "
                                   f"Momentum recovery confirmed.")
                    ))
        else:
            # BTC exit logic
            if not np.isnan(btc_sharpe) and not np.isnan(btc_ret_7d):
                if btc_sharpe < self.SHARPE_EXIT or btc_ret_7d < self.RETURN_7D_STOP:
                    self._btc_position = False
                    signals.append(Signal(
                        action="close",
                        pair="BTC/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=(f"BTC momentum fading: Sharpe={btc_sharpe:.2f}, "
                                   f"7d_ret={btc_ret_7d:.1%}")
                    ))

        # --- ETH entry logic ---
        if not self._eth_position:
            if (not np.isnan(eth_sharpe) and not np.isnan(eth_prev_sharpe)
                    and not np.isnan(eth_ret_3d) and not np.isnan(eth_ret_7d)):
                sharpe_cross = (eth_prev_sharpe < self.SHARPE_ENTRY
                                and eth_sharpe >= self.SHARPE_ENTRY)
                momentum_confirm = (eth_ret_3d >= self.RETURN_3D_MIN
                                    and eth_ret_7d >= self.RETURN_7D_MIN)
                if sharpe_cross and momentum_confirm:
                    self._eth_position = True
                    signals.append(Signal(
                        action="buy",
                        pair="ETH/USD",
                        size_pct=self.SIZE_PCT_ETH,
                        order_type="market",
                        rationale=(f"ETH Sharpe crossed {eth_prev_sharpe:.2f}→{eth_sharpe:.2f}. "
                                   f"3d={eth_ret_3d:.1%}, 7d={eth_ret_7d:.1%}. "
                                   f"Momentum recovery confirmed.")
                    ))
        else:
            # ETH exit logic
            if not np.isnan(eth_sharpe) and not np.isnan(eth_ret_7d):
                if eth_sharpe < self.SHARPE_EXIT or eth_ret_7d < self.RETURN_7D_STOP:
                    self._eth_position = False
                    signals.append(Signal(
                        action="close",
                        pair="ETH/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=(f"ETH momentum fading: Sharpe={eth_sharpe:.2f}, "
                                   f"7d_ret={eth_ret_7d:.1%}")
                    ))

        return signals

    def on_fill(self, fill: dict) -> None:
        pair = fill.get("pair", "")
        action = fill.get("action", "")
        if "BTC" in pair:
            if action == "buy":
                self._btc_position = True
            elif action in ("close", "sell"):
                self._btc_position = False
        elif "ETH" in pair:
            if action == "buy":
                self._eth_position = True
            elif action in ("close", "sell"):
                self._eth_position = False

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "btc_position": self._btc_position,
            "eth_position": self._eth_position,
        }
