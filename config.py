"""
Central Configuration
====================

All configuration constants for the solar forecast learning system.
"""

import os

# ============================================================
# DATABASE
# ============================================================
# Environment overrides let tests run against an isolated database
# (SOLAR_FORECAST_DB_DATABASE) without touching production data.
DB_HOST = os.environ.get('SOLAR_FORECAST_DB_HOST', '10.100.123.53')
DB_PORT = int(os.environ.get('SOLAR_FORECAST_DB_PORT', '5432'))
DB_USER = os.environ.get('SOLAR_FORECAST_DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('SOLAR_FORECAST_DB_PASSWORD', '123GjH#@!')
DB_DATABASE = os.environ.get('SOLAR_FORECAST_DB_DATABASE', 'homeassistant')

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
        'sensor.energy_production_d6',        # east d6
        'sensor.energy_production_d7',        # east d7
    ],
    'west': [
        'sensor.energy_production_today_4',
        'sensor.energy_production_tomorrow_4',
        'sensor.energy_production_d2_2',
        'sensor.energy_production_d3_2',
        'sensor.energy_production_d4_2',
        'sensor.energy_production_d5_2',
        'sensor.energy_production_d6_2',
        'sensor.energy_production_d7_2',
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
# No minimum-sample gate: cells are seeded on first observation and
# mature over time via EMA decay.

# Reference magnitude: slow EMA of actual production per
# (time_of_day_block, roof). Used as the denominator for normalized
# errors so that low-light blocks (dawn/dusk) cannot blow up the
# learned values. Slow alpha tracks seasonal drift (~2 week half-life).
REF_EMA_ALPHA = 0.05
# Floor for the normalization denominator (kWh per 15-min block).
# Errors are divided by max(reference_kwh, REFERENCE_FLOOR_KWH).
REFERENCE_FLOOR_KWH = 0.05
# Normalized errors (and correlations) are only learned once the
# reference for that (time_of_day_block, roof) has this many samples.
# Absolute error learning is NOT gated - it needs no reference.
REFERENCE_MIN_SAMPLES = 3

# Combined forecast: single east+west vector per issue time built from
# the raw forecasts plus the learned error model.
# Corrections are shrunk toward zero while a cell has few samples:
# applied = mean_abs_error * n / (n + COMBINED_SHRINK_K)
COMBINED_SHRINK_K = 5.0
# Denominator floor for correlation = cov / (sigma_east * sigma_west)
COMBINED_SIGMA_EPS = 1e-12
# Cross-roof correlation clamp. Immature cells (2 samples) hit |corr| = 1
# exactly as an EMA artifact; clamping keeps downstream covariance
# matrices non-singular. The clamped correlation is also used in the
# combined variance so the stored triple stays consistent.
COMBINED_CORR_CLAMP = 0.95

# ============================================================
# SHOWER BEHAVIOUR LEARNING (hourly)
# ============================================================
# A shower is a positive (off -> on) edge of this binary sensor.
# Edges within SHOWER_DEBOUNCE_MINUTES of a previous accepted edge
# are ignored (sensor chatter / re-triggering while showering).
# Environment overrides let tests run against an isolated database
# and a synthetic sensor.
SHOWER_SENSOR_ENTITY = os.environ.get(
    'SOLAR_FORECAST_SHOWER_ENTITY', 'binary_sensor.shower_bathroom_2nd')
SHOWER_SENSOR_NAME = os.environ.get(
    'SOLAR_FORECAST_SHOWER_NAME', 'bathroom_2nd')
SHOWER_EMA_ALPHA = 0.2        # EMA smoothing for the 168h probability vector
SHOWER_DEBOUNCE_MINUTES = 30  # ignore positive edges this soon after an accepted edge
SHOWER_VECTOR_HOURS = 168     # 7 days * 24 hours, indexed by UTC weekday*24 + hour
SHOWER_LAG_HOURS_MAX = 24     # var24: lags 1..24 hours in the correlation profile

# Log-odds nudge model (replaces the var24 lag correlation for prediction)
# logit(P_h) = logit(M_slot) + shrink(n_i) * nu_i  for each fired block
# Update on block firing: nu_i += alpha_eff * (y - P_h)  (combined-model gradient)
SHOWER_NUDGE_N0 = 8                    # alpha warm-up: alpha_base = max(1/(n+n0), alpha_min)
SHOWER_NUDGE_ALPHA_MIN = 0.015         # drift-tracking floor (half-life ~50 firings)
SHOWER_NUDGE_ALPHA_MAX = 0.3           # step size ceiling
SHOWER_NUDGE_BOOTSTRAP_ALPHA_MAX = 0.05  # reduced ceiling for history replay
SHOWER_NUDGE_K_SHRINK = 15             # prediction shrinkage: applied = nu * n/(n+k)
SHOWER_NUDGE_SUM_CLIP = 6.0           # clip applied nudge sum to +-this
SHOWER_NUDGE_MEAN_CLIP_LOW = 0.005     # clip M_slot before logit
SHOWER_NUDGE_MEAN_CLIP_HIGH = 0.995
SHOWER_NUDGE_P_CLIP_LOW = 0.001        # clip final P
SHOWER_NUDGE_P_CLIP_HIGH = 0.999
MATRIX_TYPE_SHOWER_NUDGE = 'shower_nudge_v1'
TABLE_VAR_MATRICES = 'model.var_matrices'

# ============================================================
# PRICE FORECAST (NL day-ahead wholesale / APX)
# ============================================================
# External pipeline: github.com/pefijnvandraat/electricitypriceforecast
# (MIT), cloned to PRICE_FORECAST_REPO and imported as the `forecaster`
# package. Daily batch model trained on public data (Energy-Charts
# prices, Open-Meteo weather, TTF gas, CO2). Its state (out-of-sample
# prediction log) is written to PRICE_FORECAST_STATE_DIR so the repo
# checkout stays pristine and git-pullable.
PRICE_FORECAST_REPO = os.environ.get(
    'PRICE_FORECAST_REPO', '/home/gijs/electricitypriceforecast')
PRICE_FORECAST_STATE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'price_state')
PRICE_FORECAST_ZONE = 'NL'
PRICE_FORECAST_HOURS = 192          # 8 days x 24h (covers the 168h MC window)
# z-score spanning the p10..p90 interval (80% central mass):
# variance = ((p90 - p10) / (2 * z))^2
PRICE_BAND_Z = 1.2816
# Minimum variance per hour ((EUR/kWh)^2) so downstream samplers
# never see a degenerate 0 for unpublished hours
PRICE_VAR_MIN = 1e-6

# VAR(1) residual correlation learning (price_var1_v1 snapshot).
# Residual r = actual - logged p50, in wholesale EUR/kWh.
#   r[h] = d_day + phi[h] * (r[h-1] - d_day) + eps[h],  eps ~ N(0, sigma_innov[h])
#   d_day ~ AR(1) across days with lag-1 correlation day_lag1_corr
# phi and correlations are shrunk by n / (n + SHRINK_K) while thin.
MATRIX_TYPE_PRICE_VAR1 = 'price_var1_v1'
PRICE_VAR1_MIN_DAYS = 20        # days of residuals before day-factor is trusted
PRICE_VAR1_MIN_PAIRS = 20       # pairs before a per-hour phi is trusted
PRICE_VAR1_SHRINK_K = 30.0

TABLE_PRICE_FORECAST = 'price_forecast'

# Rolling 15-minute publisher: every 15 minutes, publish the coming
# 168 hours (672 x 15-min blocks, matching the solar 672-block
# convention) as one vector. Blocks whose day-ahead price is already
# settled are the REAL 15-minute auction prices (Energy-Charts serves
# native quarterly settlement data, EUR/MWh -> EUR/kWh). Unsettled
# blocks take the hourly daily forecast, each hourly value repeated
# 4 times (price assumed constant within the hour).
PRICE_PUBLISHER_BLOCKS = 672          # 168h x 4 quarters
TABLE_PRICE_FORECAST_15MIN = 'price_forecast_15min'
PRICE_PUBLISHER_RETENTION_DAYS = 30   # prune published rows older than this
ENERGY_CHARTS_PRICE_URL = 'https://api.energy-charts.info/price'

# Daily forecast run gate: the external pipeline runs once per day
# only after the day-ahead auction has settled (~12:40 local), so it
# is scheduled at 11:15 AND 12:15 UTC and gated on local time here
# (13:00 Europe/Amsterdam = 11:00 UTC summer / 12:00 UTC winter).
PRICE_DAILY_RUN_TZ = 'Europe/Amsterdam'
PRICE_DAILY_RUN_LOCAL_HOUR = 13

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
TABLE_REFERENCE_MAGNITUDE = 'solar_reference_magnitude'
TABLE_COMBINED_FORECAST = 'solar_combined_forecast'

# Shower learning tables
TABLE_SHOWER_EVENTS = 'shower_events'
TABLE_SHOWER_PROBABILITY = 'shower_probability_168h'
TABLE_SHOWER_CORRELATION_LAG = 'shower_correlation_lag'

# ============================================================
# FILE PATHS
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(PROJECT_ROOT, 'db', 'schema.sql')
