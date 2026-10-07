"""
Database Client
===============

PostgreSQL client for the solar forecast system.
Handles connections, queries, and array operations.
All timestamps are stored and queried in UTC.
"""

import psycopg2
import psycopg2.extras
from psycopg2 import sql
from typing import Optional, List, Dict, Any
import logging
import json

from solar_forecast.config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_DATABASE,
    TABLE_FORECAST_VECTORS, TABLE_ACTUAL_PRODUCTION, TABLE_ACCURACY, 
    TABLE_COVARIANCE, TABLE_CORRELATION_LOCAL, TABLE_CORRELATION_DAILY,
    FIBONACCI_HORIZONS_BLOCKS
)

logger = logging.getLogger(__name__)


class DatabaseClient:
    """
    PostgreSQL client with connection management.
    
    Usage:
        with DatabaseClient() as db:
            results = db.query("SELECT * FROM table")
    """
    
    def __init__(self):
        self.connection = None
    
    def __enter__(self):
        self.connection = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_DATABASE
        )
        # Set timezone to UTC
        with self.connection.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            self.connection.close()
    
    def query(self, query: str, params: tuple = None, fetch: bool = True):
        """
        Execute a SQL query.

        Args:
            query: SQL query string
            params: Query parameters
            fetch: Whether to fetch results

        Returns:
            List of rows if fetch=True, else None

        Any data-modifying statement (INSERT/UPDATE/DELETE, including
        with RETURNING) is always committed - a fetched result set must
        never leave an open transaction behind, or the write is lost
        when the connection closes.
        """
        with self.connection.cursor() as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall() if fetch else None
        self.connection.commit()
        return rows
    
    def query_one(self, query: str, params: tuple = None):
        """Execute query and return single result."""
        results = self.query(query, params)
        return results[0] if results else None
    
    # ============================================================
    # FORECAST VECTORS
    # ============================================================
    
    def save_forecast_vector(self, timestamp: str, roof: str, vector: List[float]):
        """Save a 672-element forecast vector with UTC timestamp."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table} (timestamp, roof, forecast_vector)
                VALUES (%s, %s, %s)
                ON CONFLICT (timestamp, roof)
                DO UPDATE SET forecast_vector = EXCLUDED.forecast_vector, created_at = NOW()
            """).format(table=sql.Identifier(TABLE_FORECAST_VECTORS)),
                (timestamp, roof, vector))
            self.connection.commit()
    
    def get_forecast_vector(self, timestamp: str, roof: str) -> Optional[List[float]]:
        """Get a forecast vector by timestamp and roof."""
        result = self.query_one(
            sql.SQL("SELECT forecast_vector FROM {table} WHERE timestamp = %s AND roof = %s").format(
                table=sql.Identifier(TABLE_FORECAST_VECTORS)
            ),
            (timestamp, roof)
        )
        return list(result[0]) if result else None
    
    def get_latest_forecast_vectors(self, roof: str = None) -> Dict[str, Any]:
        """Get the latest forecast vectors for both roofs."""
        where_clause = sql.SQL("WHERE roof = %s") if roof else sql.SQL("")
        params = (roof,) if roof else ()
        
        results = self.query(
            sql.SQL("""
                SELECT timestamp, roof, forecast_vector
                FROM {table}
                {where}
                ORDER BY timestamp DESC
                LIMIT 2
            """).format(
                table=sql.Identifier(TABLE_FORECAST_VECTORS),
                where=where_clause
            ),
            params
        )
        
        return {
            row[1]: {
                'timestamp': row[0],
                'vector': list(row[2])
            }
            for row in results
        }
    
    def get_forecast_vectors_near_time(self, timestamp: str) -> Dict[str, Any]:
        """
        Get forecast vectors from near a specific timestamp (within 5 minutes).
        Useful for finding forecasts made N blocks ago.
        """
        results = self.query(
            """
            SELECT timestamp, roof, forecast_vector
            FROM solar_forecast_vectors
            WHERE timestamp BETWEEN %s AND %s
            ORDER BY timestamp DESC
            LIMIT 2
            """,
            (timestamp, timestamp)
        )
        
        if not results:
            return {}
        
        return {
            row[1]: {
                'timestamp': row[0],
                'vector': list(row[2])
            }
            for row in results
        }
    
    # ============================================================
    # ACCURACY MODEL
    # ============================================================
    
    def update_accuracy(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str,
        mean_error: float,
        variance: float,
        sample_count: int
    ):
        """Update mean and variance for a specific cell."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table} 
                (time_of_day_block, forecast_horizon_block, roof, mean_error, variance, sample_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, forecast_horizon_block, roof)
                DO UPDATE SET 
                    mean_error = EXCLUDED.mean_error,
                    variance = EXCLUDED.variance,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_ACCURACY)),
                (time_of_day_block, forecast_horizon_block, roof, 
                 mean_error, variance, sample_count))
            self.connection.commit()
    
    def get_accuracy(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str
    ) -> Optional[Dict[str, Any]]:
        """Get accuracy stats for a specific cell."""
        result = self.query_one(
            sql.SQL("""
                SELECT mean_error, variance, sample_count
                FROM {table}
                WHERE time_of_day_block = %s 
                  AND forecast_horizon_block = %s 
                  AND roof = %s
            """).format(table=sql.Identifier(TABLE_ACCURACY)),
            (time_of_day_block, forecast_horizon_block, roof)
        )
        if result:
            return {
                'mean_error': result[0],
                'variance': result[1],
                'sample_count': result[2]
            }
        return None
    
    def get_accuracy_for_fibonacci(self, time_of_day_block: int) -> Dict[int, Dict[str, Any]]:
        """
        Get accuracy for all Fibonacci horizons at a specific time-of-day block.
        Returns dict keyed by forecast_horizon_block.
        """
        results = {}
        for horizon in FIBONACCI_HORIZONS_BLOCKS:
            east_data = self.get_accuracy(time_of_day_block, horizon, 'east')
            west_data = self.get_accuracy(time_of_day_block, horizon, 'west')
            results[horizon] = {
                'east': east_data,
                'west': west_data
            }
        return results
    
    # ============================================================
    # COVARIANCE MODEL
    # ============================================================
    
    def update_covariance(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        covariance: float,
        correlation: float,
        sample_count: int
    ):
        """Update covariance and correlation for east-west at a specific cell."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (time_of_day_block, forecast_horizon_block, covariance, correlation, sample_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, forecast_horizon_block)
                DO UPDATE SET
                    covariance = EXCLUDED.covariance,
                    correlation = EXCLUDED.correlation,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_COVARIANCE)),
                (time_of_day_block, forecast_horizon_block, covariance, correlation, sample_count))
            self.connection.commit()
    
    def get_covariance(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int
    ) -> Optional[Dict[str, Any]]:
        """Get covariance stats for a specific cell."""
        result = self.query_one(
            sql.SQL("""
                SELECT covariance, correlation, sample_count
                FROM {table}
                WHERE time_of_day_block = %s 
                  AND forecast_horizon_block = %s
            """).format(table=sql.Identifier(TABLE_COVARIANCE)),
            (time_of_day_block, forecast_horizon_block)
        )
        if result:
            return {
                'covariance': result[0],
                'correlation': result[1],
                'sample_count': result[2]
            }
        return None
    
    # ============================================================
    # ACTUAL PRODUCTION
    # ============================================================
    
    def save_actual_production(self, timestamp: str, roof: str, energy_kwh: float):
        """Save actual 15-minute production data."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table} (timestamp, roof, energy_kwh)
                VALUES (%s, %s, %s)
                ON CONFLICT (timestamp, roof)
                DO UPDATE SET energy_kwh = EXCLUDED.energy_kwh, created_at = NOW()
            """).format(table=sql.Identifier(TABLE_ACTUAL_PRODUCTION)),
                (timestamp, roof, energy_kwh))
            self.connection.commit()
    
    def get_actual_production(self, timestamp: str, roof: str) -> Optional[float]:
        """Get actual production for a specific timestamp and roof."""
        result = self.query_one(
            sql.SQL("SELECT energy_kwh FROM {table} WHERE timestamp = %s AND roof = %s").format(
                table=sql.Identifier(TABLE_ACTUAL_PRODUCTION)
            ),
            (timestamp, roof)
        )
        return float(result[0]) if result else None
    
    def get_actual_production_range(
        self, 
        start_timestamp: str, 
        end_timestamp: str,
        roof: str
    ) -> List[Dict[str, Any]]:
        """Get actual production data for a time range."""
        results = self.query(
            sql.SQL("""
                SELECT timestamp, energy_kwh
                FROM {table}
                WHERE timestamp BETWEEN %s AND %s
                  AND roof = %s
                ORDER BY timestamp
            """).format(table=sql.Identifier(TABLE_ACTUAL_PRODUCTION)),
            (start_timestamp, end_timestamp, roof)
        )
        return [{'timestamp': row[0], 'energy_kwh': row[1]} for row in results]
    
    # ============================================================
    # LOCAL CORRELATION
    # ============================================================
    
    def update_correlation_local(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str,
        correlation: float,
        sample_count: int
    ):
        """Update local correlation for a specific cell."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (time_of_day_block, forecast_horizon_block, roof, correlation, sample_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, forecast_horizon_block, roof)
                DO UPDATE SET
                    correlation = EXCLUDED.correlation,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_CORRELATION_LOCAL)),
                (time_of_day_block, forecast_horizon_block, roof, correlation, sample_count))
            self.connection.commit()
    
    def get_correlation_local(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str
    ) -> Optional[Dict[str, Any]]:
        """Get local correlation for a specific cell."""
        result = self.query_one(
            sql.SQL("""
                SELECT correlation, sample_count
                FROM {table}
                WHERE time_of_day_block = %s 
                  AND forecast_horizon_block = %s 
                  AND roof = %s
            """).format(table=sql.Identifier(TABLE_CORRELATION_LOCAL)),
            (time_of_day_block, forecast_horizon_block, roof)
        )
        if result:
            return {
                'correlation': result[0],
                'sample_count': result[1]
            }
        return None
    
    # ============================================================
    # DAILY CORRELATION
    # ============================================================
    
    def update_correlation_daily(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str,
        correlation: float,
        sample_count: int
    ):
        """Update daily correlation for a specific cell."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (time_of_day_block, forecast_horizon_block, roof, correlation, sample_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, forecast_horizon_block, roof)
                DO UPDATE SET
                    correlation = EXCLUDED.correlation,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_CORRELATION_DAILY)),
                (time_of_day_block, forecast_horizon_block, roof, correlation, sample_count))
            self.connection.commit()
    
    def get_correlation_daily(
        self,
        time_of_day_block: int,
        forecast_horizon_block: int,
        roof: str
    ) -> Optional[Dict[str, Any]]:
        """Get daily correlation for a specific cell."""
        result = self.query_one(
            sql.SQL("""
                SELECT correlation, sample_count
                FROM {table}
                WHERE time_of_day_block = %s 
                  AND forecast_horizon_block = %s 
                  AND roof = %s
            """).format(table=sql.Identifier(TABLE_CORRELATION_DAILY)),
            (time_of_day_block, forecast_horizon_block, roof)
        )
        if result:
            return {
                'correlation': result[0],
                'sample_count': result[1]
            }
        return None
    
    # ============================================================
    # HOME ASSISTANT SENSOR DATA
    # ============================================================
    
    def get_latest_sensor_data(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the latest state and attributes for a Home Assistant sensor.
        
        Returns:
            dict with 'state' and 'shared_attrs' keys, or None
        """
        result = self.query_one(
            """
                SELECT s.state, sa.shared_attrs
                FROM states s
                JOIN state_attributes sa ON s.attributes_id = sa.attributes_id
                JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                WHERE sm.entity_id = %s
                  AND s.state NOT IN ('unavailable', 'unknown')
                ORDER BY COALESCE(s.last_reported_ts, s.last_changed_ts) DESC NULLS LAST,
                         s.state_id DESC
                LIMIT 1
            """,
            (entity_id,)
        )
        if result:
            try:
                attrs = json.loads(result[1]) if result[1] else {}
                return {
                    'state': result[0],
                    'shared_attrs': attrs
                }
            except (json.JSONDecodeError, TypeError):
                return None
        return None
    
    # ============================================================
    # FIBONACCI HORIZONS
    # ============================================================
    
    def get_fibonacci_horizons(self) -> List[int]:
        """Get list of Fibonacci horizons in blocks."""
        return FIBONACCI_HORIZONS_BLOCKS
