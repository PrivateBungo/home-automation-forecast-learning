#!/usr/bin/env python3
"""
Minimal Test - Directly test the learning algorithm
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


def test_learning_directly():
    """Test the learning algorithm directly by manually computing errors."""
    logger.info("Cleaning database...")
    with DatabaseClient() as db:
        db.query('DELETE FROM solar_forecast_vectors', fetch=False)
        db.query('DELETE FROM solar_actual_production', fetch=False)
        db.query('DELETE FROM solar_accuracy', fetch=False)
    
    # Create test data
    # Use horizon=12 blocks (3 hours) which IS in the Fibonacci list
    # At time T=10:00, we have:
    # - Forecast vector from T-3h = 07:00
    # - Forecast in vector at index 12-1=11 is for T=07:00+3h=10:00
    
    # Store forecast vector at 07:00
    forecast_vector_east = [0.0] * 672
    # Set block 12 to 0.5 kWh (this will be the forecast for 10:00)
    forecast_vector_east[12-1] = 0.5  # index 11 is the 12th block
    
    forecast_vector_west = [0.0] * 672
    forecast_vector_west[12-1] = 0.4
    
    forecast_time = datetime(2026, 10, 5, 7, 0, 0, tzinfo=timezone.utc)
    
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
        
        # Store actual production at 10:00 (3 hours later)
        actual_time = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
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
    
    # Verify data
    with DatabaseClient() as db:
        vectors = db.get_forecast_vectors_near_time(forecast_time.isoformat())
        actual_east = db.get_actual_production(actual_time.isoformat(), 'east')
        actual_west = db.get_actual_production(actual_time.isoformat(), 'west')
        
        logger.info(f"Retrieved vectors: {vectors is not None}")
        logger.info(f"Retrieved actual east: {actual_east}")
        logger.info(f"Retrieved actual west: {actual_west}")
        
        if vectors and 'east' in vectors:
            east_vector = vectors['east']['vector']
            forecast_value = east_vector[11]  # 12th block (index 11)
            logger.info(f"Forecast for block 12 (index 11): {forecast_value}")
            
            if actual_east:
                error = (actual_east - forecast_value) / forecast_value
                logger.info(f"Error: ({actual_east} - {forecast_value}) / {forecast_value} = {error:.4f}")
    
    # Now test with the accuracy learner
    from solar_forecast.processing.accuracy_learner import AccuracyLearner
    
    learner = AccuracyLearner()
    
    # Mock datetime.now in the accuracy_learner module
    test_time = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
    
    with mock.patch('solar_forecast.processing.accuracy_learner.datetime') as mock_datetime:
        mock_datetime.now.return_value = test_time
        # Allow datetime constructor to work normally
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        
        result = learner.update_from_latest_actuals()
        
        logger.info(f"\nAccuracy learner result: {result.get('updates_performed', 0)} updates")
        
        if result.get('updates'):
            for update in result['updates']:
                logger.info(f"  {update}")
    
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
    success = test_learning_directly()
    sys.exit(0 if success else 1)
