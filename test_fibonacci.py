#!/usr/bin/env python3
"""
Test Fibonacci Horizon Learning
===============================

Test that accuracy learning works with Fibonacci horizons.
"""

import sys
import os
sys.path.insert(0, '/home/gijs')

from datetime import datetime, timedelta, timezone
from solar_forecast.db.client import DatabaseClient
import unittest.mock as mock
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_fibonacci_learning():
    """Test learning with actual Fibonacci horizons."""
    logger.info("Cleaning database...")
    with DatabaseClient() as db:
        db.query('DELETE FROM solar_forecast_vectors', fetch=False)
        db.query('DELETE FROM solar_actual_production', fetch=False)
        db.query('DELETE FROM solar_accuracy', fetch=False)
    
    # Test with horizon = 4 blocks (1 hour)
    # At time T=10:00, forecast was made at T-1h = 09:00
    # The forecast vector from 09:00 has index 4-1=3 for the 10:00 block
    
    forecast_time = datetime(2026, 10, 5, 9, 0, 0, tzinfo=timezone.utc)
    actual_time = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
    
    # Create forecast vector with value at index 3 (4th block)
    forecast_vector_east = [0.0] * 672
    forecast_vector_east[3] = 0.5  # Forecast for 10:00
    
    forecast_vector_west = [0.0] * 672
    forecast_vector_west[3] = 0.4  # Forecast for 10:00
    
    with DatabaseClient() as db:
        db.save_forecast_vector(
            timestamp=forecast_time.isoformat(),
            roof='east',
            vector=forecast_vector_east
        )
        db.save_forecast_vector(
            timestamp=forecast_time.isoformat(),
            roof='west',
            vector=forecast_vector_west
        )
        
        # Store actual production at 10:00
        db.save_actual_production(
            timestamp=actual_time.isoformat(),
            roof='east',
            energy_kwh=0.6  # Actual was 0.6, forecast was 0.5
        )
        db.save_actual_production(
            timestamp=actual_time.isoformat(),
            roof='west',
            energy_kwh=0.45  # Actual was 0.45, forecast was 0.4
        )
    
    logger.info(f"Stored forecast at {forecast_time.isoformat()}")
    logger.info(f"Stored actual at {actual_time.isoformat()}")
    
    # Now test with the accuracy learner
    from solar_forecast.processing.accuracy_learner import AccuracyLearner
    
    learner = AccuracyLearner()
    test_time = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
    
    with mock.patch('solar_forecast.processing.accuracy_learner.datetime') as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        
        result = learner.update_from_latest_actuals()
        
        logger.info(f"\nAccuracy learner result: {result.get('updates_performed', 0)} updates")
        
        if result.get('updates'):
            for update in result['updates']:
                logger.info(f"  Horizon {update['forecast_horizon_blocks']}: "
                          f"forecast={update['forecast']}, actual={update['actual']}, "
                          f"error={update['error']:.4f}")
    
    # Check database
    with DatabaseClient() as db:
        accuracy = db.query("SELECT COUNT(*) FROM solar_accuracy")
        logger.info(f"Accuracy cells in DB: {accuracy[0][0]}")
        
        if accuracy[0][0] > 0:
            acc_data = db.query("SELECT time_of_day_block, forecast_horizon_block, roof, mean_error, variance, sample_count FROM solar_accuracy")
            logger.info("Accuracy data:")
            for row in acc_data:
                logger.info(f"  TOD={row[0]}, Hor={row[1]}, {row[2]}: mean={row[3]:.4f}, var={row[4]:.6f}, count={row[5]}")
            return True
        else:
            logger.warning("NO ACCURACY DATA CREATED!")
            # Debug: check what's in the database
            vectors = db.query("SELECT timestamp, roof FROM solar_forecast_vectors ORDER BY timestamp")
            logger.info(f"Vectors in DB: {len(vectors)}")
            for v in vectors:
                logger.info(f"  {v[0]} {v[1]}")
            
            actuals = db.query("SELECT timestamp, roof, energy_kwh FROM solar_actual_production ORDER BY timestamp")
            logger.info(f"Actuals in DB: {len(actuals)}")
            for a in actuals:
                logger.info(f"  {a[0]} {a[1]}: {a[2]}")
            return False


if __name__ == '__main__':
    success = test_fibonacci_learning()
    sys.exit(0 if success else 1)
