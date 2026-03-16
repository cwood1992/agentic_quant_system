# Strategy: quant_primary_hyp_002_ethbtc_rotation
# Written by agent quant_primary via write_strategy_code tool.

"""
ETH/BTC Long-Only Rotation Strategy based on Cointegration Spread Z-Score.

Thesis: ETH and BTC are cointegrated (ADF p=0.000538, half-life ~42h at 4h timeframe).
When the spread deviates significantly, mean reversion is highly probable.
Since Kraken margin is unavailable, this is a long-only rotation:
  - Spread z > +2.0: hold BTC (ETH is expensive relative to BTC)
  - Spread z < -2.0: hold ETH (BTC is expensive relative to ETH)
  - |z| < 0.5: exit to cash (near mean, no clear edge)

Cointegration parameters (from 90-day 4h analysis, updated each cycle):
  hedge_ratio = 0.048257  (BTC coefficient: spread = ETH_price - hedge * BTC_price - intercept)
  intercept = -1299.351
  residual_std = 46.955
"""

import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal


class EthBtcRotationStrategy(BaseStrategy):
    """
    Long-only ETH/BTC rotation strategy using cointegration spread z-score.

    Positions:
      - BTC when spread z > +ENTRY_THRESHOLD (ETH overvalued vs BTC)
      - ETH when spread z < -ENTRY_THRESHOLD (BTC overvalued vs ETH)
      - Cash when |z| < EXIT_THRESHOLD after a position is taken

    Cointegration relationship:
      spread = ETH_price - hedge_ratio * BTC_price - intercept
      z_score = (spread - spread_mean) / spread_std

    Parameters are from 90-day 4h analysis. The hedge ratio and std
    will drift slowly; recalibrate every ~30 days.
    """

    # Cointegration parameters (90-day 4h analysis, 2026-03-16)
    HEDGE_RATIO = 0.048257
    INTERCEPT = -1299.351348
    SPREAD_MEAN = 0.0        # residuals are mean-zero by construction
    SPREAD_STD = 46.954938

    # Signal thresholds
    ENTRY_THRESHOLD = 2.0    # |z| > this triggers rotation
    EXIT_THRESHOLD = 0.5     # |z| < this triggers exit to cash
    STOP_THRESHOLD = 4.5     # |z| diverges further — stop loss on spread

    # Position sizing
    POSITION_SIZE = 0.90     # 90% of available capital per trade

    def __init__(self):
        self._current_position = None   # "BTC", "ETH", or None (cash)
        self._entry_z = None            # z-score at entry, for logging
        self._portfolio_state = {}      # updated by on_cycle

    def name(self) -> str:
        return "quant_primary_hyp_002_ethbtc_rotation"

    def required_feeds(self) -> list[str]:
        return ["ETH/USD:4h", "BTC/USD:4h"]

    def _compute_z_score(self, eth_price: float, btc_price: float) -> float:
        """Compute the cointegration spread z-score from current prices."""
        spread = eth_price - self.HEDGE_RATIO * btc_price - self.INTERCEPT
        z = (spread - self.SPREAD_MEAN) / self.SPREAD_STD
        return z

    def _infer_position_from_portfolio(self) -> str | None:
        """
        Attempt to infer current position from portfolio state.
        Falls back to self._current_position if portfolio state not available.
        Works around sir_024 (state persistence) being pending.
        """
        if not self._portfolio_state:
            return self._current_position

        positions = self._portfolio_state.get("positions", {})

        # Check for open BTC or ETH positions
        btc_pos = positions.get("BTC/USD", {})
        eth_pos = positions.get("ETH/USD", {})

        btc_size = btc_pos.get("size", 0) if btc_pos else 0
        eth_size = eth_pos.get("size", 0) if eth_pos else 0

        if btc_size > 0 and eth_size == 0:
            return "BTC"
        elif eth_size > 0 and btc_size == 0:
            return "ETH"
        elif btc_size == 0 and eth_size == 0:
            return None
        else:
            # Both have positions — shouldn't happen, treat as ambiguous
            return self._current_position

    def on_data(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        """
        Called on each new 4h candle. Returns rotation signals based on
        cointegration spread z-score.
        """
        signals = []

        # Require both feeds to have data
        if "ETH/USD" not in data or "BTC/USD" not in data:
            return signals

        eth_df = data["ETH/USD"]
        btc_df = data["BTC/USD"]

        if eth_df.empty or btc_df.empty:
            return signals

        # Need at least 30 periods to compute a meaningful spread
        if len(eth_df) < 30 or len(btc_df) < 30:
            return signals

        # Get current prices (latest close)
        eth_price = float(eth_df["close"].iloc[-1])
        btc_price = float(btc_df["close"].iloc[-1])

        # Compute z-score
        z = self._compute_z_score(eth_price, btc_price)

        # Infer current position (workaround for sir_024)
        current_pos = self._infer_position_from_portfolio()

        # --- Signal logic ---

        if z > self.ENTRY_THRESHOLD:
            # ETH is expensive relative to BTC → should hold BTC
            if current_pos == "ETH":
                # Close ETH position first, then buy BTC
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} > {self.ENTRY_THRESHOLD}: ETH overvalued vs BTC. Closing ETH to rotate to BTC."
                ))
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} > {self.ENTRY_THRESHOLD}: Rotating to BTC (undervalued leg). ETH_price={eth_price:.2f}, BTC_price={btc_price:.2f}"
                ))
                self._current_position = "BTC"
                self._entry_z = z

            elif current_pos is None:
                # Enter BTC from cash
                signals.append(Signal(
                    action="buy",
                    pair="BTC/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} > {self.ENTRY_THRESHOLD}: ETH overvalued, entering BTC from cash. ETH_price={eth_price:.2f}, BTC_price={btc_price:.2f}"
                ))
                self._current_position = "BTC"
                self._entry_z = z

            elif current_pos == "BTC":
                # Already in BTC — check stop loss (spread diverging further)
                if z > self.STOP_THRESHOLD:
                    # Unexpected: spread has blown out even further
                    # This means ETH keeps outperforming — risk of prolonged divergence
                    signals.append(Signal(
                        action="close",
                        pair="BTC/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=f"STOP: Spread z={z:.2f} > {self.STOP_THRESHOLD}. Cointegration may be breaking. Exiting BTC position."
                    ))
                    self._current_position = None
                    self._entry_z = None
                # else: hold BTC, no action needed

        elif z < -self.ENTRY_THRESHOLD:
            # BTC is expensive relative to ETH → should hold ETH
            if current_pos == "BTC":
                # Close BTC, buy ETH
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} < -{self.ENTRY_THRESHOLD}: BTC overvalued vs ETH. Closing BTC to rotate to ETH."
                ))
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} < -{self.ENTRY_THRESHOLD}: Rotating to ETH (undervalued leg). ETH_price={eth_price:.2f}, BTC_price={btc_price:.2f}"
                ))
                self._current_position = "ETH"
                self._entry_z = z

            elif current_pos is None:
                # Enter ETH from cash
                signals.append(Signal(
                    action="buy",
                    pair="ETH/USD",
                    size_pct=self.POSITION_SIZE,
                    order_type="market",
                    rationale=f"Spread z={z:.2f} < -{self.ENTRY_THRESHOLD}: BTC overvalued, entering ETH from cash. ETH_price={eth_price:.2f}, BTC_price={btc_price:.2f}"
                ))
                self._current_position = "ETH"
                self._entry_z = z

            elif current_pos == "ETH":
                # Already in ETH — check stop loss
                if z < -self.STOP_THRESHOLD:
                    signals.append(Signal(
                        action="close",
                        pair="ETH/USD",
                        size_pct=1.0,
                        order_type="market",
                        rationale=f"STOP: Spread z={z:.2f} < -{self.STOP_THRESHOLD}. Cointegration may be breaking. Exiting ETH position."
                    ))
                    self._current_position = None
                    self._entry_z = None
                # else: hold ETH, no action needed

        elif abs(z) < self.EXIT_THRESHOLD:
            # Spread near mean — exit to cash if holding a position
            if current_pos == "BTC":
                signals.append(Signal(
                    action="close",
                    pair="BTC/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread z={z:.2f}: Near mean (|z| < {self.EXIT_THRESHOLD}). Taking profit, exiting to cash. Entry was z={self._entry_z:.2f} vs BTC."
                ))
                self._current_position = None
                self._entry_z = None

            elif current_pos == "ETH":
                signals.append(Signal(
                    action="close",
                    pair="ETH/USD",
                    size_pct=1.0,
                    order_type="market",
                    rationale=f"Spread z={z:.2f}: Near mean (|z| < {self.EXIT_THRESHOLD}). Taking profit, exiting to cash. Entry was z={self._entry_z:.2f} vs ETH."
                ))
                self._current_position = None
                self._entry_z = None

        # Between EXIT_THRESHOLD and ENTRY_THRESHOLD: hold current position
        return signals

    def on_fill(self, fill: dict) -> None:
        """Update internal state on fill confirmation."""
        pair = fill.get("pair", "")
        action = fill.get("action", "")

        if action == "buy":
            if "BTC" in pair:
                self._current_position = "BTC"
            elif "ETH" in pair:
                self._current_position = "ETH"
        elif action in ("sell", "close"):
            # Only clear position if we're closing the asset we think we hold
            if "BTC" in pair and self._current_position == "BTC":
                self._current_position = None
                self._entry_z = None
            elif "ETH" in pair and self._current_position == "ETH":
                self._current_position = None
                self._entry_z = None

    def on_cycle(self, cycle_number: int, portfolio_state: dict) -> dict:
        """
        Called each cycle with portfolio state. Store for position inference.
        This is the primary workaround for sir_024 (state persistence pending).
        """
        self._portfolio_state = portfolio_state

        # Infer and sync position from portfolio
        inferred = self._infer_position_from_portfolio()
        if inferred != self._current_position:
            # Portfolio disagrees with internal state — trust portfolio
            self._current_position = inferred

        # Compute current z-score for stats
        positions = portfolio_state.get("positions", {})
        eth_price = portfolio_state.get("prices", {}).get("ETH/USD", None)
        btc_price = portfolio_state.get("prices", {}).get("BTC/USD", None)

        stats = {
            "current_position": self._current_position,
            "entry_z": self._entry_z,
        }

        if eth_price and btc_price:
            z = self._compute_z_score(eth_price, btc_price)
            stats["current_z"] = round(z, 4)
            stats["spread"] = round(
                eth_price - self.HEDGE_RATIO * btc_price - self.INTERCEPT, 4
            )

        return stats
