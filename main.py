#!/usr/bin/env python3
"""
Main Orchestration Script
========================

Entry point for the solar forecast learning system.
Orchestrates the pipeline: fetch, build, learn, store.

Run frequency: Every 15 minutes (not hourly)

Usage:
    # Run full pipeline
    python3 main.py
    
    # Run in test mode (print to terminal, no DB writes)
    python3 main.py --test
    
    # Force run (ignore 15-min timing)
    python3 main.py --force

Schedule with cron (every 15 minutes):
    */15 * * * * /usr/bin/python3 /home/gijs/solar_forecast/main.py
"""

import sys
import os
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

import argparse
import logging
from datetime import datetime, timezone
from typing import Optional

from solar_forecast.config import (
    TOTAL_FORECAST_BLOCKS,
    LEARNING_FREQUENCY_MINUTES,
    FIBONACCI_HORIZONS_BLOCKS
)
from solar_forecast.db.client import DatabaseClient
from solar_forecast.sensors.forecast_fetcher import ForecastFetcher
from solar_forecast.processing.vector_builder import VectorBuilder
from solar_forecast.processing.accuracy_learner import AccuracyLearner

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Orchestrates the solar forecast learning pipeline.
    
    Pipeline (runs every 15 minutes):
    1. Build and store forecast vectors (from 7-day sensors)
    2. Update accuracy model (using Fibonacci horizons)
    
    Key design decisions:
    - Learning frequency: 15 minutes (not hourly)
    - Learning targets: Fibonacci horizons only (computationally efficient)
    - What we learn: ERROR between forecast and actual, not the forecast itself
    - Timestamps: All UTC to avoid DST issues
    
    Responsibilities:
    - Coordinate module interactions
    - Handle timing (15-min execution)
    - Manage errors gracefully
    - Log progress
    """
    
    def __init__(self):
        self.builder = VectorBuilder()
        self.learner = AccuracyLearner()
    
    def run(self, test_mode: bool = False, force: bool = False) -> dict:
        """
        Run the full pipeline.
        
        Args:
            test_mode: If True, print to terminal, no DB writes
            force: If True, run immediately regardless of time
            
        Returns:
            dict with results summary
        """
        timestamp = datetime.now(timezone.utc)
        
        if not force and not self._is_learning_time(timestamp):
            logger.info(f"Not 15-min boundary (minute={timestamp.minute}), skipping")
            return {'status': 'skipped', 'reason': 'not_15min_boundary'}
        
        logger.info(f"Starting pipeline at {timestamp.isoformat()} UTC")
        
        results = {}
        
        # Step 1: Build and store forecast vectors
        logger.info("Step 1/2: Building forecast vectors...")
        if test_mode:
            vectors = self.builder.fetcher.fetch_all()
            for roof, data in vectors.items():
                print(f"\n{roof.upper()} Forecast Vector:")
                print(f"  Timestamp: {data.get('timestamp')}")
                print(f"  Length: {len(data.get('vector', []))} blocks")
                print(f"  Total: {sum(data.get('vector', [])):.2f} kWh")
            results['vectors'] = vectors
        else:
            vector_results = self.builder.build_and_store(timestamp)
            results['vectors'] = vector_results
            logger.info("Forecast vectors stored with UTC timestamp")
        
        # Step 2: Update accuracy model
        logger.info("Step 2/2: Updating accuracy model...")
        if test_mode:
            print("\n[TEST] Would update accuracy model for Fibonacci horizons:")
            print(f"  {FIBONACCI_HORIZONS_BLOCKS}")
        else:
            learn_result = self.learner.update_from_latest_actuals()
            results['accuracy'] = learn_result
            logger.info(f"Accuracy model updated: {learn_result.get('updates_performed', 0)} cells")
        
        results['status'] = 'completed'
        results['timestamp'] = timestamp.isoformat()
        results['fibonacci_horizons'] = FIBONACCI_HORIZONS_BLOCKS
        
        return results
    
    def _is_learning_time(self, timestamp: datetime) -> bool:
        """Check if we're at a 15-minute boundary."""
        # Run at :00, :15, :30, :45 past each hour
        return timestamp.minute % LEARNING_FREQUENCY_MINUTES == 0


# ============================================================
# MAIN ENTRY POINT
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Solar Forecast Learning System - Main Orchestrator'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode: print to terminal, no database writes'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force run immediately (ignore 15-min timing)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose logging'
    )
    args = parser.parse_args()
    
    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    try:
        orchestrator = Orchestrator()
        result = orchestrator.run(
            test_mode=args.test,
            force=args.force
        )
        
        if args.test:
            print(f"\n[TEST MODE] Completed at {result.get('timestamp')}")
            print(f"Fibonacci horizons (blocks): {result.get('fibonacci_horizons')}")
        else:
            logger.info(f"Pipeline completed at {result.get('timestamp')}")
        
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
