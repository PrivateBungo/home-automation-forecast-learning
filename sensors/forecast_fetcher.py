"""
Forecast Fetcher
================

Fetches raw forecast data from Home Assistant PostgreSQL database.
Converts sensor data (in Wh) to standardized kWh format.
"""

import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from solar_forecast.config import (
    SENSOR_GROUPS,
    BLOCKS_PER_HOUR,
    BLOCKS_PER_DAY,
    FORECAST_DAYS,
    TOTAL_FORECAST_BLOCKS,
    WATTS_TO_KWH_15MIN,
    WH_TO_KWH
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)


class ForecastFetcher:
    """
    Fetches and processes forecast data from Home Assistant sensors.
    
    Responsibilities:
    - Query sensor data from PostgreSQL
    - Convert Wh to kWh
    - Handle missing data
    - Return structured forecast data
    """
    
    def __init__(self):
        self.db = DatabaseClient()
    
    def fetch_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch latest forecast data for both roofs.
        
        Returns:
            dict with keys 'east' and 'west', each containing:
            - 'timestamp': when the forecast was generated
            - 'raw_data': dict of sensor_name -> list of 15-min kWh values
            - 'vector': combined 672-element forecast vector
        """
        with self.db as db:
            result = {}
            for roof in ['east', 'west']:
                result[roof] = self._fetch_roof(db, roof)
            return result
    
    def _fetch_roof(self, db, roof: str) -> Dict[str, Any]:
        """Fetch and process data for a single roof."""
        sensor_list = SENSOR_GROUPS[roof]
        roof_data = {
            'raw_data': {},
            'vector': None,
            'timestamp': None
        }
        
        # Fetch data from all sensors for this roof
        for sensor_id in sensor_list:
            sensor_data = self._fetch_sensor(db, sensor_id)
            if sensor_data:
                roof_data['raw_data'][sensor_id] = sensor_data
                # Use the first sensor's timestamp as the reference
                if roof_data['timestamp'] is None:
                    roof_data['timestamp'] = sensor_data['timestamp']
        
        # Build combined vector
        roof_data['vector'] = self._build_vector(roof_data['raw_data'])
        
        return roof_data
    
    def _fetch_sensor(self, db, entity_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch data from a single sensor.
        
        Returns:
            dict with:
            - 'timestamp': sensor timestamp
            - 'values': list of kWh values (15-min blocks)
        """
        data = db.get_latest_sensor_data(entity_id)
        if not data or not data.get('shared_attrs'):
            logger.warning(f"No data for sensor: {entity_id}")
            return None
        
        attrs = data['shared_attrs']
        timestamp_str = attrs.get('ts')
        
        # Prefer wh_period if available (hourly kWh)
        if 'wh_period' in attrs:
            values = self._process_wh_period(attrs['wh_period'])
        # Otherwise try watts (15-min instantaneous power)
        elif 'watts' in attrs:
            values = self._process_watts(attrs['watts'])
        else:
            logger.warning(f"No wh_period or watts in sensor: {entity_id}")
            return None
        
        return {
            'timestamp': timestamp_str,
            'values': values
        }
    
    def _process_wh_period(self, wh_period: Dict[str, float]) -> List[float]:
        """
        Process wh_period (hourly) to 15-min kWh blocks.
        
        Input: dict with hourly timestamps as keys, Wh as values
        Output: list of kWh values, one per 15-min block
        """
        # Sort by timestamp
        sorted_hours = sorted(wh_period.keys())
        
        result = []
        for hour_str in sorted_hours:
            wh_value = float(wh_period[hour_str])
            # Convert Wh to kWh
            kwh_value = wh_value * WH_TO_KWH
            # Split hourly value into 4 x 15-min blocks (assuming uniform distribution)
            block_value = kwh_value / BLOCKS_PER_HOUR
            result.extend([block_value] * BLOCKS_PER_HOUR)
        
        return result
    
    def _process_watts(self, watts: Dict[str, float]) -> List[float]:
        """
        Process watts (15-min) to kWh.
        
        Input: dict with 15-min timestamps as keys, watts as values
        Output: list of kWh values for each 15-min block
        
        Formula: kWh = watts * (15 minutes / 60 minutes) / 1000
                    = watts * 0.00025
        """
        sorted_times = sorted(watts.keys())
        return [float(watts[t]) * WATTS_TO_KWH_15MIN for t in sorted_times]
    
    def _build_vector(self, raw_data: Dict[str, Dict[str, Any]]) -> List[float]:
        """
        Combine raw data from all sensors into a single 672-element vector.
        """
        # Collect all values from all sensors
        all_values = []
        for sensor_id, sensor_data in sorted(raw_data.items()):
            all_values.extend(sensor_data['values'])
        
        # Truncate or pad to exactly TOTAL_FORECAST_BLOCKS
        if len(all_values) > TOTAL_FORECAST_BLOCKS:
            all_values = all_values[:TOTAL_FORECAST_BLOCKS]
        elif len(all_values) < TOTAL_FORECAST_BLOCKS:
            all_values += [0.0] * (TOTAL_FORECAST_BLOCKS - len(all_values))
        
        # Verify length
        assert len(all_values) == TOTAL_FORECAST_BLOCKS, \
            f"Vector length {len(all_values)} != {TOTAL_FORECAST_BLOCKS}"
        
        return all_values


# ============================================================
# STANDALONE SCRIPT
# ============================================================
if __name__ == '__main__':
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description='Fetch solar forecasts')
    parser.add_argument('--test', action='store_true', help='Run in test mode')
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    
    try:
        fetcher = ForecastFetcher()
        forecasts = fetcher.fetch_all()
        
        if args.test:
            for roof, data in forecasts.items():
                print(f"\n{roof.upper()} Forecast:")
                print(f"  Timestamp: {data['timestamp']}")
                print(f"  Vector length: {len(data['vector'])}")
                print(f"  Total energy: {sum(data['vector']):.2f} kWh")
                print(f"  First 10 values: {data['vector'][:10]}")
        
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
