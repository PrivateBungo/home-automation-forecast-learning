"""
Central Configuration
====================

All configuration constants for the solar forecast learning system.
"""

# ============================================================
# DATABASE
# ============================================================
DB_HOST = '10.100.123.53'
DB_PORT = 5432
DB_USER = 'postgres'
DB_PASSWORD = '123GjH#@!'
DB_DATABASE = 'homeassistant'

# ============================================================
# TIME STRUCTURE (15-minute blocks)
# ============================================================
BLOCKS_PER_HOUR = 4
BLOCKS_PER_DAY = 24 * BLOCKS_PER_HOUR  # 96
FORECAST_DAYS = 7
TOTAL_FORECAST_BLOCKS = FORECAST_DAYS * BLOCKS_PER_DAY  # 672

# ============================================================
# LEARNING HORIZONS (Fibonacci sequence)
# ============================================================
FIBONACCI_HORIZONS_HOURS = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 168]
FIBONACCI_HORIZONS_BLOCKS = [h * BLOCKS_PER_HOUR for h in FIBONACCI_HORIZONS_HOURS]
# Result: [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672]

# ============================================================
# TEMPORAL CORRELATION WINDOWS
# ============================================================
LOCAL_WINDOW_BLOCKS = list(range(-8, 1))  # [-8, -7, ..., 0] = 9 blocks = 2h15m
DAILY_OFFSET_BLOCKS = 96  # 24 hours = 96 blocks

# ============================================================
# SENSOR GROUPS
# ============================================================
# Legacy name for backward compatibility
SENSOR_GROUPS = {
    'east': [
        'sensor.energy_production_today_3',
        'sensor.energy_production_tomorrow_3',
        'sensor.energy_production_d2',
        'sensor.energy_production_d3',
        'sensor.energy_production_d4',
        'sensor.energy_production_d5',
        'sensor.energy_production_d6',
    ],
    'west': [
        'sensor.energy_production_today_4',
        'sensor.energy_production_tomorrow_4',
        'sensor.energy_production_d2_2',
        'sensor.energy_production_d3_2',
        'sensor.energy_production_d4_2',
        'sensor.energy_production_d5_2',
        'sensor.energy_production_d6_2',
    ]
}

# Forecast sensors: used to build 672-block vectors
SENSOR_GROUPS_FORECAST = SENSOR_GROUPS

# Actual sensors: 15-minute actual energy
SENSOR_QUARTERLY_ENERGY = {
    'east': 'sensor.solar_quaterly_energy_east_roof',
    'west': 'sensor.solar_quaterly_energy_west_roof'
}

# ============================================================
# LEARNING PARAMETERS
# ============================================================
EMA_ALPHA = 0.2  # Smoothing factor for EMA (0 < alpha <= 1)
MIN_SAMPLES = 5   # Minimum observations before using learned values

# ============================================================
# TIME & SCHEDULING
# ============================================================
LEARNING_FREQUENCY_MINUTES = 15

# ============================================================
# DATA STANDARDS
# ============================================================
# All energy values are stored and processed in kWh
# All time values are in 15-minute blocks (0-indexed)
# All timestamps are in UTC (no DST issues)

# Wh -> kWh conversion factor
WH_TO_KWH = 0.001  # Divide by 1000

# Watts -> kWh for 15 minutes: watts * (15/3600) / 1000 = watts * 0.000041667
WATTS_TO_KWH_15MIN = 0.0000416667

# ============================================================
# DATABASE TABLES
# ============================================================
TABLE_FORECAST_VECTORS = 'solar_forecast_vectors'
TABLE_ACTUAL_PRODUCTION = 'solar_actual_production'
TABLE_ACCURACY = 'solar_accuracy'
TABLE_COVARIANCE = 'solar_covariance'
TABLE_CORRELATION_LOCAL = 'solar_correlation_local'
TABLE_CORRELATION_DAILY = 'solar_correlation_daily'

# ============================================================
# FILE PATHS
# ============================================================
import os
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(PROJECT_ROOT, 'db', 'schema.sql')
