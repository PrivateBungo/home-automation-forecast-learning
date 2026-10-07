"""
Accuracy Learner
================

Learns accuracy model (mean error, variance, covariance, correlations) from forecast vs actual data.

Key Concept: We learn the ERROR between forecast and actual, NOT the forecast itself.
The forecast comes from an external API that doesn't account for local factors 
(trees, shadows, orientation, etc.). We learn how wrong that forecast typically is.

All calculations in kWh, 15-minute resolution.
Uses Fibonacci horizons for computational efficiency.
Implements temporal correlation learning (local ±2h, daily 24h offset).
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
import math

from solar_forecast.config import (
    TOTAL_FORECAST_BLOCKS,
    BLOCKS_PER_HOUR,
    BLOCKS_PER_DAY,
    FIBONACCI_HORIZONS_BLOCKS,
    EMA_ALPHA,
    SENSOR_QUARTERLY_ENERGY,
    LOCAL_WINDOW_BLOCKS,
    DAILY_OFFSET_BLOCKS
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)


class AccuracyLearner:
    """
    Learns accuracy model from forecast errors.
    
    Updates:
    - Mean error and variance for specific Fibonacci horizons
    - Local temporal correlation (current error vs avg error in past 2h)
    - Daily temporal correlation (current error vs avg error at same time 24h ago)
    - Stores actual production data for reference
    """
    
    def __init__(self):
        self.db = DatabaseClient()
    
    def update_from_latest_actuals(self) -> Dict[str, Any]:
        """
        Main entry point: fetch latest actuals and update all models.
        
        Called every 15 minutes.
        For each Fibonacci horizon, finds the forecast made that many blocks ago
        and computes the error for the current 15-min block.
        
        Returns:
            Summary of all updates performed
        """
        # Get current time in UTC
        now_utc = datetime.now(timezone.utc)
        
        # Determine the current 15-min block timestamp
        # The actuals are for the block ending at now_utc
        current_block_start = self._get_block_start_time(now_utc)
        
        # Get time-of-day block (0-95) for the current time
        time_of_day_block = self._get_block_in_day(now_utc)
        
        # Use a single database connection for the entire operation
        with self.db as db:
            # Step 1: Fetch and store latest actual production
            actuals = self._fetch_and_store_latest_actuals(db, current_block_start)
            if not actuals:
                return {'status': 'skipped', 'reason': 'no_actuals'}
            
            updates = []
            
            # Step 2: For each Fibonacci horizon, compute error and update models
            for horizon_blocks in FIBONACCI_HORIZONS_BLOCKS:
                # Skip if horizon is beyond our data
                if horizon_blocks > TOTAL_FORECAST_BLOCKS:
                    logger.debug(f"Skipping horizon {horizon_blocks}, beyond max blocks")
                    continue
                
                # Calculate when the forecast was generated
                # The forecast was made H blocks before the current block started
                forecast_time_utc = current_block_start - timedelta(minutes=15 * horizon_blocks)
                
                # Get forecast vectors from that time
                vectors = self._get_forecast_vectors_at_time(db, forecast_time_utc)
                if not vectors:
                    logger.debug(f"No forecast vectors for horizon {horizon_blocks} blocks ago")
                    continue
                
                # For each roof
                for roof in ['east', 'west']:
                    actual_value = actuals[roof]
                    
                    # Get the specific forecast value for this block
                    forecast_value = self._get_forecast_for_block(
                        vectors, roof, horizon_blocks
                    )
                    
                    if forecast_value is None or forecast_value == 0:
                        logger.debug(f"No valid forecast for {roof} at horizon {horizon_blocks}")
                        continue
                    
                    # Compute relative error
                    error = self._relative_error(forecast_value, actual_value)
                    
                    # Update EMA accuracy
                    ema_result = self._update_accuracy(
                        db, time_of_day_block, horizon_blocks, roof, error
                    )
                    
                    # Compute and update local correlation
                    local_corr_result = self._update_local_correlation(
                        db, now_utc, time_of_day_block, horizon_blocks, roof, error, horizon_blocks
                    )
                    
                    # Compute and update daily correlation
                    daily_corr_result = self._update_daily_correlation(
                        db, now_utc, time_of_day_block, horizon_blocks, roof, error, horizon_blocks
                    )
                    
                    updates.append({
                        'roof': roof,
                        'time_of_day_block': time_of_day_block,
                        'forecast_horizon_blocks': horizon_blocks,
                        'forecast': forecast_value,
                        'actual': actual_value,
                        'error': error,
                        'ema': ema_result,
                        'local_corr': local_corr_result,
                        'daily_corr': daily_corr_result
                    })
            
            return {
                'status': 'completed',
                'timestamp': now_utc.isoformat(),
                'time_of_day_block': time_of_day_block,
                'updates_performed': len(updates),
                'updates': updates
            }
    
    def _fetch_and_store_latest_actuals(self, db, timestamp: datetime) -> Optional[Dict[str, float]]:
        """
        Fetch latest actual energy for both roofs and store in database.
        
        Note: Checks if actual production already exists for this timestamp before storing.
        
        Returns:
            dict with 'east' and 'west' keys (in kWh), or None if no data.
            Always returns values FROM DATABASE to ensure consistency.
        """
        # First, check if actuals already exist in database for this timestamp
        actuals = {}
        for roof in ['east', 'west']:
            value = db.get_actual_production(timestamp.isoformat(), roof)
            if value is not None:
                actuals[roof] = value
        
        if len(actuals) == 2:
            # Both roofs have existing data, return it
            return actuals
        
        # If not all roofs have data, try to fetch from sensors
        sensor_actuals = {}
        
        # Try quarterly sensors first, then hourly as fallback
        # We always try both to get the best available data
        for roof in ['east', 'west']:
            if roof in actuals:
                continue
            
            # Try quarterly sensor
            quarterly_sensor = SENSOR_QUARTERLY_ENERGY.get(roof)
            if quarterly_sensor:
                data = db.get_latest_sensor_data(quarterly_sensor)
                if data and data.get('state') is not None:
                    try:
                        value = float(data['state'])
                        if value > 10:
                            value = value * 0.001
                        # Only accept non-zero or reasonable values
                        if value > 0.001:  # More than 1 Wh is reasonable
                            sensor_actuals[roof] = value
                            continue
                    except (ValueError, TypeError):
                        pass
            
            # Fall back to hourly sensor
            hourly_sensor = {
                'east': 'sensor.solar_hourly_energy_east_roof',
                'west': 'sensor.solar_hourly_energy_west_roof'
            }.get(roof)
            
            if hourly_sensor:
                data = db.get_latest_sensor_data(hourly_sensor)
                if data and data.get('state') is not None:
                    try:
                        hourly_value = float(data['state'])
                        if hourly_value > 10:
                            hourly_value = hourly_value * 0.001
                        quarter_hour_value = hourly_value / BLOCKS_PER_HOUR
                        if quarter_hour_value > 0.001:
                            sensor_actuals[roof] = quarter_hour_value
                            continue
                    except (ValueError, TypeError):
                        pass
            
            # If we still don't have data for this roof, fail
            if roof not in sensor_actuals and roof not in actuals:
                return None
        
        # Merge existing and sensor data
        all_actuals = {**actuals, **sensor_actuals}
        
        # Store sensor-derived values if we don't have existing data
        for roof, value in sensor_actuals.items():
            existing = db.get_actual_production(timestamp.isoformat(), roof)
            if existing is None:
                db.save_actual_production(
                    timestamp=timestamp.isoformat(),
                    roof=roof,
                    energy_kwh=value
                )
        
        if len(all_actuals) == 2:
            return all_actuals
        else:
            return None
    
    def _get_forecast_vectors_at_time(self, db, timestamp: datetime) -> Optional[Dict[str, Any]]:
        """
        Get forecast vectors from a specific timestamp.
        
        Args:
            db: Database connection
            timestamp: UTC datetime when forecasts were generated
            
        Returns:
            dict with 'east' and 'west' vectors, or None
        """
        # Find vectors closest to this timestamp
        # Allow up to 15 minute tolerance for timing differences
        # (since forecast vectors might be stored with seconds, but we're looking at rounded times)
        results = db.query(
            """
            SELECT timestamp, roof, forecast_vector
            FROM solar_forecast_vectors
            WHERE timestamp BETWEEN %s AND %s
            ORDER BY ABS(EXTRACT(EPOCH FROM timestamp - %s::timestamp))
            LIMIT 4
            """,
            (timestamp - timedelta(minutes=15), 
             timestamp + timedelta(minutes=15),
             timestamp)
        )
        
        if not results:
            return None
        
        vectors = {}
        for row in results:
            vectors[row[1]] = {
                'timestamp': row[0],
                'vector': list(row[2])
            }
        
        # Need both roofs
        return vectors if len(vectors) >= 2 else None
    
    def _get_forecast_for_block(
        self,
        vectors: Dict[str, Dict[str, Any]],
        roof: str,
        horizon_blocks: int
    ) -> Optional[float]:
        """
        Get the forecast value for a specific horizon block.
        
        Args:
            vectors: dict with east/west vectors
            roof: 'east' or 'west'
            horizon_blocks: How many blocks ahead this forecast was for
            
        Returns:
            Forecast kWh value, or None
        """
        if roof not in vectors:
            return None
        
        vector = vectors[roof]['vector']
        
        # The forecast vector has blocks for the next 672 blocks (7 days)
        # For a forecast made N blocks ago, the current block is at index (N-1)
        # because the vector is [block_0, block_1, ..., block_671]
        # where block_0 is the next 15-min block after the forecast time.
        
        if horizon_blocks > len(vector):
            return None
        
        forecast_value = vector[horizon_blocks - 1]
        return forecast_value if forecast_value is not None and forecast_value >= 0 else None
    
    def _get_block_start_time(self, ts: datetime) -> datetime:
        """Get the start time of the current 15-min block."""
        minute = ts.minute
        block_start_minute = (minute // 15) * 15
        return ts.replace(minute=block_start_minute, second=0, microsecond=0)
    
    def _get_block_in_day(self, ts: datetime) -> int:
        """Get 15-min block within the day (0-95)."""
        hour = ts.hour
        minute = ts.minute
        block_in_hour = minute // 15
        return hour * BLOCKS_PER_HOUR + block_in_hour
    
    def _relative_error(self, forecast: float, actual: float) -> float:
        """
        Calculate relative error: (actual - forecast) / forecast.
        
        This is what we learn: how wrong the forecast is, relative to its magnitude.
        Handles edge cases (forecast=0) gracefully.
        """
        epsilon = 0.0001  # Small value to avoid division by zero
        safe_forecast = max(abs(forecast), epsilon)
        return (actual - forecast) / safe_forecast
    
    def _update_accuracy(
        self,
        db,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str,
        error: float
    ) -> Dict[str, Any]:
        """Update mean and variance for a single cell using EMA."""
        current = db.get_accuracy(time_of_day_block, forecast_horizon_block, roof)
        
        if current is None:
            # First observation
            new_mean = error
            new_var = 0.0
            count = 1
        else:
            # EMA update
            old_mean = current['mean_error']
            old_var = current['variance']
            old_count = current['sample_count']
            
            new_mean = (1 - EMA_ALPHA) * old_mean + EMA_ALPHA * error
            new_var = (1 - EMA_ALPHA) * old_var + EMA_ALPHA * (error - new_mean) ** 2
            count = old_count + 1
        
        db.update_accuracy(
            time_of_day_block=time_of_day_block,
            forecast_horizon_block=forecast_horizon_block,
            roof=roof,
            mean_error=new_mean,
            variance=new_var,
            sample_count=count
        )
        
        logger.debug(f"EMA Updated: tod={time_of_day_block}, "
                    f"hor={forecast_horizon_block}, {roof}, "
                    f"error={error:.4f}, mean={new_mean:.4f}, var={new_var:.6f}")
        
        return {
            'mean_error': new_mean,
            'variance': new_var,
            'sample_count': count
        }
    
    def _update_local_correlation(
        self,
        db,
        now_utc: datetime,
        current_tod: int,
        horizon_blocks: int,
        roof: str,
        current_error: float,
        max_lookback_blocks: int
    ) -> Optional[Dict[str, Any]]:
        """
        Update local temporal correlation.
        
        Correlates current error with average error in local window (past 2h).
        For horizon H, we look at forecasts made H blocks ago and compute error
        for the local window around that forecast time.
        """
        current = db.get_correlation_local(current_tod, horizon_blocks, roof)
        
        # Collect errors from local window
        local_errors = []
        for offset_blocks in LOCAL_WINDOW_BLOCKS:
            # Calculate the time we need to look at
            lookback_time = now_utc - timedelta(minutes=15 * (horizon_blocks + offset_blocks))
            
            # Get actual at that time
            actual = db.get_actual_production(
                lookback_time.isoformat(),
                roof
            )
            
            # Get forecast from horizon_blocks ago from that lookback_time
            forecast_time = lookback_time - timedelta(minutes=15 * horizon_blocks)
            forecast_vectors = self._get_forecast_vectors_at_time(db, forecast_time)
            
            if forecast_vectors and actual is not None:
                forecast_val = self._get_forecast_for_block(
                    forecast_vectors, roof, horizon_blocks
                )
                if forecast_val and forecast_val > 0:
                    error = self._relative_error(forecast_val, actual)
                    local_errors.append(error)
        
        if not local_errors:
            logger.debug(f"No local errors for {roof} hor={horizon_blocks}")
            return None
        
        avg_local_error = sum(local_errors) / len(local_errors)
        current_error_normalized = current_error
        
        # Compute correlation
        if current is None:
            correlation = current_error_normalized * avg_local_error
            count = 1
        else:
            old_corr = current['correlation']
            old_count = current['sample_count']
            correlation = (1 - EMA_ALPHA) * old_corr + EMA_ALPHA * (current_error_normalized * avg_local_error)
            count = old_count + 1
        
        db.update_correlation_local(
            time_of_day_block=current_tod,
            forecast_horizon_block=horizon_blocks,
            roof=roof,
            correlation=correlation,
            sample_count=count
        )
        
        logger.debug(f"Local Corr Updated: tod={current_tod}, hor={horizon_blocks}, "
                    f"{roof}, corr={correlation:.4f}, count={count}")
        
        return {
            'correlation': correlation,
            'sample_count': count,
            'local_errors': len(local_errors)
        }
    
    def _update_daily_correlation(
        self,
        db,
        now_utc: datetime,
        current_tod: int,
        horizon_blocks: int,
        roof: str,
        current_error: float,
        max_lookback_blocks: int
    ) -> Optional[Dict[str, Any]]:
        """
        Update daily temporal correlation.
        
        Correlates current error with average error at the same time 24h ago.
        For horizon H, we look at forecasts made H blocks ago.
        """
        current = db.get_correlation_daily(current_tod, horizon_blocks, roof)
        
        # Calculate when we need to look (24h ago from now, then H blocks back)
        daily_lookback_time = now_utc - timedelta(hours=24)
        
        # Collect errors from daily window
        daily_errors = []
        for offset_blocks in LOCAL_WINDOW_BLOCKS:
            # Time to check: 24h ago + offset
            check_time = daily_lookback_time + timedelta(minutes=15 * offset_blocks)
            
            # Get actual at that time
            actual = db.get_actual_production(
                check_time.isoformat(),
                roof
            )
            
            # Get forecast from horizon_blocks ago from that check_time
            forecast_time = check_time - timedelta(minutes=15 * horizon_blocks)
            forecast_vectors = self._get_forecast_vectors_at_time(db, forecast_time)
            
            if forecast_vectors and actual is not None:
                forecast_val = self._get_forecast_for_block(
                    forecast_vectors, roof, horizon_blocks
                )
                if forecast_val and forecast_val > 0:
                    error = self._relative_error(forecast_val, actual)
                    daily_errors.append(error)
        
        if not daily_errors:
            logger.debug(f"No daily errors for {roof} hor={horizon_blocks}")
            return None
        
        avg_daily_error = sum(daily_errors) / len(daily_errors)
        current_error_normalized = current_error
        
        # Compute correlation
        if current is None:
            correlation = current_error_normalized * avg_daily_error
            count = 1
        else:
            old_corr = current['correlation']
            old_count = current['sample_count']
            correlation = (1 - EMA_ALPHA) * old_corr + EMA_ALPHA * (current_error_normalized * avg_daily_error)
            count = old_count + 1
        
        db.update_correlation_daily(
            time_of_day_block=current_tod,
            forecast_horizon_block=horizon_blocks,
            roof=roof,
            correlation=correlation,
            sample_count=count
        )
        
        logger.debug(f"Daily Corr Updated: tod={current_tod}, hor={horizon_blocks}, "
                    f"{roof}, corr={correlation:.4f}, count={count}")
        
        return {
            'correlation': correlation,
            'sample_count': count,
            'daily_errors': len(daily_errors)
        }


# ============================================================
# STANDALONE SCRIPT
# ============================================================
if __name__ == '__main__':
    import argparse
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    parser = argparse.ArgumentParser(description='Update accuracy model with correlations')
    parser.add_argument('--test', action='store_true', help='Run in test mode')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        learner = AccuracyLearner()
        
        result = learner.update_from_latest_actuals()
        
        if args.test:
            print(f"\nTest mode result:")
            print(f"  Status: {result.get('status')}")
            print(f"  Timestamp: {result.get('timestamp')}")
            print(f"  Time of day block: {result.get('time_of_day_block')}")
            print(f"  Updates performed: {result.get('updates_performed', 0)}")
            
            if result.get('updates'):
                print(f"\n  First update details:")
                first_update = result['updates'][0]
                for key, value in first_update.items():
                    if isinstance(value, dict):
                        print(f"    {key}: {value}")
                    else:
                        print(f"    {key}: {value}")
        else:
            logger.info(f"Updated {result.get('updates_performed', 0)} cells")
    
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
