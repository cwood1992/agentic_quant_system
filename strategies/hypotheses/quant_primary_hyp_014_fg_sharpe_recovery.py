# Strategy: quant_primary_hyp_014_fg_sharpe_recovery
# Written by agent quant_primary via write_strategy_code tool.

"""
Fear & Greed + Rolling Sharpe Recovery Entry
Strategy ID: quant_primary_hyp_014_fg_sharpe_recovery

Thesis: When Fear & Greed Index is in Extreme Fear (<= 25) AND the 14-day
rolling Sharpe of BTC has already turned positive (>= 1.5, annualized),
price momentum has inflected while sentiment remains maximally negative.
This "sentiment lag" window is a high-probability recovery entry.

Entry: F&G <= 25 AND rolling Sharpe (14d, 4h) >= 1.5
Exit: Rolling Sharpe drops below 1.0 (momentum waning) OR F&G >= 50 (sentiment recovered)
Stop: -8% from entry (hard stop)

Size: 50% BTC, 20% ETH (BTC primary signal, ETH amplified beta ~1.24)
Total: 70% of capital deployed per signal

Required feeds: BTC/USD:4h, ETH/USD:4h, fear_greed_index:1d
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class FearGreedSharpeRecoveryStrategy(BaseStrategy):
    """
    Long BTC (and ETH as beta amplifier) when extreme fear + positive momentum confluence.
    Sentiment lag thesis: market bottoms form when momentum turns but sentiment still negative.
    """

    FG_ENTRY_THRESHOLD = 25       # Extreme Fear
    FG_EXIT_THRESHOLD = 50        # Neutral sentiment — thesis resolved
    SHARPE_ENTRY_THRESHOLD = 1.5  # Rolling 14d Sharpe must be positive and meaningful
    SHARPE_EXIT_THRESHOLD = 1.0   # Exit if momentum wanes
    HARD_STOP_PCT = 0.08          # 8% hard stop
    BTC_SIZE_PCT = 0.50
    ETH_SIZE_PCT = 0.20
    SHARPE_WINDOW = 84            # 84 × 4h periods = 14 days

    def __init__(self):
        self._btc_in_position = False
        self._eth_in_position = False
        self._btc_entry_price = None
        self._eth_entry_price = None

    def name(self) -> str:
        return "quant_primary_hyp_014_fg_sharpe_recovery"

    def required_feeds(self) -> list[str]:
        return ["BTC/USD:4h", "ETH/USD:4h", "fear_greed_index:1d"]

    def _compute_rolling_sharpe(self, df: pd.DataFrame) -> float | None:
        """Compute annualized 14-day rolling Sharpe from 4h OHLCV data."""
        if len(df) < self.SHARPE_WINDOW + 1:
            return None

        closes = df["close"].tail(self.SHARPE_WINDOW + 1)
        returns = closes.pct_change().dropna()

        if len(returns) < self.SHARPE_WINDOW:
            return None

        mean_ret = returns.tail(self.SHARPE_WINDOW).mean()
        std_ret = returns.tail(self.SHARPE_WINDOW).std()

        if std_ret == 0 or np.isnan(std_ret):
            return None

        # Annualize: 6 periods per day × 365 days = 2190 periods per year
        annualization = np.sqrt(2190)
        sharpe = (mean_ret / std_ret) * annualization
        return float(sharpe)

    def _get_fear_greed(self, data: dict) -> float | None:
        """Extract latest Fear & Greed value."""
        fg_df = data.get("fear_greed_index:1d")
        if fg_df is None or len(fg_df) == 0:
            return None

        # Try 'value' column; fall back to 'close'
        if "value" in fg_df.columns:
            return float(fg_df["value"].iloc[-1])
        elif "close" in fg_df.columns:
            return float(fg_df["close"].iloc[-1])
        return None

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        btc_df = data.get("BTC/USD:4h")
        eth_df = data.get("ETH/USD:4h")

        if btc_df is None or len(btc_df) < self.SHARPE_WINDOW + 1:
            return []

        btc_sharpe = self._compute_rolling_sharpe(btc_df)
        if btc_sharpe is None:
            return []

        fg_value = self._get_fear_greed(data)

        signals = []
        current_btc = float(btc_df["close"].iloc[-1])
        current_eth = float(eth_df["close"].iloc[-1]) if eth_df is not None and len(eth_df) > 0 else None

        # --- Check hard stops first ---
        if self._btc_in_position and self._btc_entry_price is not None:
            btc_drawdown = (current_btc - self._btc_entry_price) / self._btc_entry_price
            if btc_drawdown <= -self.HARD_STOP_PCT:
                self._btc_in_position = False
                self._btc_entry_price = None
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Hard stop triggered: BTC drawdown {btc_drawdown:.1%} from entry"
                ))

        if self._eth_in_position and self._eth_entry_price is not None and current_eth is not None:
            eth_drawdown = (current_eth - self._eth_entry_price) / self._eth_entry_price
            if eth_drawdown <= -self.HARD_STOP_PCT:
                self._eth_in_position = False
                self._eth_entry_price = None
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Hard stop triggered: ETH drawdown {eth_drawdown:.1%} from entry"
                ))

        # --- Exit logic: momentum waning or sentiment recovered ---
        exit_reason = None
        if btc_sharpe < self.SHARPE_EXIT_THRESHOLD:
            exit_reason = f"Rolling Sharpe {btc_sharpe:.2f} dropped below exit threshold {self.SHARPE_EXIT_THRESHOLD}"
        elif fg_value is not None and fg_value >= self.FG_EXIT_THRESHOLD:
            exit_reason = f"Fear & Greed {fg_value:.0f} recovered to neutral — thesis resolved"

        if exit_reason:
            if self._btc_in_position:
                self._btc_in_position = False
                self._btc_entry_price = None
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
            if self._eth_in_position:
                self._eth_in_position = False
                self._eth_entry_price = None
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=exit_reason
                ))
            return signals

        # --- Entry logic: extreme fear + positive momentum ---
        if fg_value is not None and fg_value <= self.FG_ENTRY_THRESHOLD and btc_sharpe >= self.SHARPE_ENTRY_THRESHOLD:
            if not self._btc_in_position:
                self._btc_in_position = True
                self._btc_entry_price = current_btc
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.BTC_SIZE_PCT,
                    order_type="market",
                    rationale=f"Fear & Greed={fg_value:.0f} (Extreme Fear) AND BTC 14d Sharpe={btc_sharpe:.2f} >= {self.SHARPE_ENTRY_THRESHOLD}. Sentiment lag entry."
                ))

            if not self._eth_in_position and current_eth is not None and eth_df is not None and len(eth_df) > 0:
                self._eth_in_position = True
                self._eth_entry_price = current_eth
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.ETH_SIZE_PCT,
                    order_type="market",
                    rationale=f"ETH beta amplifier: F&G={fg_value:.0f}, BTC Sharpe={btc_sharpe:.2f}. Beta ~1.24 vs BTC for amplified recovery capture."
                ))

        return signals

    def on_fill(self, fill: dict) -> None:
        pair = fill.get("pair", "")
        action = fill.get("action", "")
        price = fill.get("price")

        if "BTC" in pair:
            if action == "buy":
                self._btc_in_position = True
                self._btc_entry_price = price
            elif action in ("sell", "close"):
                self._btc_in_position = False
                self._btc_entry_price = None
        elif "ETH" in pair:
            if action == "buy":
                self._eth_in_position = True
                self._eth_entry_price = price
            elif action in ("sell", "close"):
                self._eth_in_position = False
                self._eth_entry_price = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        return {
            "strategy": self.name(),
            "btc_in_position": self._btc_in_position,
            "eth_in_position": self._eth_in_position,
            "btc_entry_price": self._btc_entry_price,
            "eth_entry_price": self._eth_entry_price,
            "sharpe_entry_threshold": self.SHARPE_ENTRY_THRESHOLD,
            "fg_entry_threshold": self.FG_ENTRY_THRESHOLD,
        }
