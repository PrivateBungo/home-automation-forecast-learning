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
    TABLE_REFERENCE_MAGNITUDE, TABLE_COMBINED_FORECAST,
    TABLE_SHOWER_EVENTS, TABLE_SHOWER_PROBABILITY, TABLE_SHOWER_CORRELATION_LAG,
    TABLE_VAR_MATRICES, TABLE_PRICE_FORECAST, TABLE_PRICE_FORECAST_15MIN,
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
        """Get the latest forecast vectors for both roofs.
        
        With a roof filter, returns exactly that roof's latest vector
        (LIMIT 1). Without, returns the latest row per roof (LIMIT 2:
        one east, one west).
        """
        where_clause = sql.SQL("WHERE roof = %s") if roof else sql.SQL("")
        params = (roof,) if roof else ()
        row_limit = 1 if roof else 2
        
        results = self.query(
            sql.SQL("""
                SELECT timestamp, roof, forecast_vector
                FROM {table}
                {where}
                ORDER BY timestamp DESC
                LIMIT {limit}
            """).format(
                table=sql.Identifier(TABLE_FORECAST_VECTORS),
                where=where_clause,
                limit=sql.Literal(row_limit)
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
        mean_abs_error: float,
        variance: float,
        sample_count: int
    ):
        """Update a specific cell.
        
        mean_error: EMA of normalized error (dimensionless)
        mean_abs_error: EMA of absolute error (kWh)
        variance: EMA variance of absolute errors (kWh^2)
        """
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table} 
                (time_of_day_block, forecast_horizon_block, roof, mean_error, mean_abs_error, variance, sample_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, forecast_horizon_block, roof)
                DO UPDATE SET 
                    mean_error = EXCLUDED.mean_error,
                    mean_abs_error = EXCLUDED.mean_abs_error,
                    variance = EXCLUDED.variance,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_ACCURACY)),
                (time_of_day_block, forecast_horizon_block, roof, 
                 mean_error, mean_abs_error, variance, sample_count))
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
                SELECT mean_error, mean_abs_error, variance, sample_count
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
                'mean_abs_error': result[1],
                'variance': result[2],
                'sample_count': result[3]
            }
        return None
    
    # ============================================================
    # REFERENCE MAGNITUDE
    # ============================================================
    
    def update_reference_magnitude(
        self,
        time_of_day_block: int,
        roof: str,
        reference_kwh: float,
        sample_count: int
    ):
        """Update the reference magnitude for a (time_of_day_block, roof)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (time_of_day_block, roof, reference_kwh, sample_count)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (time_of_day_block, roof)
                DO UPDATE SET
                    reference_kwh = EXCLUDED.reference_kwh,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_REFERENCE_MAGNITUDE)),
                (time_of_day_block, roof, reference_kwh, sample_count))
            self.connection.commit()
    
    def get_reference_magnitude(
        self,
        time_of_day_block: int,
        roof: str
    ) -> Optional[Dict[str, Any]]:
        """Get reference magnitude for a (time_of_day_block, roof)."""
        result = self.query_one(
            sql.SQL("""
                SELECT reference_kwh, sample_count
                FROM {table}
                WHERE time_of_day_block = %s
                  AND roof = %s
            """).format(table=sql.Identifier(TABLE_REFERENCE_MAGNITUDE)),
            (time_of_day_block, roof)
        )
        if result:
            return {
                'reference_kwh': result[0],
                'sample_count': result[1]
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
    # COMBINED FORECAST
    # ============================================================
    
    def save_combined_forecast(
        self,
        issued_at: str,
        mean_kwh: List[float],
        variance_kwh2: List[float],
        corr_ew: List[float]
    ):
        """Save a combined forecast vector set (672 blocks each)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (issued_at, mean_kwh, variance_kwh2, corr_ew)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (issued_at)
                DO UPDATE SET
                    mean_kwh = EXCLUDED.mean_kwh,
                    variance_kwh2 = EXCLUDED.variance_kwh2,
                    corr_ew = EXCLUDED.corr_ew,
                    created_at = NOW()
            """).format(table=sql.Identifier(TABLE_COMBINED_FORECAST)),
                (issued_at, mean_kwh, variance_kwh2, corr_ew))
            self.connection.commit()
    
    def get_latest_combined_forecast(self) -> Optional[Dict[str, Any]]:
        """Get the most recent combined forecast."""
        result = self.query_one(
            sql.SQL("""
                SELECT issued_at, mean_kwh, variance_kwh2, corr_ew
                FROM {table}
                ORDER BY issued_at DESC
                LIMIT 1
            """).format(table=sql.Identifier(TABLE_COMBINED_FORECAST))
        )
        if result:
            return {
                'issued_at': result[0],
                'mean_kwh': list(result[1]),
                'variance_kwh2': list(result[2]),
                'corr_ew': list(result[3])
            }
        return None
    
    # ============================================================
    # PRICE FORECAST (NL day-ahead wholesale)
    # ============================================================

    def save_price_forecast(
        self,
        issued_at: str,
        start_utc: str,
        horizon_hours: int,
        mean_eur_kwh: List[float],
        variance_eur_kwh2: List[float]
    ):
        """Save a daily price forecast (hourly mean/variance arrays)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (issued_at, start_utc, horizon_hours, mean_eur_kwh, variance_eur_kwh2)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (issued_at)
                DO UPDATE SET
                    start_utc = EXCLUDED.start_utc,
                    horizon_hours = EXCLUDED.horizon_hours,
                    mean_eur_kwh = EXCLUDED.mean_eur_kwh,
                    variance_eur_kwh2 = EXCLUDED.variance_eur_kwh2,
                    created_at = NOW()
            """).format(table=sql.Identifier(TABLE_PRICE_FORECAST)),
                (issued_at, start_utc, horizon_hours,
                 mean_eur_kwh, variance_eur_kwh2))
            self.connection.commit()

    def get_latest_price_forecast(self) -> Optional[Dict[str, Any]]:
        """Get the most recent price forecast."""
        result = self.query_one(
            sql.SQL("""
                SELECT issued_at, start_utc, horizon_hours,
                       mean_eur_kwh, variance_eur_kwh2
                FROM {table}
                ORDER BY issued_at DESC
                LIMIT 1
            """).format(table=sql.Identifier(TABLE_PRICE_FORECAST))
        )
        if result:
            return {
                'issued_at': result[0],
                'start_utc': result[1],
                'horizon_hours': result[2],
                'mean_eur_kwh': list(result[3]),
                'variance_eur_kwh2': list(result[4])
            }
        return None

    def save_price_forecast_15min(
        self,
        issued_at: str,
        start_utc: str,
        horizon_blocks: int,
        mean_eur_kwh: List[float],
        variance_eur_kwh2: List[float],
        published_blocks: int
    ):
        """Save a rolling 15-minute price forecast vector."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (issued_at, start_utc, horizon_blocks, mean_eur_kwh,
                 variance_eur_kwh2, published_blocks)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (issued_at)
                DO UPDATE SET
                    start_utc = EXCLUDED.start_utc,
                    horizon_blocks = EXCLUDED.horizon_blocks,
                    mean_eur_kwh = EXCLUDED.mean_eur_kwh,
                    variance_eur_kwh2 = EXCLUDED.variance_eur_kwh2,
                    published_blocks = EXCLUDED.published_blocks,
                    created_at = NOW()
            """).format(table=sql.Identifier(TABLE_PRICE_FORECAST_15MIN)),
                (issued_at, start_utc, horizon_blocks, mean_eur_kwh,
                 variance_eur_kwh2, published_blocks))
            self.connection.commit()

    def get_latest_price_forecast_15min(self) -> Optional[Dict[str, Any]]:
        """Get the most recent rolling 15-minute price forecast."""
        result = self.query_one(
            sql.SQL("""
                SELECT issued_at, start_utc, horizon_blocks,
                       mean_eur_kwh, variance_eur_kwh2, published_blocks
                FROM {table}
                ORDER BY issued_at DESC
                LIMIT 1
            """).format(table=sql.Identifier(TABLE_PRICE_FORECAST_15MIN))
        )
        if result:
            return {
                'issued_at': result[0],
                'start_utc': result[1],
                'horizon_blocks': result[2],
                'mean_eur_kwh': list(result[3]),
                'variance_eur_kwh2': list(result[4]),
                'published_blocks': result[5]
            }
        return None

    def prune_price_forecast_15min(self, retention_days: int) -> int:
        """Delete published 15-minute rows older than retention_days."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                DELETE FROM {table}
                WHERE issued_at < NOW() - (%s * INTERVAL '1 day')
            """).format(table=sql.Identifier(TABLE_PRICE_FORECAST_15MIN)),
                (retention_days,))
            deleted = cur.rowcount
            self.connection.commit()
        return deleted

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
    # SHOWER BEHAVIOUR LEARNING
    # ============================================================
    
    def get_binary_sensor_states_between(
        self,
        entity_id: str,
        start_timestamp: str,
        end_timestamp: str
    ) -> List[Dict[str, Any]]:
        """
        Get binary sensor state changes from the Home Assistant recorder.
        
        Includes the last known state before start_timestamp so positive
        (off -> on) edges at the window boundary are detected.
        'unknown'/'unavailable' states are skipped (transparent), so a
        restart does not suppress a real off -> on edge.
        
        Args:
            entity_id: Home Assistant entity id
            start_timestamp: UTC ISO timestamp (inclusive window start)
            end_timestamp: UTC ISO timestamp (exclusive window end)
            
        Returns:
            List of dicts with 'timestamp' (datetime, UTC) and 'state' (str),
            ordered by timestamp.
        """
        # Last known state before the window (edge detection context)
        prior = self.query_one(
            """
                SELECT to_timestamp(s.last_updated_ts) AS ts, s.state
                FROM states s
                JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                WHERE sm.entity_id = %s
                  AND s.last_updated_ts < EXTRACT(EPOCH FROM %s::TIMESTAMPTZ)
                  AND s.state IN ('on', 'off')
                ORDER BY s.last_updated_ts DESC
                LIMIT 1
            """,
            (entity_id, start_timestamp)
        )
        
        # States inside the window
        results = self.query(
            """
                SELECT to_timestamp(s.last_updated_ts) AS ts, s.state
                FROM states s
                JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                WHERE sm.entity_id = %s
                  AND s.last_updated_ts >= EXTRACT(EPOCH FROM %s::TIMESTAMPTZ)
                  AND s.last_updated_ts < EXTRACT(EPOCH FROM %s::TIMESTAMPTZ)
                  AND s.state IN ('on', 'off')
                ORDER BY s.last_updated_ts
            """,
            (entity_id, start_timestamp, end_timestamp)
        )
        
        states = []
        if prior:
            states.append({'timestamp': prior[0], 'state': prior[1]})
        states.extend({'timestamp': row[0], 'state': row[1]} for row in results)
        return states
    
    def save_shower_events(self, sensor: str, timestamps: List[str]):
        """Store debounced shower event timestamps (idempotent upsert)."""
        if not timestamps:
            return
        with self.connection.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                sql.SQL("""
                    INSERT INTO {table} (timestamp, sensor)
                    VALUES %s
                    ON CONFLICT (timestamp, sensor) DO NOTHING
                """).format(table=sql.Identifier(TABLE_SHOWER_EVENTS)).as_string(self.connection),
                [(ts, sensor) for ts in timestamps]
            )
            self.connection.commit()
    
    def get_shower_events_between(
        self,
        sensor: str,
        start_timestamp: str,
        end_timestamp: str
    ) -> List[Dict[str, Any]]:
        """Get debounced shower events in a time range (UTC datetimes)."""
        results = self.query(
            sql.SQL("""
                SELECT timestamp
                FROM {table}
                WHERE sensor = %s AND timestamp >= %s AND timestamp < %s
                ORDER BY timestamp
            """).format(table=sql.Identifier(TABLE_SHOWER_EVENTS)),
            (sensor, start_timestamp, end_timestamp)
        )
        return [row[0] for row in results]
    
    def get_last_shower_event(self, sensor: str) -> Optional[Any]:
        """Get the most recent stored shower event timestamp (UTC datetime), or None."""
        result = self.query_one(
            sql.SQL("SELECT MAX(timestamp) FROM {table} WHERE sensor = %s").format(
                table=sql.Identifier(TABLE_SHOWER_EVENTS)
            ),
            (sensor,)
        )
        return result[0] if result else None
    
    def get_shower_probability(self, sensor: str) -> Optional[Dict[str, Any]]:
        """Get the learned 168h shower probability vector state."""
        result = self.query_one(
            sql.SQL("""
                SELECT p_shower_168h, mean_x, sample_count, last_x,
                       updated_hour_index, last_updated_hour
                FROM {table}
                WHERE sensor = %s
            """).format(table=sql.Identifier(TABLE_SHOWER_PROBABILITY)),
            (sensor,)
        )
        if result:
            return {
                'p_shower_168h': list(result[0]),
                'mean_x': result[1],
                'sample_count': result[2],
                'last_x': result[3],
                'updated_hour_index': result[4],
                'last_updated_hour': result[5]
            }
        return None
    
    def save_shower_probability(
        self,
        sensor: str,
        p_shower_168h: List[float],
        mean_x: float,
        sample_count: int,
        last_x: Optional[int],
        updated_hour_index: int,
        last_updated_hour: str
    ):
        """Save the 168h shower probability vector (upsert)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (sensor, p_shower_168h, mean_x, sample_count, last_x,
                 updated_hour_index, last_updated_hour)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sensor)
                DO UPDATE SET
                    p_shower_168h = EXCLUDED.p_shower_168h,
                    mean_x = EXCLUDED.mean_x,
                    sample_count = EXCLUDED.sample_count,
                    last_x = EXCLUDED.last_x,
                    updated_hour_index = EXCLUDED.updated_hour_index,
                    last_updated_hour = EXCLUDED.last_updated_hour,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_SHOWER_PROBABILITY)),
                (sensor, p_shower_168h, mean_x, sample_count, last_x,
                 updated_hour_index, last_updated_hour))
            self.connection.commit()
    
    def get_shower_correlation_lag(self, sensor: str, lag_hours: int) -> Optional[Dict[str, Any]]:
        """Get the var24 correlation state for one lag."""
        result = self.query_one(
            sql.SQL("""
                SELECT mean_xy, correlation, sample_count
                FROM {table}
                WHERE sensor = %s AND lag_hours = %s
            """).format(table=sql.Identifier(TABLE_SHOWER_CORRELATION_LAG)),
            (sensor, lag_hours)
        )
        if result:
            return {
                'mean_xy': result[0],
                'correlation': result[1],
                'sample_count': result[2]
            }
        return None
    
    def update_shower_correlation_lag(
        self,
        sensor: str,
        lag_hours: int,
        mean_xy: float,
        correlation: float,
        sample_count: int
    ):
        """Update the var24 correlation state for one lag (upsert)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table}
                (sensor, lag_hours, mean_xy, correlation, sample_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sensor, lag_hours)
                DO UPDATE SET
                    mean_xy = EXCLUDED.mean_xy,
                    correlation = EXCLUDED.correlation,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """).format(table=sql.Identifier(TABLE_SHOWER_CORRELATION_LAG)),
                (sensor, lag_hours, mean_xy, correlation, sample_count))
            self.connection.commit()
    
    # ============================================================
    # MODEL VAR MATRICES (append-only snapshots)
    # ============================================================
    
    def insert_var_matrix(
        self,
        matrix_type: str,
        calculated_at: str,
        target_hour: str,
        payload: Dict[str, Any]
    ):
        """Append a model state snapshot (jsonb payload)."""
        with self.connection.cursor() as cur:
            cur.execute(sql.SQL("""
                INSERT INTO {table} (matrix_type, calculated_at, target_hour, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (matrix_type, calculated_at, target_hour) DO NOTHING
            """).format(table=sql.SQL(TABLE_VAR_MATRICES)),
                (matrix_type, calculated_at, target_hour,
                 psycopg2.extras.Json(payload)))
            self.connection.commit()
    
    def get_latest_var_matrix(self, matrix_type: str) -> Optional[Dict[str, Any]]:
        """
        Get the most recent snapshot for a matrix type.
        
        Returns:
            dict with 'calculated_at', 'target_hour' (datetimes) and
            'payload' (parsed json), or None.
        """
        result = self.query_one(
            sql.SQL("""
                SELECT calculated_at, target_hour, payload
                FROM {table}
                WHERE matrix_type = %s
                ORDER BY calculated_at DESC
                LIMIT 1
            """).format(table=sql.SQL(TABLE_VAR_MATRICES)),
            (matrix_type,)
        )
        if result:
            return {
                'calculated_at': result[0],
                'target_hour': result[1],
                'payload': result[2]
            }
        return None
    
    # ============================================================
    # FIBONACCI HORIZONS
    # ============================================================
    
    def get_fibonacci_horizons(self) -> List[int]:
        """Get list of Fibonacci horizons in blocks."""
        return FIBONACCI_HORIZONS_BLOCKS
