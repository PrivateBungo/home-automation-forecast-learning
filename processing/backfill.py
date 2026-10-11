"""
Backfill
========

Recomputes the accuracy model (mean errors, variance, correlations,
reference magnitudes) from stored history: forecast vectors and actual
production already in the database.

Replays history chronologically with exactly the same EMA math and the
same skip rules as the live learner, so a backfillled model is
indistinguishable from one built by live operation over the same period.

Usage:
    python3 -m solar_forecast.processing.backfill
    (or: python3 /home/gijs/solar_forecast/run_backfill.py)

Deletes and rebuilds:
    solar_accuracy, solar_correlation_local,
    solar_correlation_daily, solar_covariance, solar_reference_magnitude

Raw data (solar_forecast_vectors, solar_actual_production) is never
modified.
"""

import logging
import math
from bisect import bisect_left
from datetime import timedelta, timezone
from typing import Dict, List, Optional, Tuple

from solar_forecast.config import (
    EMA_ALPHA,
    REF_EMA_ALPHA,
    REFERENCE_FLOOR_KWH,
    REFERENCE_MIN_SAMPLES,
    FIBONACCI_HORIZONS_BLOCKS,
    LOCAL_WINDOW_BLOCKS,
    DAILY_OFFSET_BLOCKS
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)

ROOFS = ('east', 'west')
FORECAST_TOLERANCE_MINUTES = 15


class Backfiller:
    """Rebuilds learning tables from stored forecast/actual history."""
    
    def __init__(self):
        self.db = DatabaseClient()
        
        # Raw history
        self.actuals_by_time: Dict = {}          # T -> {roof: kwh}
        self.vectors_by_roof: Dict = {}          # roof -> [(ts, vector)]
        self.vector_times: Dict = {}             # roof -> [ts] (sorted, parallel)
        
        # EMA states (in memory, persisted once at the end)
        self.state_accuracy: Dict = {}           # (tod, hor, roof) -> dict
        self.state_corr_local: Dict = {}          # (tod, hor, roof) -> dict
        self.state_corr_daily: Dict = {}          # (tod, hor, roof) -> dict
        self.state_covariance: Dict = {}          # (tod, hor) -> dict
        self.state_reference: Dict = {}          # (tod, roof) -> dict
    
    # ============================================================
    # HISTORY LOADING
    # ============================================================
    
    def load_history(self):
        """Load all stored forecast vectors and actual production."""
        results = self.db.query(
            "SELECT timestamp, roof, forecast_vector FROM solar_forecast_vectors"
        )
        for ts, roof, vector in results:
            # solar_forecast_vectors.timestamp is TIMESTAMP (naive);
            # actuals are TIMESTAMPTZ. Normalize to aware UTC.
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            self.vectors_by_roof.setdefault(roof, []).append((ts, vector))
        for roof, items in self.vectors_by_roof.items():
            items.sort(key=lambda item: item[0])
            self.vector_times[roof] = [ts for ts, _ in items]
        
        results = self.db.query(
            "SELECT timestamp, roof, energy_kwh FROM solar_actual_production"
        )
        for ts, roof, kwh in results:
            self.actuals_by_time.setdefault(ts, {})[roof] = float(kwh)
        
        n_vectors = sum(len(v) for v in self.vectors_by_roof.values())
        logger.info(
            f"Loaded {n_vectors} forecast vectors, "
            f"{len(self.actuals_by_time)} actual-production timestamps"
        )
    
    def _find_vectors(self, target) -> Optional[Dict[str, List[float]]]:
        """Find forecast vectors nearest to target time (within tolerance).
        
        Same semantics as the live learner: requires BOTH roofs within the
        tolerance window, picks the nearest per roof. Returns None if
        either roof is missing.
        """
        vectors = {}
        for roof in ROOFS:
            times = self.vector_times.get(roof, [])
            idx = bisect_left(times, target)
            best_vec, best_d = None, None
            for i in (idx - 1, idx):
                if 0 <= i < len(times):
                    d = abs((times[i] - target).total_seconds())
                    if d > FORECAST_TOLERANCE_MINUTES * 60:
                        continue
                    if best_d is None or d < best_d:
                        best_vec = self.vectors_by_roof[roof][i][1]
                        best_d = d
            if best_vec is None:
                return None
            vectors[roof] = best_vec
        return vectors
    
    def _forecast_for_block(self, vectors, roof, horizon_blocks) -> Optional[float]:
        """Forecast value for the block H blocks after generation time.
        
        Same indexing as the live learner: vector[H-1].
        """
        vector = vectors[roof]
        if horizon_blocks > len(vector):
            return None
        value = vector[horizon_blocks - 1]
        if value is None or value <= 0:
            return None
        return float(value)
    
    # ============================================================
    # EMA STATE UPDATES (identical math to the live learner)
    # ============================================================
    
    def _update_reference(self, tod: int, roof: str, actual_kwh: float):
        state = self.state_reference.get((tod, roof))
        if state is None:
            self.state_reference[(tod, roof)] = {
                'reference_kwh': actual_kwh, 'sample_count': 1
            }
        else:
            # Warm-up: plain average (1/n) until the slow EMA rate is
            # faster than that (same as the live learner).
            alpha = max(REF_EMA_ALPHA, 1.0 / (state['sample_count'] + 1))
            state['reference_kwh'] = (
                (1 - alpha) * state['reference_kwh'] + alpha * actual_kwh
            )
            state['sample_count'] += 1
    
    def _update_accuracy(self, tod: int, horizon: int, roof: str,
                         abs_error: float, norm_error):
        state = self.state_accuracy.get((tod, horizon, roof))
        if state is None:
            self.state_accuracy[(tod, horizon, roof)] = {
                'mean_error': norm_error if norm_error is not None else 0.0,
                'mean_abs_error': abs_error,
                'variance': 0.0,
                'sample_count': 1
            }
        else:
            state['mean_abs_error'] = (
                (1 - EMA_ALPHA) * state['mean_abs_error'] + EMA_ALPHA * abs_error
            )
            state['variance'] = (
                (1 - EMA_ALPHA) * state['variance']
                + EMA_ALPHA * (abs_error - state['mean_abs_error']) ** 2
            )
            if norm_error is not None:
                state['mean_error'] = (
                    (1 - EMA_ALPHA) * state['mean_error'] + EMA_ALPHA * norm_error
                )
            state['sample_count'] += 1
    
    def _update_correlation(self, table_state: Dict, key: Tuple,
                            current_norm: float, window_norms: List[float]):
        if not window_norms:
            return
        avg = sum(window_norms) / len(window_norms)
        state = table_state.get(key)
        if state is None:
            table_state[key] = {
                'correlation': current_norm * avg, 'sample_count': 1
            }
        else:
            state['correlation'] = (
                (1 - EMA_ALPHA) * state['correlation']
                + EMA_ALPHA * (current_norm * avg)
            )
            state['sample_count'] += 1
    
    # ============================================================
    # ERROR COMPUTATION
    # ============================================================
    
    def _error_at(self, block_time, roof: str, horizon: int):
        """(abs_error, norm_error) for the block at block_time, forecast
        made H blocks earlier. None if it cannot be computed.
        
        norm_error is None while the reference magnitude for that block's
        time-of-day is still warming up (fewer than REFERENCE_MIN_SAMPLES
        samples). Normalization is causal: the reference state as of the
        current replay point.
        """
        actuals = self.actuals_by_time.get(block_time)
        if not actuals or roof not in actuals:
            return None
        vectors = self._find_vectors(block_time - timedelta(minutes=15 * horizon))
        if vectors is None:
            return None
        forecast_val = self._forecast_for_block(vectors, roof, horizon)
        if forecast_val is None:
            return None
        abs_error = actuals[roof] - forecast_val
        tod = block_time.hour * 4 + block_time.minute // 15
        ref_state = self.state_reference.get((tod, roof))
        if ref_state and ref_state['sample_count'] >= REFERENCE_MIN_SAMPLES:
            norm_error = abs_error / max(ref_state['reference_kwh'],
                                         REFERENCE_FLOOR_KWH)
        else:
            norm_error = None
        return abs_error, norm_error
    
    def _update_covariance(self, tod: int, horizon: int, roof_errors: Dict):
        """Cross-roof covariance of absolute errors, same math as live.
        
        Centered on the current per-roof mean errors. Correlation is
        recomputed from the final variances at persist time."""
        e = roof_errors['east'][0]
        w = roof_errors['west'][0]
        acc_e = self.state_accuracy.get((tod, horizon, 'east'))
        acc_w = self.state_accuracy.get((tod, horizon, 'west'))
        mu_e = acc_e['mean_abs_error'] if acc_e else 0.0
        mu_w = acc_w['mean_abs_error'] if acc_w else 0.0
        product = (e - mu_e) * (w - mu_w)
        
        state = self.state_covariance.get((tod, horizon))
        if state is None:
            self.state_covariance[(tod, horizon)] = {
                'covariance': product, 'sample_count': 1
            }
        else:
            state['covariance'] = (
                (1 - EMA_ALPHA) * state['covariance'] + EMA_ALPHA * product
            )
            state['sample_count'] += 1
    
    # ============================================================
    # MAIN REPLAY
    # ============================================================
    
    def run(self) -> Dict[str, int]:
        """Replay all history and rebuild the learning tables."""
        with self.db:
            return self._run()
    
    def _run(self) -> Dict[str, int]:
        self.load_history()
        
        self._reset_learning_tables()
        
        times = sorted(t for t, roofs in self.actuals_by_time.items()
                       if all(r in roofs for r in ROOFS))
        logger.info(f"Replaying {len(times)} blocks with both roofs' actuals")
        
        for i, block_time in enumerate(times):
            tod = block_time.hour * 4 + block_time.minute // 15
            actuals = self.actuals_by_time[block_time]
            
            # Reference magnitude first (same order as the live learner)
            for roof in ROOFS:
                self._update_reference(tod, roof, actuals[roof])
            
            for horizon in FIBONACCI_HORIZONS_BLOCKS:
                # Errors for both roofs first (covariance needs both)
                roof_errors = {}
                for roof in ROOFS:
                    error = self._error_at(block_time, roof, horizon)
                    if error is not None:
                        roof_errors[roof] = error
                
                for roof, (abs_error, norm_error) in roof_errors.items():
                    self._update_accuracy(tod, horizon, roof,
                                           abs_error, norm_error)
                    
                    # Local correlation: window around the forecast time.
                    # Correlations need normalized errors; skip while
                    # either side is still warming up.
                    if norm_error is not None:
                        local_norms = []
                        for offset in LOCAL_WINDOW_BLOCKS:
                            lb = block_time - timedelta(
                                minutes=15 * (horizon + offset)
                            )
                            e = self._error_at(lb, roof, horizon)
                            if e is not None and e[1] is not None:
                                local_norms.append(e[1])
                        self._update_correlation(
                            self.state_corr_local, (tod, horizon, roof),
                            norm_error, local_norms
                        )
                        
                        # Daily correlation: window around the same time 24h ago
                        daily_base = block_time - timedelta(
                            minutes=15 * DAILY_OFFSET_BLOCKS
                        )
                        daily_norms = []
                        for offset in LOCAL_WINDOW_BLOCKS:
                            ct = daily_base + timedelta(minutes=15 * offset)
                            e = self._error_at(ct, roof, horizon)
                            if e is not None and e[1] is not None:
                                daily_norms.append(e[1])
                        self._update_correlation(
                            self.state_corr_daily, (tod, horizon, roof),
                            norm_error, daily_norms
                        )
                
                # Cross-roof covariance: needs both roofs this block
                if len(roof_errors) == 2:
                    self._update_covariance(tod, horizon, roof_errors)
            
            if (i + 1) % 100 == 0:
                logger.info(f"Replayed {i + 1}/{len(times)} blocks")
        
        counts = self._persist()
        return counts
    
    # ============================================================
    # PERSISTENCE
    # ============================================================
    
    def _reset_learning_tables(self):
        """Delete all derived model data. Raw history is untouched."""
        logger.info("Resetting learning tables (accuracy, correlations, covariance, reference)")
        for table in ('solar_accuracy', 'solar_correlation_local',
                      'solar_correlation_daily', 'solar_covariance',
                      'solar_reference_magnitude'):
            self.db.query(f'DELETE FROM {table}', fetch=False)
    
    def _persist(self) -> Dict[str, int]:
        counts = {}
        for (tod, horizon, roof), state in self.state_accuracy.items():
            self.db.update_accuracy(
                time_of_day_block=tod,
                forecast_horizon_block=horizon,
                roof=roof,
                mean_error=state['mean_error'],
                mean_abs_error=state['mean_abs_error'],
                variance=state['variance'],
                sample_count=state['sample_count']
            )
        counts['accuracy_cells'] = len(self.state_accuracy)
        
        for (tod, horizon, roof), state in self.state_corr_local.items():
            self.db.update_correlation_local(
                time_of_day_block=tod,
                forecast_horizon_block=horizon,
                roof=roof,
                correlation=state['correlation'],
                sample_count=state['sample_count']
            )
        counts['correlation_local_cells'] = len(self.state_corr_local)
        
        for (tod, horizon, roof), state in self.state_corr_daily.items():
            self.db.update_correlation_daily(
                time_of_day_block=tod,
                forecast_horizon_block=horizon,
                roof=roof,
                correlation=state['correlation'],
                sample_count=state['sample_count']
            )
        counts['correlation_daily_cells'] = len(self.state_corr_daily)
        
        for (tod, horizon), state in self.state_covariance.items():
            acc_e = self.state_accuracy.get((tod, horizon, 'east'))
            acc_w = self.state_accuracy.get((tod, horizon, 'west'))
            sigma_e = math.sqrt(acc_e['variance']) if acc_e else 0.0
            sigma_w = math.sqrt(acc_w['variance']) if acc_w else 0.0
            denom = sigma_e * sigma_w
            correlation = (
                state['covariance'] / denom if denom > 1e-12 else 0.0
            )
            self.db.update_covariance(
                time_of_day_block=tod,
                forecast_horizon_block=horizon,
                covariance=state['covariance'],
                correlation=correlation,
                sample_count=state['sample_count']
            )
        counts['covariance_cells'] = len(self.state_covariance)
        
        for (tod, roof), state in self.state_reference.items():
            self.db.update_reference_magnitude(
                time_of_day_block=tod,
                roof=roof,
                reference_kwh=state['reference_kwh'],
                sample_count=state['sample_count']
            )
        counts['reference_cells'] = len(self.state_reference)
        
        logger.info(f"Persisted: {counts}")
        return counts


def main():
    import argparse
    import sys
    import os
    
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(project_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    parser = argparse.ArgumentParser(
        description='Rebuild learning tables from stored forecast/actual history'
    )
    parser.parse_args()
    
    backfiller = Backfiller()
    counts = backfiller.run()
    print(f"\nBackfill complete: {counts}")


if __name__ == '__main__':
    main()
