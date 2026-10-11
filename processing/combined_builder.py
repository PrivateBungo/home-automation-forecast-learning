"""
Combined Forecast Builder
=========================

Builds the single combined east+west forecast per issue time:

- Corrected mean per 15-min block: raw forecast + learned absolute
  error correction (shrunk toward zero while a cell has few samples)
- Combined variance per block: var_east + var_west + 2*cov_east_west
- Cross-roof error correlation per block: covariance / (sigma_e * sigma_w)

All learned statistics exist only at the 12 Fibonacci horizon knots per
(time_of_day_block, roof); values for the other 660 blocks are
interpolated between knots with log-horizon weights (linear in value).

Written to solar_combined_forecast as three 672-element arrays keyed by
the issue time. All values in kWh / kWh^2; all times UTC.
"""

import logging
import math
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from solar_forecast.config import (
    TOTAL_FORECAST_BLOCKS,
    FIBONACCI_HORIZONS_BLOCKS,
    COMBINED_SHRINK_K,
    COMBINED_SIGMA_EPS,
    COMBINED_CORR_CLAMP
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)

ROOFS = ('east', 'west')
N_KNOTS = len(FIBONACCI_HORIZONS_BLOCKS)


class CombinedBuilder:
    """Builds the combined forecast vector from raw forecasts + error model."""
    
    def __init__(self):
        self.db = DatabaseClient()
    
    def build_and_store(self, vectors: Dict = None) -> Dict[str, any]:
        """Build the combined forecast and store it.
        
        Args:
            vectors: optional {roof: {'timestamp', 'vector'}}; when omitted
                     the latest stored vectors are used. Both roofs must
                     share the same issue timestamp.
        """
        with self.db:
            result = self.build(vectors)
            self.db.save_combined_forecast(
                issued_at=result['issued_at'].isoformat(),
                mean_kwh=result['mean_kwh'],
                variance_kwh2=result['variance_kwh2'],
                corr_ew=result['corr_ew']
            )
            logger.info(
                f"Combined forecast stored: issued_at={result['issued_at'].isoformat()}, "
                f"week total={result['total_mean_kwh']:.2f} kWh"
            )
            return result
    
    def build(self, vectors: Dict = None) -> Dict[str, any]:
        """Build the combined forecast arrays (no DB write).
        
        Requires an open connection (run inside `with self.db:`).
        """
        if vectors is None:
            vectors = self._get_latest_vectors()
        issued_at = self._common_timestamp(vectors)
        
        accuracy, covariance = self._load_error_model()
        
        mean_arr: List[float] = []
        var_arr: List[float] = []
        corr_arr: List[float] = []
        
        for roof in ROOFS:
            if len(vectors[roof]['vector']) < TOTAL_FORECAST_BLOCKS:
                raise ValueError(
                    f"{roof} forecast vector too short: "
                    f"{len(vectors[roof]['vector'])} < {TOTAL_FORECAST_BLOCKS}"
                )
        
        for i in range(TOTAL_FORECAST_BLOCKS):
            horizon = i + 1  # vector[i] is the (i+1)-th block ahead
            target_time = issued_at + timedelta(minutes=15 * horizon)
            tod = target_time.hour * 4 + target_time.minute // 15
            
            corrected = {}
            variances = {}
            for roof in ROOFS:
                raw = float(vectors[roof]['vector'][i])
                knots = accuracy['mean_abs'].get((roof, tod))
                mu, n = self._interp_knots(knots, horizon)
                correction = mu * n / (n + COMBINED_SHRINK_K) if mu else 0.0
                corrected[roof] = max(0.0, raw + correction)
                
                knots = accuracy['variance'].get((roof, tod))
                var, _ = self._interp_knots(knots, horizon)
                variances[roof] = max(0.0, var)
            
            cov, n_cov = self._interp_knots(covariance.get(tod), horizon)
            
            # Shrink covariance toward 0 while the cell is immature (few
            # samples), same rationale as the mean correction: immature
            # cells hit |corr| ~ 1 as an EMA artifact, which would inflate
            # or collapse the combined variance.
            if cov:
                cov = cov * n_cov / (n_cov + COMBINED_SHRINK_K)
            
            # Cross-roof correlation; clamped because immature cells hit
            # |corr| = 1 exactly as an EMA artifact (2 samples). The
            # clamped value feeds the variance too, keeping the stored
            # (mean, variance, correlation) triple consistent.
            denom = math.sqrt(variances['east']) * math.sqrt(variances['west'])
            if denom > COMBINED_SIGMA_EPS:
                corr = max(-COMBINED_CORR_CLAMP,
                           min(COMBINED_CORR_CLAMP, cov / denom))
                cov = corr * denom
            else:
                corr = 0.0
            
            mean_arr.append(corrected['east'] + corrected['west'])
            var_arr.append(max(
                0.0,
                variances['east'] + variances['west'] + 2.0 * cov
            ))
            corr_arr.append(corr)
        
        return {
            'issued_at': issued_at,
            'mean_kwh': mean_arr,
            'variance_kwh2': var_arr,
            'corr_ew': corr_arr,
            'total_mean_kwh': sum(mean_arr),
            'total_raw_kwh': sum(
                float(vectors[roof]['vector'][i])
                for roof in ROOFS for i in range(TOTAL_FORECAST_BLOCKS)
            )
        }
    
    # ============================================================
    # INPUT VECTORS
    # ============================================================
    
    def _get_latest_vectors(self) -> Dict:
        """Latest stored forecast vectors for both roofs."""
        vectors = {}
        for roof in ROOFS:
            latest = self.db.get_latest_forecast_vectors(roof)
            if roof not in latest:
                raise ValueError(f"No stored forecast vector for roof {roof}")
            vectors[roof] = latest[roof]
        return vectors
    
    def _common_timestamp(self, vectors: Dict):
        """Both roofs' vectors must come from the same issue time."""
        ts = {roof: vectors[roof]['timestamp'] for roof in ROOFS}
        if len(set(ts.values())) != 1:
            raise ValueError(f"Roof vectors have different issue times: {ts}")
        return list(ts.values())[0]
    
    # ============================================================
    # ERROR MODEL LOADING
    # ============================================================
    
    def _load_error_model(self):
        """Load all learned cells, re-indexed per (roof, tod) / tod with
        knot-aligned lists of (value, count) or None."""
        accuracy = {
            'mean_abs': {},
            'variance': {}
        }
        for row in self.db.query(
            "SELECT roof, time_of_day_block, forecast_horizon_block, "
            "mean_abs_error, variance, sample_count FROM solar_accuracy"
        ):
            roof, tod, horizon, mean_abs, variance, count = row
            if count <= 0:
                continue
            for key, value in (('mean_abs', mean_abs),
                               ('variance', variance)):
                knots = accuracy[key].setdefault((roof, tod),
                                                 [None] * N_KNOTS)
                try:
                    idx = FIBONACCI_HORIZONS_BLOCKS.index(horizon)
                except ValueError:
                    continue
                knots[idx] = (float(value), count)
        
        covariance: Dict[int, List] = {}
        for row in self.db.query(
            "SELECT time_of_day_block, forecast_horizon_block, "
            "covariance, sample_count FROM solar_covariance"
        ):
            tod, horizon, cov, count = row
            if count <= 0:
                continue
            try:
                idx = FIBONACCI_HORIZONS_BLOCKS.index(horizon)
            except ValueError:
                continue
            knots = covariance.setdefault(tod, [None] * N_KNOTS)
            knots[idx] = (float(cov), count)
        
        logger.info(
            f"Error model loaded: "
            f"{sum(1 for k in accuracy['mean_abs'].values() if any(k))} east/west tod columns, "
            f"{sum(1 for k in covariance.values() if any(k))} covariance tod columns"
        )
        return accuracy, covariance
    
    # ============================================================
    # HORIZON INTERPOLATION
    # ============================================================
    
    def _interp_knots(self, knot_vals: Optional[List], horizon: int) -> Tuple[float, float]:
        """Interpolate (value, count) at the given horizon from the
        Fibonacci knots.
        
        Log-horizon weights, linear in value. Below the first available
        knot (the first hour) the nearest knot is used; above the last,
        likewise. Returns (0.0, 0) when no knot has data for this tod.
        """
        if not knot_vals:
            return 0.0, 0.0
        
        available = [
            (H, v) for H, v in zip(FIBONACCI_HORIZONS_BLOCKS, knot_vals)
            if v is not None
        ]
        if not available:
            return 0.0, 0.0
        
        if horizon <= available[0][0]:
            value, count = available[0][1]
            return value, count
        if horizon >= available[-1][0]:
            value, count = available[-1][1]
            return value, count
        
        for (h1, (v1, n1)), (h2, (v2, n2)) in zip(available, available[1:]):
            if h1 <= horizon <= h2:
                if horizon == h1:
                    return v1, n1
                if horizon == h2:
                    return v2, n2
                w = (math.log(horizon) - math.log(h1)) / (math.log(h2) - math.log(h1))
                return ((1 - w) * v1 + w * v2, (1 - w) * n1 + w * n2)
        
        value, count = available[-1][1]
        return value, count
