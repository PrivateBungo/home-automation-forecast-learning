"""
Forecast Fetcher
================

Fetches raw forecast data from Home Assistant PostgreSQL database.
Converts sensor data (in Wh) to standardized kWh format.

The day-based sensors (today/tomorrow/d2..d7) are each anchored at
midnight of their own calendar day. This fetcher merges them into a
single time-indexed map of 15-minute blocks (UTC), then slices out a
ROLLING window of TOTAL_FORECAST_BLOCKS blocks that starts at the next
15-minute boundary after "now". The result is a vector that always
represents the next 168 hours from the current moment, regardless of
the time of day.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone

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
    - Anchor every value to its absolute UTC timestamp
    - Build a rolling now-anchored 672-block forecast vector
    """
    
    def __init__(self):
        self.db = DatabaseClient()
    
    def fetch_all(self, now_utc: datetime = None) -> Dict[str, Dict[str, Any]]:
        """
        Fetch latest forecast data for both roofs.
        
        Args:
            now_utc: Reference time (UTC). Vector starts at the next
                     15-min boundary after this moment. Defaults to now.
        
        Returns:
            dict with keys 'east' and 'west', each containing:
            - 'timestamp': when the forecast was generated (now_utc)
            - 'vector_start': UTC start time of vector[0]
            - 'raw_data': dict of sensor_name -> block map (UTC start -> kWh)
            - 'vector': rolling 672-element forecast vector from now
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        elif now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        
        with self.db as db:
            result = {}
            for roof in ['east', 'west']:
                result[roof] = self._fetch_roof(db, roof, now_utc)
            return result
    
    def _fetch_roof(self, db, roof: str, now_utc: datetime) -> Dict[str, Any]:
        """Fetch and process data for a single roof."""
        sensor_list = SENSOR_GROUPS[roof]
        roof_data = {
            'raw_data': {},
            'vector': None,
            'timestamp': now_utc.isoformat()
        }
        
        # Merge blocks from all sensors into one time-indexed map
        block_map: Dict[datetime, float] = {}
        for sensor_id in sensor_list:
            sensor_data = self._fetch_sensor(db, sensor_id)
            if sensor_data:
                roof_data['raw_data'][sensor_id] = sensor_data['blocks']
                block_map.update(sensor_data['blocks'])
            else:
                logger.warning(f"No data for sensor: {sensor_id}")
        
        # Build rolling vector anchored at "now"
        roof_data['vector_start'] = self._vector_start(now_utc)
        roof_data['vector'] = self._build_vector(block_map, roof_data['vector_start'])
        
        return roof_data
    
    def _fetch_sensor(self, db, entity_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch data from a single sensor.
        
        Returns:
            dict with:
            - 'blocks': dict of block start (UTC datetime) -> kWh
        """
        data = db.get_latest_sensor_data(entity_id)
        if not data or not data.get('shared_attrs'):
            return None
        
        attrs = data['shared_attrs']
        
        # Prefer wh_period if available (hourly Wh)
        if 'wh_period' in attrs:
            blocks = self._process_wh_period(attrs['wh_period'])
        # Otherwise try watts (15-min instantaneous power)
        elif 'watts' in attrs:
            blocks = self._process_watts(attrs['watts'])
        else:
            logger.warning(f"No wh_period or watts in sensor: {entity_id}")
            return None
        
        return {
            'blocks': blocks
        }
    
    def _parse_timestamp(self, ts_str: str) -> Optional[datetime]:
        """
        Parse a sensor timestamp key into an aware UTC datetime.
        
        Handles ISO strings with or without UTC offset
        (e.g. '2026-10-05T01:00:00+02:00').
        Naive timestamps are assumed to be UTC.
        """
        try:
            ts = datetime.fromisoformat(str(ts_str))
        except (ValueError, TypeError):
            logger.warning(f"Unparseable timestamp: {ts_str!r}")
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    
    def _process_wh_period(self, wh_period: Dict[str, float]) -> Dict[datetime, float]:
        """
        Process wh_period (hourly) into 15-min kWh blocks.
        
        Input: dict with hourly timestamps as keys, Wh as values
        Output: dict of block start (UTC) -> kWh (15-min block)
        """
        blocks: Dict[datetime, float] = {}
        for hour_str, wh_value in wh_period.items():
            hour_start = self._parse_timestamp(hour_str)
            if hour_start is None:
                continue
            kwh_value = float(wh_value) * WH_TO_KWH
            # Split hourly value into 4 x 15-min blocks (uniform distribution)
            block_value = kwh_value / BLOCKS_PER_HOUR
            for i in range(BLOCKS_PER_HOUR):
                blocks[hour_start + timedelta(minutes=15 * i)] = block_value
        return blocks
    
    def _process_watts(self, watts: Dict[str, float]) -> Dict[datetime, float]:
        """
        Process watts (15-min) to kWh.
        
        Input: dict with 15-min timestamps as keys, watts as values
        Output: dict of block start (UTC) -> kWh
        
        Formula: kWh = watts * (15 minutes / 60 minutes) / 1000
        """
        blocks: Dict[datetime, float] = {}
        for ts_str, watt_value in watts.items():
            block_start = self._parse_timestamp(ts_str)
            if block_start is None:
                continue
            blocks[block_start] = float(watt_value) * WATTS_TO_KWH_15MIN
        return blocks
    
    def _vector_start(self, now_utc: datetime) -> datetime:
        """
        UTC start time of vector[0]: the next 15-min block after now.
        
        The accuracy learner indexes the vector so that vector[h-1] is
        the block starting h*15 minutes AFTER the forecast timestamp;
        therefore vector[0] is the block immediately following now,
        not the block we are currently in.
        """
        floored = now_utc.replace(minute=(now_utc.minute // 15) * 15,
                                  second=0, microsecond=0)
        return floored + timedelta(minutes=15)
    
    def _build_vector(
        self,
        block_map: Dict[datetime, float],
        vector_start: datetime
    ) -> List[float]:
        """
        Build the rolling forecast vector.
        
        Extracts TOTAL_FORECAST_BLOCKS consecutive 15-min blocks starting
        at vector_start. Blocks with no sensor data are filled with 0.0
        and logged so coverage gaps are visible.
        """
        vector = []
        missing = 0
        for i in range(TOTAL_FORECAST_BLOCKS):
            block_time = vector_start + timedelta(minutes=15 * i)
            value = block_map.get(block_time)
            if value is None:
                vector.append(0.0)
                missing += 1
            else:
                vector.append(float(value))
        
        if missing > 0:
            logger.warning(
                f"Rolling vector: {missing}/{TOTAL_FORECAST_BLOCKS} blocks "
                f"have no sensor data (filled with 0.0)"
            )
        
        assert len(vector) == TOTAL_FORECAST_BLOCKS, \
            f"Vector length {len(vector)} != {TOTAL_FORECAST_BLOCKS}"
        
        return vector


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
                print(f"  Vector start (UTC): {data['vector_start']}")
                print(f"  Vector length: {len(data['vector'])}")
                print(f"  Total energy: {sum(data['vector']):.2f} kWh")
                print(f"  First 10 values: {data['vector'][:10]}")
                print(f"  Last block ends: "
                      f"{data['vector_start'] + timedelta(minutes=15 * len(data['vector']))}")
    
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
