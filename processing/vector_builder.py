"""
Vector Builder
==============

Builds standardized 672-element forecast vectors from raw sensor data.
All values in kWh, 15-minute resolution.
Timestamps are stored in UTC to avoid DST issues.
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

from solar_forecast.config import (
    TOTAL_FORECAST_BLOCKS,
    FORECAST_DAYS,
    BLOCKS_PER_DAY,
    SENSOR_GROUPS_FORECAST
)
from solar_forecast.db.client import DatabaseClient
from solar_forecast.sensors.forecast_fetcher import ForecastFetcher

logger = logging.getLogger(__name__)


class VectorBuilder:
    """
    Builds forecast vectors from raw sensor data.
    
    Responsibilities:
    - Fetch raw forecast data
    - Validate vector lengths
    - Store vectors in database with UTC timestamps
    """
    
    def __init__(self):
        self.fetcher = ForecastFetcher()
        self.db = DatabaseClient()
    
    def build_and_store(self, timestamp: datetime = None) -> Dict[str, Any]:
        """
        Build forecast vectors for both roofs and store in database.
        
        Args:
            timestamp: Explicit UTC timestamp, or uses now() if None
            
        Returns:
            dict with results for both roofs
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        else:
            # Ensure timestamp is UTC
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            else:
                timestamp = timestamp.astimezone(timezone.utc)
        
        # Fetch raw data (rolling vector anchored at this timestamp)
        forecasts = self.fetcher.fetch_all(timestamp)
        
        results = {}
        with self.db as db:
            for roof in ['east', 'west']:
                vector = forecasts[roof]['vector']
                
                # Validate
                if len(vector) != TOTAL_FORECAST_BLOCKS:
                    logger.error(f"{roof} vector length {len(vector)} != {TOTAL_FORECAST_BLOCKS}")
                    raise ValueError(f"Invalid vector length for {roof}")
                
                # Store with UTC timestamp
                db.save_forecast_vector(
                    timestamp=timestamp,
                    roof=roof,
                    vector=vector
                )
                
                results[roof] = {
                    'timestamp': timestamp.isoformat(),
                    'vector_length': len(vector),
                    'total_kwh': sum(vector)
                }
                logger.info(f"Stored {roof} vector: {len(vector)} blocks, "
                           f"{sum(vector):.2f} kWh, UTC={timestamp.isoformat()}")
        
        return results
    
    def get_latest_vectors(self) -> Dict[str, Dict[str, Any]]:
        """
        Get the latest forecast vectors from database.
        
        Returns:
            dict with keys 'east' and 'west', each containing
            timestamp (UTC) and vector
        """
        with self.db as db:
            return db.get_latest_forecast_vectors()
    
    def get_vector_at_horizon(
        self,
        roof: str,
        forecast_timestamp: datetime,
        horizon_block: int
    ) -> Optional[float]:
        """
        Get the forecast value for a specific horizon block.
        
        Args:
            roof: 'east' or 'west'
            forecast_timestamp: When the forecast was generated (UTC)
            horizon_block: Which 15-min block ahead (1-672)
            
        Returns:
            Forecast kWh value, or None if not found
        """
        with self.db as db:
            vector = db.get_forecast_vector(
                timestamp=forecast_timestamp.isoformat() if hasattr(forecast_timestamp, 'isoformat') else str(forecast_timestamp),
                roof=roof
            )
            if vector and horizon_block <= len(vector):
                return vector[horizon_block - 1]  # Convert to 0-index
        return None


# ============================================================
# STANDALONE SCRIPT
# ============================================================
if __name__ == '__main__':
    import argparse
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    parser = argparse.ArgumentParser(description='Build forecast vectors')
    parser.add_argument('--test', action='store_true', help='Run in test mode')
    args = parser.parse_args()
    
    try:
        builder = VectorBuilder()
        
        if args.test:
            # Just fetch and display, don't store
            forecasts = builder.fetcher.fetch_all()
            for roof, data in forecasts.items():
                print(f"\n{roof.upper()}:")
                print(f"  Vector length: {len(data['vector'])}")
                print(f"  Total: {sum(data['vector']):.2f} kWh")
        else:
            results = builder.build_and_store()
            for roof, res in results.items():
                print(f"{roof}: {res['total_kwh']:.2f} kWh at {res['timestamp']}")
    
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
