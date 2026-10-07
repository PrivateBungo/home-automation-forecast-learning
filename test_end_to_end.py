#!/usr/bin/env python3
"""
End-to-End Test Script
=====================

Simulates running the pipeline over multiple time periods to test
accuracy learning with realistic data.
"""

import sys
import os
sys.path.insert(0, '/home/gijs')

# SAFETY: run against the isolated test database, never production.
# Must be set BEFORE importing any solar_forecast module (config reads
# it at import time).
os.environ['SOLAR_FORECAST_DB_DATABASE'] = 'solar_forecast_test'

from datetime import datetime, timedelta, timezone
from solar_forecast.db.client import DatabaseClient
from solar_forecast.config import DB_DATABASE

assert DB_DATABASE == 'solar_forecast_test', \
    f'Refusing to run tests against database {DB_DATABASE!r} - ' \
    'tests must only run against solar_forecast_test'
from solar_forecast.processing.accuracy_learner import AccuracyLearner
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_test_forecast_vector(peak_value=1.0, noise=0.1):
    """Create a test forecast vector with some realistic values.

    Rolling-vector convention: index k is the (k+1)-th 15-min block
    after generation time. Shape the first ~5 hours so the short
    Fibonacci horizons (4, 8, 12, 20 blocks -> indexes 3, 7, 11, 19)
    have non-zero forecast values for the learner to work with.
    """
    vector = [0.0] * 672
    # Peak ~2h after generation, spread of 3 hours
    for i in range(1, 20):
        hours_from_peak = abs(i - 8)
        sigma = 12
        value = peak_value * (0.5 + 0.5 * (1 - hours_from_peak / sigma)) ** 2
        # Add some noise
        import random
        value = max(0, value + random.uniform(-noise, noise))
        vector[i] = round(value, 4)
    return vector


def simulate_pipeline_run(run_time_utc, east_production_kwh, west_production_kwh):
    """
    Simulate a pipeline run at a specific time with specific actual production.
    This bypasses the sensor fetching and directly stores data.
    """
    logger.info(f"\n=== Simulating run at {run_time_utc.isoformat()} ===")
    
    # Create test forecast vectors
    east_vector = create_test_forecast_vector()
    west_vector = create_test_forecast_vector()
    
    with DatabaseClient() as db:
        # Store forecast vectors
        for roof, vector in [('east', east_vector), ('west', west_vector)]:
            db.save_forecast_vector(
                timestamp=run_time_utc.isoformat(),
                roof=roof,
                vector=vector
            )
        logger.info(f"Stored forecast vectors")
        
        # Store actual production for the block that just completed
        minute = run_time_utc.minute
        block_start_minute = (minute // 15) * 15
        block_start = run_time_utc.replace(minute=block_start_minute, second=0, microsecond=0)
        
        # Store the SIMULATED actual production, not from sensors
        db.save_actual_production(
            timestamp=block_start.isoformat(),
            roof='east',
            energy_kwh=east_production_kwh
        )
        db.save_actual_production(
            timestamp=block_start.isoformat(),
            roof='west',
            energy_kwh=west_production_kwh
        )
        logger.info(f"Stored actual: east={east_production_kwh:.3f}, west={west_production_kwh:.3f}")
    
    # Now run the accuracy learner with mocked datetime
    learner = AccuracyLearner()
    
    # Mock datetime.now in the accuracy_learner module
    import unittest.mock as mock
    with mock.patch('solar_forecast.processing.accuracy_learner.datetime') as mock_datetime:
        mock_datetime.now.return_value = run_time_utc
        # Allow datetime constructor to work normally
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        
        result = learner.update_from_latest_actuals()
    
    logger.info(f"Accuracy update: {result.get('updates_performed', 0)} cells")
    if result.get('updates'):
        for update in result['updates'][:3]:
            logger.debug(f"  {update}")
    return result.get('updates_performed', 0)


def test_end_to_end():
    """Run comprehensive end-to-end test."""
    logger.info("Starting end-to-end test...")
    
    # Clean database
    with DatabaseClient() as db:
        db.query('DELETE FROM solar_forecast_vectors', fetch=False)
        db.query('DELETE FROM solar_actual_production', fetch=False)
        db.query('DELETE FROM solar_accuracy', fetch=False)
        db.query('DELETE FROM solar_correlation_local', fetch=False)
        db.query('DELETE FROM solar_correlation_daily', fetch=False)
    logger.info("Database cleaned")
    
    # Simulate runs every 15 minutes for 6 hours
    start_time = datetime(2026, 10, 5, 6, 0, 0, tzinfo=timezone.utc)
    total_updates = 0
    
    for i in range(25):  # 25 * 15 min = 6 hours 15 min
        run_time = start_time + timedelta(minutes=15 * i)
        
        # Simulate realistic solar production
        hours = run_time.hour + run_time.minute / 60
        if hours < 6:
            east_prod = 0.05
            west_prod = 0.04
        elif hours < 9:
            east_prod = 0.15
            west_prod = 0.12
        elif hours < 12:
            east_prod = 0.4
            west_prod = 0.35
        elif hours < 15:
            east_prod = 0.8
            west_prod = 0.7
        elif hours < 18:
            east_prod = 0.4
            west_prod = 0.35
        else:
            east_prod = 0.1
            west_prod = 0.08
        
        # Add some variation
        import random
        random.seed(42 + i)  # Reproducible
        east_prod = round(east_prod * random.uniform(0.8, 1.2), 4)
        west_prod = round(west_prod * random.uniform(0.8, 1.2), 4)
        
        updates = simulate_pipeline_run(run_time, east_prod, west_prod)
        total_updates += updates
    
    logger.info(f"\n=== Test Results ===")
    logger.info(f"Total accuracy updates: {total_updates}")
    
    # Verify data in database
    with DatabaseClient() as db:
        vectors = db.query("SELECT COUNT(*) FROM solar_forecast_vectors")
        actuals = db.query("SELECT COUNT(*) FROM solar_actual_production")
        accuracy = db.query("SELECT COUNT(*) FROM solar_accuracy")
        local_corr = db.query("SELECT COUNT(*) FROM solar_correlation_local")
        daily_corr = db.query("SELECT COUNT(*) FROM solar_correlation_daily")
        
        logger.info(f"Forecast vectors: {vectors[0][0]}")
        logger.info(f"Actual production: {actuals[0][0]}")
        logger.info(f"Accuracy cells: {accuracy[0][0]}")
        logger.info(f"Local correlation cells: {local_corr[0][0]}")
        logger.info(f"Daily correlation cells: {daily_corr[0][0]}")
        
        # Check some accuracy data
        acc_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, mean_error, variance FROM solar_accuracy LIMIT 5")
        if acc_data:
            logger.info("\nSample accuracy data:")
            for row in acc_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: mean={row[3]:.4f}, var={row[4]:.6f}")
        
        # Check correlation data
        corr_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, correlation FROM solar_correlation_local LIMIT 5")
        if corr_data:
            logger.info("\nSample local correlation data:")
            for row in corr_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: corr={row[3]:.4f}")
    
    return total_updates > 0


if __name__ == '__main__':
    success = test_end_to_end()
    sys.exit(0 if success else 1)
