"""Analysis engine for the agentic quant trading system.

Provides statistical analysis functions over OHLCV data stored in SQLite.
Processes pending analysis_request events and stores results to disk.
"""

import json
import os
from datetime import datetime, timezone, timedelta

import numpy as np

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("data_collector.analysis")


class AnalysisEngine:
    """Statistical analysis over OHLCV price data.

    Args:
        db_path: Path to the SQLite database file.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Helper: load close prices for a pair
    # ------------------------------------------------------------------

    def _load_closes(
        self, pair: str, timeframe: str, lookback_days: int
    ) -> np.ndarray:
        """Load close prices from ohlcv_cache for a pair/timeframe.

        Returns:
            1-D numpy array of close prices ordered by time ascending.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=lookback_days)
        ).isoformat()

        conn = get_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT close FROM ohlcv_cache
                WHERE pair = ? AND timeframe = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (pair, timeframe, cutoff),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return np.array([], dtype=np.float64)

        return np.array([r["close"] for r in rows], dtype=np.float64)

    def _load_returns(
        self, pair: str, timeframe: str, lookback_days: int
    ) -> np.ndarray:
        """Load log returns for a pair/timeframe."""
        closes = self._load_closes(pair, timeframe, lookback_days)
        if len(closes) < 2:
            return np.array([], dtype=np.float64)
        return np.diff(np.log(closes))

    # ------------------------------------------------------------------
    # 1. Correlation matrix
    # ------------------------------------------------------------------

    def correlation(
        self, pairs: list[str], timeframe: str, lookback_days: int = 30
    ) -> dict:
        """Compute a correlation matrix of log returns across pairs.

        Args:
            pairs: List of trading pairs.
            timeframe: OHLCV timeframe (e.g. '1h').
            lookback_days: Number of days to look back.

        Returns:
            Dict with keys: pairs, matrix (list of lists), timestamps_used.
        """
        returns_map: dict[str, np.ndarray] = {}
        for pair in pairs:
            ret = self._load_returns(pair, timeframe, lookback_days)
            if len(ret) > 0:
                returns_map[pair] = ret

        valid_pairs = list(returns_map.keys())
        if len(valid_pairs) < 2:
            return {
                "pairs": valid_pairs,
                "matrix": [],
                "error": "Need at least 2 pairs with data",
            }

        # Align lengths to shortest series
        min_len = min(len(r) for r in returns_map.values())
        aligned = np.column_stack(
            [returns_map[p][-min_len:] for p in valid_pairs]
        )

        corr = np.corrcoef(aligned, rowvar=False)

        return {
            "pairs": valid_pairs,
            "matrix": corr.tolist(),
            "timestamps_used": min_len,
        }

    # ------------------------------------------------------------------
    # 2. Rolling Sharpe ratio
    # ------------------------------------------------------------------

    def rolling_sharpe(
        self,
        pair: str,
        timeframe: str,
        lookback_days: int = 30,
        window: int | None = None,
        window_days: int = 14,
    ) -> dict:
        """Compute rolling Sharpe ratio over a window.

        The Sharpe ratio uses zero as the risk-free rate and annualises
        based on the number of periods per year implied by the timeframe.

        Args:
            pair: Trading pair.
            timeframe: OHLCV timeframe.
            lookback_days: Days of data to use.
            window: Rolling window size in candles (overrides window_days).
            window_days: Rolling window size in days (default 14).

        Returns:
            Dict with keys: pair, window, values (list of floats), count.
        """
        if window is None:
            annualisation = self._annualisation_factor(timeframe)
            candles_per_day = annualisation / 365
            window = max(2, int(window_days * candles_per_day))
        returns = self._load_returns(pair, timeframe, lookback_days)
        if len(returns) < window:
            return {
                "pair": pair,
                "window": window,
                "values": [],
                "error": f"Insufficient data: {len(returns)} returns < window {window}",
            }

        # Annualisation factor based on timeframe
        annualisation = self._annualisation_factor(timeframe)

        sharpe_values = []
        for i in range(window, len(returns) + 1):
            w = returns[i - window : i]
            std = float(np.std(w, ddof=1))
            if std == 0:
                sharpe_values.append(0.0)
            else:
                sharpe_values.append(
                    float(np.mean(w) / std * np.sqrt(annualisation))
                )

        return {
            "pair": pair,
            "window": window,
            "values": sharpe_values,
            "count": len(sharpe_values),
        }

    # ------------------------------------------------------------------
    # 3. Autocorrelation
    # ------------------------------------------------------------------

    def autocorrelation(
        self,
        pair: str,
        timeframe: str,
        lookback_days: int = 30,
        max_lag: int = 20,
    ) -> dict:
        """Compute autocorrelation of returns at lags 1..max_lag.

        Args:
            pair: Trading pair.
            timeframe: OHLCV timeframe.
            lookback_days: Days of data to use.
            max_lag: Maximum lag to compute.

        Returns:
            Dict with keys: pair, lags (dict mapping lag -> autocorrelation).
        """
        returns = self._load_returns(pair, timeframe, lookback_days)
        if len(returns) < max_lag + 1:
            return {
                "pair": pair,
                "lags": {},
                "error": f"Insufficient data: {len(returns)} returns < max_lag+1",
            }

        mean = float(np.mean(returns))
        var = float(np.var(returns))
        if var == 0:
            return {"pair": pair, "lags": {str(i): 0.0 for i in range(1, max_lag + 1)}}

        lags = {}
        n = len(returns)
        for lag in range(1, max_lag + 1):
            cov = float(np.mean((returns[: n - lag] - mean) * (returns[lag:] - mean)))
            lags[str(lag)] = round(cov / var, 6)

        # Ljung-Box p-values: p<0.05 means that lag's autocorrelation is significant
        ljung_box_pvalues: dict = {}
        try:
            from statsmodels.stats.diagnostic import acorr_ljungbox
            max_test_lag = min(max_lag, n // 5)
            if max_test_lag >= 1:
                lb = acorr_ljungbox(
                    returns, lags=list(range(1, max_test_lag + 1)), return_df=True
                )
                ljung_box_pvalues = {
                    str(int(lag)): round(float(pval), 6)
                    for lag, pval in lb["lb_pvalue"].items()
                }
        except Exception as exc:
            logger.warning("Ljung-Box test failed: %s", exc)

        return {
            "pair": pair,
            "lags": lags,
            "ljung_box_pvalues": ljung_box_pvalues,
            "ljung_box_note": "p<0.05 means autocorrelation at that lag is statistically significant",
        }

    # ------------------------------------------------------------------
    # 4. Return distribution
    # ------------------------------------------------------------------

    def distribution(
        self, pair: str, timeframe: str, lookback_days: int = 30
    ) -> dict:
        """Compute distribution statistics of log returns.

        Args:
            pair: Trading pair.
            timeframe: OHLCV timeframe.
            lookback_days: Days of data to use.

        Returns:
            Dict with mean, std, skew, kurtosis, and percentiles.
        """
        returns = self._load_returns(pair, timeframe, lookback_days)
        if len(returns) < 3:
            return {
                "pair": pair,
                "error": f"Insufficient data: {len(returns)} returns",
            }

        mean = float(np.mean(returns))
        std = float(np.std(returns, ddof=1))
        n = len(returns)

        # Skewness (Fisher)
        if std == 0:
            skew = 0.0
            kurtosis = 0.0
        else:
            centered = returns - mean
            skew = float(
                (n / ((n - 1) * (n - 2))) * np.sum((centered / std) ** 3)
            ) if n > 2 else 0.0
            # Excess kurtosis (Fisher)
            if n > 3:
                k4 = float(np.mean(centered ** 4) / (std ** 4))
                kurtosis = k4 - 3.0
            else:
                kurtosis = 0.0

        percentiles = {
            str(p): round(float(np.percentile(returns, p)), 8)
            for p in [1, 5, 10, 25, 50, 75, 90, 95, 99]
        }

        return {
            "pair": pair,
            "count": n,
            "mean": round(mean, 8),
            "std": round(std, 8),
            "skew": round(skew, 4),
            "kurtosis": round(kurtosis, 4),
            "percentiles": percentiles,
        }

    # ------------------------------------------------------------------
    # 5. Cointegration (simplified Engle-Granger)
    # ------------------------------------------------------------------

    def cointegration(
        self, pairs: list[str], timeframe: str, lookback_days: int = 30
    ) -> dict:
        """Simplified Engle-Granger cointegration test between two pairs.

        Fits OLS of pair1 ~ pair2, then tests residuals for stationarity
        using the ADF-like heuristic (residual autocorrelation check).

        Args:
            pairs: Exactly two trading pairs.
            timeframe: OHLCV timeframe.
            lookback_days: Days of data to use.

        Returns:
            Dict with hedge_ratio, residual_mean, residual_std,
            mean_reversion_score (0-1 where higher = more mean-reverting).
        """
        if len(pairs) != 2:
            return {"error": "Cointegration requires exactly 2 pairs"}

        closes_a = self._load_closes(pairs[0], timeframe, lookback_days)
        closes_b = self._load_closes(pairs[1], timeframe, lookback_days)

        if len(closes_a) < 20 or len(closes_b) < 20:
            return {"error": "Insufficient data for cointegration test"}

        # Align to same length
        min_len = min(len(closes_a), len(closes_b))
        a = closes_a[-min_len:]
        b = closes_b[-min_len:]

        # OLS: a = beta * b + alpha + residual
        # Using numpy least squares
        X = np.column_stack([b, np.ones(min_len)])
        result = np.linalg.lstsq(X, a, rcond=None)
        beta, alpha = result[0]

        residuals = a - (beta * b + alpha)

        residual_mean = float(np.mean(residuals))
        residual_std = float(np.std(residuals, ddof=1))

        # Mean-reversion score: autocorrelation of residuals at lag 1
        # Strongly negative lag-1 autocorr => mean-reverting
        if len(residuals) > 1 and residual_std > 0:
            r_centered = residuals - residual_mean
            var = float(np.var(residuals))
            if var > 0:
                lag1_autocorr = float(
                    np.mean(r_centered[:-1] * r_centered[1:]) / var
                )
            else:
                lag1_autocorr = 0.0
            # Map autocorrelation to 0-1 score:
            # -1 autocorr -> 1.0 score, +1 autocorr -> 0.0 score
            mean_reversion_score = max(0.0, min(1.0, (1.0 - lag1_autocorr) / 2.0))
        else:
            lag1_autocorr = 0.0
            mean_reversion_score = 0.5

        # Half-life from AR(1): periods for spread to revert halfway
        if 0 < lag1_autocorr < 1:
            half_life_periods = round(-np.log(2) / np.log(lag1_autocorr), 2)
        else:
            half_life_periods = None  # diverging (>=1) or already mean-reverting (<=0)

        # ADF test on residuals for stationarity
        adf_statistic = None
        adf_pvalue = None
        try:
            from statsmodels.tsa.stattools import adfuller
            adf_result = adfuller(residuals, autolag="AIC")
            adf_statistic = round(float(adf_result[0]), 6)
            adf_pvalue = round(float(adf_result[1]), 6)
        except Exception as exc:
            logger.warning("ADF test failed: %s", exc)

        return {
            "pair_a": pairs[0],
            "pair_b": pairs[1],
            "hedge_ratio": round(float(beta), 6),
            "intercept": round(float(alpha), 6),
            "residual_mean": round(residual_mean, 6),
            "residual_std": round(residual_std, 6),
            "lag1_autocorrelation": round(lag1_autocorr, 6),
            "mean_reversion_score": round(mean_reversion_score, 4),
            "n_observations": min_len,
            "half_life_periods": half_life_periods,
            "adf_statistic": adf_statistic,
            "adf_pvalue": adf_pvalue,
            "adf_note": "p<0.05 rejects unit root — spread is stationary (supports mean reversion)",
        }

    # ------------------------------------------------------------------
    # 6. Rolling beta
    # ------------------------------------------------------------------

    def rolling_beta(
        self,
        target: str,
        reference: str,
        timeframe: str,
        window_days: int = 30,
        lookback_days: int = 180,
    ) -> dict:
        """Compute rolling beta of target returns vs reference returns.

        At each rolling window, beta = cov(target, reference) / var(reference).
        A beta > 1 means the target amplifies reference moves.

        Args:
            target: Trading pair whose beta is being measured.
            reference: Reference pair (e.g. 'BTC/USD').
            timeframe: OHLCV timeframe.
            window_days: Rolling window width in days.
            lookback_days: Total history to load.

        Returns:
            Dict with current_beta, mean_beta, min_beta, max_beta, beta_std, n_windows.
        """
        target_closes = self._load_closes(target, timeframe, lookback_days)
        ref_closes = self._load_closes(reference, timeframe, lookback_days)

        n = min(len(target_closes), len(ref_closes))
        if n < 10:
            return {"error": f"Insufficient data: {n} observations for {target}/{reference}"}

        target_returns = np.diff(np.log(target_closes[-n:]))
        ref_returns = np.diff(np.log(ref_closes[-n:]))

        candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 24)
        window = max(10, window_days * candles_per_day)

        betas = []
        for i in range(window, len(target_returns) + 1):
            t = target_returns[i - window : i]
            r = ref_returns[i - window : i]
            var_r = float(np.var(r, ddof=1))
            if var_r > 0:
                betas.append(float(np.cov(t, r, ddof=1)[0, 1] / var_r))

        if not betas:
            return {"error": f"Window ({window} candles) too large for available data ({len(target_returns)})"}

        return {
            "target": target,
            "reference": reference,
            "timeframe": timeframe,
            "window_days": window_days,
            "current_beta": round(betas[-1], 4),
            "mean_beta": round(float(np.mean(betas)), 4),
            "min_beta": round(float(np.min(betas)), 4),
            "max_beta": round(float(np.max(betas)), 4),
            "beta_std": round(float(np.std(betas, ddof=1)), 4),
            "n_windows": len(betas),
            "note": "beta>1 means target amplifies reference moves; beta<1 means dampened",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _annualisation_factor(timeframe: str) -> float:
        """Return the number of periods per year for a given timeframe."""
        mapping = {
            "1m": 525960,
            "5m": 105192,
            "15m": 35064,
            "1h": 8760,
            "4h": 2190,
            "1d": 365,
            "1w": 52,
        }
        return mapping.get(timeframe, 8760)

    # ------------------------------------------------------------------
    # Process pending analysis requests from events table
    # ------------------------------------------------------------------


def process_pending_analysis(db_path: str) -> list[str]:
    """Pick up analysis_request events, run them, store results.

    Looks for events with event_type='analysis_request' that have not
    been processed. Runs the requested analysis and saves the result
    as a JSON file in data/analysis/.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        List of result file paths written.
    """
    conn = get_db(db_path)
    result_paths: list[str] = []

    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, agent_id, payload
            FROM events
            WHERE event_type = 'analysis_request'
            ORDER BY timestamp ASC
            """
        ).fetchall()

        if not rows:
            return result_paths

        engine = AnalysisEngine(db_path)
        output_dir = os.path.join("data", "analysis")
        os.makedirs(output_dir, exist_ok=True)

        for row in rows:
            event_id = row["id"]
            try:
                request = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid analysis_request payload for event %d", event_id)
                continue

            analysis_type = request.get("type", "")
            result: dict = {}

            try:
                if analysis_type == "correlation":
                    result = engine.correlation(
                        pairs=request.get("pairs", []),
                        timeframe=request.get("timeframe", "1h"),
                        lookback_days=request.get("lookback_days", 30),
                    )
                elif analysis_type == "rolling_sharpe":
                    result = engine.rolling_sharpe(
                        pair=request.get("pair", ""),
                        timeframe=request.get("timeframe", "1h"),
                        lookback_days=request.get("lookback_days", 30),
                        window=request.get("window", 24),
                    )
                elif analysis_type == "autocorrelation":
                    result = engine.autocorrelation(
                        pair=request.get("pair", ""),
                        timeframe=request.get("timeframe", "1h"),
                        lookback_days=request.get("lookback_days", 30),
                        max_lag=request.get("max_lag", 20),
                    )
                elif analysis_type == "distribution":
                    result = engine.distribution(
                        pair=request.get("pair", ""),
                        timeframe=request.get("timeframe", "1h"),
                        lookback_days=request.get("lookback_days", 30),
                    )
                elif analysis_type == "cointegration":
                    result = engine.cointegration(
                        pairs=request.get("pairs", []),
                        timeframe=request.get("timeframe", "1h"),
                        lookback_days=request.get("lookback_days", 30),
                    )
                else:
                    result = {"error": f"Unknown analysis type: {analysis_type}"}
            except Exception as exc:
                logger.exception("Error running analysis for event %d", event_id)
                result = {"error": str(exc)}

            # Write result file
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"{analysis_type}_{event_id}_{ts}.json"
            filepath = os.path.join(output_dir, filename)

            output = {
                "event_id": event_id,
                "request": request,
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

            with open(filepath, "w") as f:
                json.dump(output, f, indent=2)

            result_paths.append(filepath)

            # Mark event as processed by updating payload
            conn.execute(
                """
                UPDATE events SET payload = ?
                WHERE id = ?
                """,
                (json.dumps({**request, "_processed": True}), event_id),
            )

        conn.commit()

    finally:
        conn.close()

    return result_paths
