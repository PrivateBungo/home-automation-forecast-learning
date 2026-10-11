"""
NL Day-Ahead Price Forecaster
=============================

Wraps the external electricitypriceforecast pipeline (MIT,
github.com/pefijnvandraat/electricitypriceforecast): a daily batch
model of the NL wholesale day-ahead (APX/EPEX) price, trained on
public data (Energy-Charts prices, Open-Meteo weather, TTF gas,
CO2). Run daily, after the day-ahead publication.

What this module adds on top of the external pipeline:

1. Mean/variance conversion: the pipeline outputs a calibrated
   conformal band (p10/p50/p90). We store mean = p50 and
   variance = ((p90 - p10) / (2 * z80))^2, so the band's lead-time
   growth carries over into the variance.

2. Published-price overlay: hours whose day-ahead price is already
   published (rest of today + tomorrow, after ~12:40 local) are
   stored as mean = published price, variance = 0.

3. VAR(1) learning: the pipeline logs its raw out-of-sample
   predictions (state/NL.json pred_log). For every logged hour that
   has since become known we compute the honest residual
   (actual - p50) and learn the internal correlation structure:

       r[h] = d_day + phi[h] * (r[h-1] - d_day) + eps[h]
       eps[h] ~ N(0, sigma_innov[h])

   with d_day a daily factor following an AR(1) across days
   (day_lag1_corr). Correlations are shrunk by n / (n + K) while
   samples are thin. Stored as a 'price_var1_v1' snapshot in
   model.var_matrices for Monte Carlo path generation.

Output (per daily run):
- price_forecast row: hourly mean/variance arrays, wholesale EUR/kWh,
  PRICE_FORECAST_HOURS hours (8 days), all times UTC.
- model.var_matrices 'price_var1_v1' snapshot.

The repo checkout stays pristine (git pull-able); all local state
lives in PRICE_FORECAST_STATE_DIR.
"""

import json
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from solar_forecast.config import (
    PRICE_FORECAST_REPO,
    PRICE_FORECAST_STATE_DIR,
    PRICE_FORECAST_ZONE,
    PRICE_FORECAST_HOURS,
    PRICE_BAND_Z,
    PRICE_VAR_MIN,
    MATRIX_TYPE_PRICE_VAR1,
    PRICE_VAR1_MIN_DAYS,
    PRICE_VAR1_MIN_PAIRS,
    PRICE_VAR1_SHRINK_K,
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)

# pred_log entries are raw model outputs in EUR/MWh; price_forecast
# arrays are wholesale EUR/kWh (project unit standard).
MWH_TO_KWH = 0.001


class PriceForecaster:
    """Daily NL day-ahead price forecast + VAR(1) residual learning."""

    def __init__(self):
        self.db = DatabaseClient()

    # ============================================================
    # MAIN ENTRY POINT
    # ============================================================

    def update(self, test_mode: bool = False) -> Dict[str, Any]:
        """Run the daily price forecast pipeline.

        Args:
            test_mode: If True, print to terminal, no database writes.

        Returns:
            Summary dict of the run.
        """
        result = self._run_external()
        if not result.get('forecast'):
            error = result.get('error', 'unknown')
            logger.error(f"External price pipeline produced no forecast: {error}")
            return {'status': 'failed', 'error': error}

        now = pd.Timestamp.now(tz='UTC').floor('h')
        mean_arr, var_arr, times, n_published = self._to_mean_variance(result, now)
        var1_payload, last_residual_hour = self._learn_var1(result, now)

        summary = {
            'status': 'ok',
            'issued_at': now.isoformat(),
            'start_utc': times[0].isoformat(),
            'horizon_hours': len(times),
            'published_hours': n_published,
            'mean_first_24h': round(float(np.sum(mean_arr[:24])), 4),
            'var1_sample_count': var1_payload.get('n_residuals', 0),
            'var1_days': var1_payload.get('n_days', 0),
            'learning_metrics': result.get('learning'),
            'features': {
                k: result.get(k) for k in (
                    'resid_demand', 'price_lags', 'entso_forecast',
                    'scarcity_feature', 'regional_fundamentals',
                    'conformal_band', 'fallback_hours')
            }
        }

        if test_mode:
            logger.info(f"TEST MODE: would store {len(times)} hours "
                        f"({n_published} already published)")
            logger.info(f"mean first 24h sum: {summary['mean_first_24h']} EUR/kWh")
        else:
            with self.db as db:
                db.save_price_forecast(
                    issued_at=now.isoformat(),
                    start_utc=times[0].isoformat(),
                    horizon_hours=len(times),
                    mean_eur_kwh=[float(v) for v in mean_arr],
                    variance_eur_kwh2=[float(v) for v in var_arr]
                )
                db.insert_var_matrix(
                    matrix_type=MATRIX_TYPE_PRICE_VAR1,
                    calculated_at=now.isoformat(),
                    target_hour=(last_residual_hour or times[0]).isoformat(),
                    payload=var1_payload
                )
            logger.info(
                f"Price forecast stored: issued_at={now.isoformat()}, "
                f"{len(times)} hours, {n_published} published, "
                f"var1 residuals={var1_payload.get('n_residuals', 0)}"
            )
        return summary

    # ============================================================
    # EXTERNAL PIPELINE
    # ============================================================

    def _run_external(self) -> Dict[str, Any]:
        """Run the external pipeline for the configured zone.

        FORECAST_OUT must be set before importing forecaster.run:
        the module resolves its output directory at import time.

        State handling: run_zone loads its prediction log from the
        repo's public/data/state (frozen at clone time) and saves the
        merged log to FORECAST_OUT/state. Copying our accumulated log
        over the repo's file before each run makes the log accumulate
        across runs (in the original CI setup the deploy job commits
        the state back into the repo instead).
        """
        os.makedirs(PRICE_FORECAST_STATE_DIR, exist_ok=True)
        os.environ['FORECAST_OUT'] = PRICE_FORECAST_STATE_DIR
        if PRICE_FORECAST_REPO not in sys.path:
            sys.path.insert(0, PRICE_FORECAST_REPO)
        from forecaster import run as epf_run

        our_state = os.path.join(PRICE_FORECAST_STATE_DIR, 'state',
                                 f'{PRICE_FORECAST_ZONE}.json')
        repo_state = os.path.join(PRICE_FORECAST_REPO, 'public', 'data',
                                  'state', f'{PRICE_FORECAST_ZONE}.json')
        if os.path.exists(our_state):
            os.makedirs(os.path.dirname(repo_state), exist_ok=True)
            shutil.copyfile(our_state, repo_state)
            logger.debug("Synced accumulated prediction log to repo state")

        cfg = epf_run.load_config()
        logger.info(f"Running external price pipeline for zone "
                    f"{PRICE_FORECAST_ZONE}")
        return epf_run.run_zone(PRICE_FORECAST_ZONE, cfg)

    # ============================================================
    # MEAN / VARIANCE CONVERSION
    # ============================================================

    def _to_mean_variance(
        self, result: Dict[str, Any], now: pd.Timestamp
    ) -> Tuple[np.ndarray, np.ndarray, List[pd.Timestamp], int]:
        """Convert the conformal band to mean/variance arrays.

        Hours whose day-ahead price is already published (present in
        the history series) are overlaid: mean = published price,
        variance = 0.
        """
        fc = result['forecast']
        times_all = [pd.Timestamp(t) for t in fc['time']]
        p10 = np.asarray(fc['p10_wholesale_kwh'], dtype=float)
        p50 = np.asarray(fc['p50_wholesale_kwh'], dtype=float)
        p90 = np.asarray(fc['p90_wholesale_kwh'], dtype=float)

        n = min(PRICE_FORECAST_HOURS, len(times_all))
        times = times_all[:n]
        mean = p50[:n].copy()
        var = np.maximum(((p90[:n] - p10[:n]) / (2.0 * PRICE_BAND_Z)) ** 2,
                         PRICE_VAR_MIN)

        actual = self._history_series(result)
        n_published = 0
        if actual is not None and len(actual):
            for i, t in enumerate(times):
                if t in actual.index:
                    mean[i] = float(actual.loc[t])
                    var[i] = 0.0
                    n_published += 1

        mean = np.where(np.isfinite(mean), mean, 0.0)
        var = np.where(np.isfinite(var), var, PRICE_VAR_MIN)
        logger.info(f"Converted {n} hours to mean/var "
                    f"({n_published} overlaid with published prices)")
        return mean, var, times, n_published

    def _history_series(self, result: Dict[str, Any]) -> Optional[pd.Series]:
        """Published wholesale prices (EUR/kWh) from the run, as a Series."""
        hist = result.get('history')
        if not hist:
            return None
        times = [pd.Timestamp(t) for t in hist['time']]
        values = np.asarray(
            [np.nan if v is None else float(v) for v in hist['wholesale_kwh']],
            dtype=float)
        s = pd.Series(values, index=pd.DatetimeIndex(times)).dropna()
        return s[~s.index.duplicated(keep='last')].sort_index()

    # ============================================================
    # VAR(1) RESIDUAL CORRELATION LEARNING
    # ============================================================

    def _learn_var1(
        self, result: Dict[str, Any], now: pd.Timestamp
    ) -> Tuple[Dict[str, Any], Optional[pd.Timestamp]]:
        """Learn the VAR(1) structure of honest out-of-sample residuals.

        Residuals come from the pipeline's prediction log (state/NL.json),
        matched against published prices. pred_log is in EUR/MWh;
        residuals are converted to EUR/kWh.
        """
        zone = PRICE_FORECAST_ZONE
        state_file = os.path.join(PRICE_FORECAST_STATE_DIR, 'state',
                                  f'{zone}.json')
        payload = {
            'unit': 'eur_kwh',
            'phi_by_hour': [0.0] * 24,
            'sigma_innov_by_hour': [0.0] * 24,
            'day_factor_var': 0.0,
            'day_lag1_corr': 0.0,
            'residual_mean': 0.0,
            'residual_std_by_hour': [0.0] * 24,
            'lag24_corr_by_hour': [0.0] * 24,
            'n_residuals': 0,
            'n_days': 0,
            'n_pairs_by_hour': [0] * 24,
            'config': {
                'min_days': PRICE_VAR1_MIN_DAYS,
                'min_pairs': PRICE_VAR1_MIN_PAIRS,
                'shrink_k': PRICE_VAR1_SHRINK_K,
                'band_z': PRICE_BAND_Z,
                'horizon_hours': PRICE_FORECAST_HOURS
            }
        }
        last_residual_hour = None

        if not os.path.exists(state_file):
            logger.info("No prediction log yet; VAR(1) snapshot starts empty")
            return payload, last_residual_hour

        try:
            with open(state_file, encoding='utf-8') as f:
                pred_log = (json.load(f) or {}).get('pred_log', {})
        except (ValueError, OSError):
            logger.warning("Could not read prediction log; VAR(1) not updated")
            return payload, last_residual_hour

        actual = self._history_series(result)
        if actual is None or not len(actual):
            logger.info("No published prices available; VAR(1) not updated")
            return payload, last_residual_hour

        # Honest residuals: logged p50 (EUR/MWh) vs actual (EUR/kWh)
        stamps, residuals = [], []
        for iso, preds in pred_log.items():
            ts = pd.Timestamp(iso)
            if ts > now or ts not in actual.index:
                continue
            p50_mwh = float(preds[1])
            if not np.isfinite(p50_mwh):
                continue
            stamps.append(ts)
            residuals.append(float(actual.loc[ts]) - p50_mwh * MWH_TO_KWH)
        if len(residuals) < 24:
            logger.info(f"Only {len(residuals)} logged residuals; "
                        f"VAR(1) needs more history")
            payload['n_residuals'] = len(residuals)
            return payload, last_residual_hour

        res = pd.Series(residuals, index=pd.DatetimeIndex(stamps)).sort_index()
        res = res[~res.index.duplicated(keep='last')]
        last_residual_hour = res.index[-1]

        # Local (Europe/Amsterdam) hour-of-day and day structure
        cfg_tz = 'Europe/Amsterdam'
        local = res.index.tz_convert(cfg_tz)
        hour = np.asarray(local.hour)
        day = pd.Series(local.date)
        res_mean = float(res.mean())
        res_dev = res - res_mean

        # --- day factor: variance of the daily mean + AR(1) across days ---
        daily = res_dev.groupby(day.values).mean()
        n_days = int(len(daily))
        day_var = 0.0
        day_lag1 = 0.0
        if n_days >= PRICE_VAR1_MIN_DAYS:
            day_var = float(daily.var())
            if n_days >= PRICE_VAR1_MIN_DAYS + 1 and daily.std() > 1e-12:
                x = daily.values[:-1]
                y = daily.values[1:]
                corr = float(np.corrcoef(x, y)[0, 1])
                if np.isfinite(corr):
                    day_lag1 = corr * n_days / (n_days + PRICE_VAR1_SHRINK_K)
        else:
            day_var = float(daily.var()) if n_days >= 3 else 0.0

        # --- per-hour lag-1 correlation within the day ---
        # All neighbour lookups are timestamp-based (not positional) so
        # gaps in the residual series can never misalign a pair.
        phi = [0.0] * 24
        sigma_innov = [0.0] * 24
        std_by_hour = [0.0] * 24
        lag24 = [0.0] * 24
        n_pairs = [0] * 24

        day_arr = np.asarray(day.values)
        res_arr = res_dev.values
        # lag-1h values aligned by timestamp (NaN where missing)
        prev1 = res_dev.reindex(res_dev.index - pd.Timedelta(hours=1))
        # lag-24h values aligned by timestamp
        prev24 = res_dev.reindex(res_dev.index - pd.Timedelta(hours=24))
        # previous timestamp's local date (for same-day pairing)
        prev_day = pd.Series(day_arr, index=res_dev.index).shift(1)
        global_pairs_x, global_pairs_y = [], []
        for h in range(24):
            at_h = hour == h
            std_by_hour[h] = float(res_dev[at_h].std())
            # lag-1: same local day, previous hour
            if h >= 1:
                mask = (at_h & prev1.notna().values
                        & (prev_day.values == day_arr))
                if mask.sum() >= PRICE_VAR1_MIN_PAIRS:
                    y = res_arr[mask]
                    x = prev1.values[mask]
                    n_pairs[h] = int(mask.sum())
                    global_pairs_x.append(x)
                    global_pairs_y.append(y)
                    if np.std(x) > 1e-12 and np.std(y) > 1e-12:
                        corr = float(np.corrcoef(x, y)[0, 1])
                        if np.isfinite(corr):
                            phi[h] = corr * n_pairs[h] / (
                                n_pairs[h] + PRICE_VAR1_SHRINK_K)
                    innov = y - phi[h] * x
                    sigma_innov[h] = float(np.std(innov))
            # hour 0 innovation: deviation from that day's factor
            else:
                day_means = pd.Series(res_arr).groupby(day_arr).transform('mean')
                innov = res_arr[at_h] - day_means[at_h]
                sigma_innov[0] = (float(np.std(innov))
                                 if len(innov) >= 3 else std_by_hour[h])
            # lag-24: same hour previous day
            mask24 = at_h & prev24.notna().values
            if mask24.sum() >= PRICE_VAR1_MIN_PAIRS:
                y = res_arr[mask24]
                x = prev24.values[mask24]
                if np.std(x) > 1e-12 and np.std(y) > 1e-12:
                    corr = float(np.corrcoef(x, y)[0, 1])
                    if np.isfinite(corr):
                        n24 = int(mask24.sum())
                        lag24[h] = corr * n24 / (n24 + PRICE_VAR1_SHRINK_K)

        # global lag-1 fallback for thin hours
        if global_pairs_x:
            gx = np.concatenate(global_pairs_x)
            gy = np.concatenate(global_pairs_y)
            if np.std(gx) > 1e-12 and np.std(gy) > 1e-12:
                gcorr = float(np.corrcoef(gx, gy)[0, 1])
                for h in range(1, 24):
                    if n_pairs[h] < PRICE_VAR1_MIN_PAIRS and np.isfinite(gcorr):
                        phi[h] = gcorr * len(gx) / (
                            len(gx) + PRICE_VAR1_SHRINK_K)

        payload.update({
            'phi_by_hour': [round(v, 5) for v in phi],
            'sigma_innov_by_hour': [round(v, 6) for v in sigma_innov],
            'day_factor_var': round(day_var, 8),
            'day_lag1_corr': round(day_lag1, 5),
            'residual_mean': round(res_mean, 6),
            'residual_std_by_hour': [round(v, 6) for v in std_by_hour],
            'lag24_corr_by_hour': [round(v, 5) for v in lag24],
            'n_residuals': int(len(res)),
            'n_days': n_days,
            'n_pairs_by_hour': n_pairs
        })
        logger.info(
            f"VAR(1) learned from {len(res)} residuals over {n_days} days "
            f"(day_var={day_var:.2e}, day_lag1={day_lag1:.3f})")
        return payload, last_residual_hour
