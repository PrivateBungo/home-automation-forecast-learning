#!/usr/bin/env python3
"""
Direct Test - Bypasses sensor fetching to test learning logic
"""

import sys
import os
sys.path.insert(0, '/home/gijs')

from datetime import datetime, timedelta, timezone
from solar_forecast.db.client import DatabaseClient
from solar_forecast.processing.accuracy_learner import AccuracyLearner
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_test_forecast_vector(peak_value=1.0, noise=0.05):
    """Create a deterministic test forecast vector."""
    import random
    random.seed(42)
    vector = [0.0] * 672
    for i in range(24, 72):
        hours_from_peak = abs(i - 48)
        sigma = 12
        value = peak_value * (0.5 + 0.5 * (1 - hours_from_peak / sigma)) ** 2
        value = max(0, value + random.uniform(-noise, noise))
        vector[i] = round(value, 4)
    return vector


def test_direct():
    """Test the learning logic directly."""
    logger.info("Cleaning database...")
    with DatabaseClient() as db:
        db.query('DELETE FROM solar_forecast_vectors', fetch=False)
        db.query('DELETE FROM solar_actual_production', fetch=False)
        db.query('DELETE FROM solar_accuracy', fetch=False)
        db.query('DELETE FROM solar_correlation_local', fetch=False)
        db.query('DELETE FROM solar_correlation_daily', fetch=False)
    
    # Create a sequence of times every 15 minutes for 4 hours
    base_time = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)
    
    # Store forecast vectors and actuals for multiple time points
    for i in range(17):  # 17 * 15 min = 4 hours 15 min
        run_time = base_time + timedelta(minutes=15 * i)
        
        # Store forecast vector
        vector = create_test_forecast_vector()
        with DatabaseClient() as db:
            db.save_forecast_vector(
                timestamp=run_time.isoformat(),
                roof='east',
                vector=vector
            )
            db.save_forecast_vector(
                timestamp=run_time.isoformat(),
                roof='west',
                vector=vector
            )
        
        # Store actual production for the previous block
        if i > 0:  # Can't store actual for first block (no previous)
            prev_block_time = run_time - timedelta(minutes=15)
            block_start = prev_block_time.replace(second=0, microsecond=0)
            actual_value = 0.5 * (1 + 0.1 * i)  # Varying actual
            
            with DatabaseClient() as db:
                db.save_actual_production(
                    timestamp=block_start.isoformat(),
                    roof='east',
                    energy_kwh=round(actual_value, 4)
                )
                db.save_actual_production(
                    timestamp=block_start.isoformat(),
                    roof='west',
                    energy_kwh=round(actual_value * 0.9, 4)
                )
    
    logger.info(f"Stored {17} forecast vector pairs and {16} actual production pairs")
    
    # Now check what we have
    with DatabaseClient() as db:
        vectors = db.query("SELECT COUNT(*) FROM solar_forecast_vectors")
        actuals = db.query("SELECT COUNT(*) FROM solar_actual_production")
        logger.info(f"Forecast vectors: {vectors[0][0]}")
        logger.info(f"Actual production: {actuals[0][0]}")
    
    # Now manually trigger learning for the last few time points
    learner = AccuracyLearner()
    total_updates = 0
    
    # Try to learn from each of the last few runs
    for i in range(15, 17):
        run_time = base_time + timedelta(minutes=15 * i)
        # Manually set the learner's internal time
        result = learner.update_from_latest_actuals()
        updates = result.get('updates_performed', 0)
        total_updates += updates
        logger.info(f"Run {i}: {updates} updates")
    
    # Check results
    with DatabaseClient() as db:
        accuracy = db.query("SELECT COUNT(*) FROM solar_accuracy")
        local_corr = db.query("SELECT COUNT(*) FROM solar_correlation_local")
        daily_corr = db.query("SELECT COUNT(*) FROM solar_correlation_daily")
        
        logger.info(f"\nFinal counts:")
        logger.info(f"Accuracy cells: {accuracy[0][0]}")
        logger.info(f"Local correlation cells: {local_corr[0][0]}")
        logger.info(f"Daily correlation cells: {daily_corr[0][0]}")
        
        if accuracy[0][0] > 0:
            acc_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, mean_error, variance, sample_count FROM solar_accuracy ORDER BY sample_count DESC LIMIT 10")
            logger.info("\nTop accuracy data:")
            for row in acc_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: mean={row[3]:.4f}, var={row[4]:.6f}, count={row[5]}")
        
        if local_corr[0][0] > 0:
            corr_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, correlation, sample_count FROM solar_correlation_local ORDER BY sample_count DESC LIMIT 5")
            logger.info("\nLocal correlation data:")
            for row in corr_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: corr={row[3]:.4f}, count={row[4]}")
        
        if daily_corr[0][0] > 0:
            corr_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, correlation, sample_count FROM solar_correlation_daily ORDER BY sample_count DESC LIMIT 5")
            logger.info("\nDaily correlation data:")
            for row in corr_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: corr={row[3]:.4f}, count={row[4]}")
    
    return total_updates > 0 or accuracy[0][0] > 0


if __name__ == '__main__':
    success = test_direct()
    sys.exit(0 if success else 1)
