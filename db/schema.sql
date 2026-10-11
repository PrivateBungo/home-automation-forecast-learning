-- Solar Forecast System Database Schema
-- =====================================
-- All values in kWh, all times in UTC
-- Uses Fibonacci horizons for computational efficiency

-- ============================================================
-- FORECAST VECTORS
-- Stores raw 15-minute forecast vectors for each roof
-- 672 elements = 7 days * 96 fifteen-minute blocks per day
-- Timestamps are always in UTC
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_forecast_vectors (
    timestamp       TIMESTAMPTZ NOT NULL,  -- UTC timestamp when forecast was generated
    roof            VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    forecast_vector FLOAT[] NOT NULL,     -- 672 kWh values (15-min blocks)
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (timestamp, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_forecast_vectors_timestamp 
    ON solar_forecast_vectors(timestamp);

CREATE INDEX IF NOT EXISTS idx_solar_forecast_vectors_roof 
    ON solar_forecast_vectors(roof);

-- ============================================================
-- ACCURACY MODEL
-- Mean error and variance per (time_of_day_block, forecast_horizon_block, roof)
-- Uses Fibonacci horizons only: [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672] blocks
-- time_of_day_block: 0-95 (96 fifteen-minute blocks per day)
-- forecast_horizon_block: Fibonacci values only (not all 672)
-- roof: east, west
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_accuracy (
    time_of_day_block     INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    forecast_horizon_block INTEGER NOT NULL,  -- Fibonacci horizon in blocks
    roof                 VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    mean_error           FLOAT NOT NULL,      -- EMA of normalized error: (actual - forecast) / max(reference, floor)
    mean_abs_error       FLOAT NOT NULL DEFAULT 0,  -- EMA of absolute error in kWh: actual - forecast
    variance             FLOAT NOT NULL,      -- EMA variance of ABSOLUTE errors (kWh^2)
    sample_count         INTEGER NOT NULL DEFAULT 0,
    last_updated         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, forecast_horizon_block, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_accuracy_tod 
    ON solar_accuracy(time_of_day_block);

CREATE INDEX IF NOT EXISTS idx_solar_accuracy_horizon 
    ON solar_accuracy(forecast_horizon_block);

CREATE INDEX IF NOT EXISTS idx_solar_accuracy_roof 
    ON solar_accuracy(roof);

-- ============================================================
-- COVARIANCE MODEL
-- Cross-roof covariance per (time_of_day_block, forecast_horizon_block)
-- Uses Fibonacci horizons only
-- Captures correlation between east and west forecast errors
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_covariance (
    time_of_day_block     INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    forecast_horizon_block INTEGER NOT NULL,  -- Fibonacci horizon in blocks
    covariance            FLOAT NOT NULL,
    correlation          FLOAT NOT NULL,
    sample_count         INTEGER NOT NULL DEFAULT 0,
    last_updated         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, forecast_horizon_block)
);

CREATE INDEX IF NOT EXISTS idx_solar_covariance_tod 
    ON solar_covariance(time_of_day_block);

CREATE INDEX IF NOT EXISTS idx_solar_covariance_horizon 
    ON solar_covariance(forecast_horizon_block);

-- ============================================================
-- FIBONACCI HORIZONS REFERENCE
-- ============================================================

-- Reference table for Fibonacci horizons (in hours and blocks)
CREATE TABLE IF NOT EXISTS fibonacci_horizons (
    hours       INTEGER NOT NULL PRIMARY KEY,
    blocks      INTEGER NOT NULL,
    description VARCHAR(50)
);

-- Populate if empty
INSERT INTO fibonacci_horizons (hours, blocks, description)
VALUES 
    (1, 4, '1 hour'),
    (2, 8, '2 hours'),
    (3, 12, '3 hours'),
    (5, 20, '5 hours'),
    (8, 32, '8 hours'),
    (13, 52, '13 hours'),
    (21, 84, '21 hours'),
    (34, 136, '34 hours'),
    (55, 220, '55 hours'),
    (89, 356, '89 hours'),
    (144, 576, '144 hours'),
    (168, 672, '168 hours = 7 days')
ON CONFLICT DO NOTHING;

-- ============================================================
-- VIEWS
-- ============================================================

-- View for getting full accuracy matrix for a roof
CREATE OR REPLACE VIEW vw_accuracy_matrix AS
SELECT 
    time_of_day_block,
    forecast_horizon_block,
    roof,
    mean_error,
    variance,
    sample_count,
    last_updated
FROM solar_accuracy
ORDER BY time_of_day_block, forecast_horizon_block, roof;

-- View for getting covariance matrix
CREATE OR REPLACE VIEW vw_covariance_matrix AS
SELECT 
    time_of_day_block,
    forecast_horizon_block,
    covariance,
    correlation,
    sample_count,
    last_updated
FROM solar_covariance
ORDER BY time_of_day_block, forecast_horizon_block;

-- Combined view: all accuracy data with covariance
-- (dropped first so re-runs on a DB with the extended view don't fail:
--  CREATE OR REPLACE VIEW cannot drop columns)
DROP VIEW IF EXISTS vw_full_accuracy;
CREATE OR REPLACE VIEW vw_full_accuracy AS
SELECT 
    a.time_of_day_block,
    a.forecast_horizon_block,
    a.roof,
    a.mean_error,
    a.variance,
    a.sample_count as accuracy_count,
    c.covariance,
    c.correlation,
    c.sample_count as covariance_count
FROM solar_accuracy a
LEFT JOIN solar_covariance c 
    ON a.time_of_day_block = c.time_of_day_block 
    AND a.forecast_horizon_block = c.forecast_horizon_block
ORDER BY a.time_of_day_block, a.forecast_horizon_block, a.roof;

-- ============================================================
-- COMMENTS
-- ============================================================

COMMENT ON TABLE solar_forecast_vectors IS 'Raw 15-minute forecast vectors (kWh) for east and west roofs, 672 elements each. Timestamps are UTC.';
COMMENT ON TABLE solar_accuracy IS 'Mean error and variance of forecast errors per time-of-day block and Fibonacci forecast horizon block';
COMMENT ON TABLE solar_covariance IS 'Covariance between east and west forecast errors per time-of-day block and Fibonacci forecast horizon block';

COMMENT ON COLUMN solar_forecast_vectors.forecast_vector IS 'Array of 672 kWh values (15-minute blocks)';
COMMENT ON COLUMN solar_forecast_vectors.timestamp IS 'UTC timestamp when forecast was generated';
COMMENT ON COLUMN solar_accuracy.mean_error IS 'EMA of normalized error: (actual - forecast) / max(reference_kwh, 0.05). Dimensionless, comparable across blocks';
COMMENT ON COLUMN solar_accuracy.mean_abs_error IS 'EMA of absolute forecast error in kWh: actual - forecast. Use for forecast corrections';
COMMENT ON COLUMN solar_accuracy.variance IS 'EMA variance of ABSOLUTE forecast errors (kWh^2). Use for uncertainty quantification';
COMMENT ON COLUMN solar_covariance.covariance IS 'Covariance between east and west absolute errors (kWh^2)';
COMMENT ON COLUMN solar_covariance.correlation IS 'Correlation coefficient between east and west (covariance / (sigma_east * sigma_west))';

-- ============================================================
-- REFERENCE MAGNITUDE
-- Slow EMA of actual production per (time_of_day_block, roof).
-- Typical production at this time of day; used as the denominator
-- for normalized errors so low-light blocks cannot blow up learning.
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_reference_magnitude (
    time_of_day_block INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    roof              VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    reference_kwh     FLOAT NOT NULL,
    sample_count      INTEGER NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_reference_magnitude_roof
    ON solar_reference_magnitude(roof);

COMMENT ON TABLE solar_reference_magnitude IS 'Reference magnitude per (time_of_day_block, roof): slow EMA of actual production used to normalize errors';
COMMENT ON COLUMN solar_reference_magnitude.reference_kwh IS 'Slow EMA of actual production (kWh per 15-min block) for this time-of-day block and roof';

-- ============================================================
-- COMBINED FORECAST
-- Single east+west forecast per issue time: corrected mean,
-- variance and cross-roof error correlation, 672 blocks each.
-- Built from the raw forecast vectors plus the learned error
-- model (solar_accuracy + solar_covariance).
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_combined_forecast (
    issued_at     TIMESTAMPTZ NOT NULL,  -- UTC timestamp of the input forecast vectors
    mean_kwh      FLOAT[] NOT NULL,      -- 672 corrected combined mean (kWh per 15-min block)
    variance_kwh2 FLOAT[] NOT NULL,      -- 672 combined variance (kWh^2 per block)
    corr_ew       FLOAT[] NOT NULL,      -- 672 cross-roof error correlations (dimensionless)
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (issued_at)
);

CREATE INDEX IF NOT EXISTS idx_solar_combined_forecast_issued
    ON solar_combined_forecast(issued_at);

COMMENT ON TABLE solar_combined_forecast IS 'Combined east+west forecast per issue time: corrected mean, variance and cross-roof error correlation (672 blocks each)';
COMMENT ON COLUMN solar_combined_forecast.mean_kwh IS 'Corrected combined mean per 15-min block (kWh): raw east+west forecast plus shrunk learned error corrections';
COMMENT ON COLUMN solar_combined_forecast.variance_kwh2 IS 'Combined variance per block (kWh^2): var_east + var_west + 2*covariance_east_west, interpolated over Fibonacci knots';
COMMENT ON COLUMN solar_combined_forecast.corr_ew IS 'Cross-roof error correlation per block: covariance / (sigma_east * sigma_west); 0 where either variance is unknown';

-- ============================================================
-- ACTUAL PRODUCTION
-- 15-minute actual energy production, UTC timestamped
-- Used for computing forecast errors
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_actual_production (
    timestamp       TIMESTAMPTZ NOT NULL,  -- UTC timestamp of the 15-min block
    roof            VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    energy_kwh      FLOAT NOT NULL,       -- Actual energy in kWh for this 15-min block
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (timestamp, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_actual_production_timestamp 
    ON solar_actual_production(timestamp);

CREATE INDEX IF NOT EXISTS idx_solar_actual_production_roof 
    ON solar_actual_production(roof);

-- ============================================================
-- LOCAL CORRELATION
-- Correlation between forecast error at current block and average error
-- in local window (past 2 hours, 8 blocks back + current = 9 blocks)
-- Key: (time_of_day_block, horizon, roof)
-- Value: learned correlation coefficient
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_correlation_local (
    time_of_day_block INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    forecast_horizon_block INTEGER NOT NULL,  -- Fibonacci horizon in blocks
    roof              VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    correlation       FLOAT NOT NULL,
    sample_count      INTEGER NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, forecast_horizon_block, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_local_tod 
    ON solar_correlation_local(time_of_day_block);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_local_horizon 
    ON solar_correlation_local(forecast_horizon_block);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_local_roof 
    ON solar_correlation_local(roof);

-- ============================================================
-- DAILY CORRELATION
-- Correlation between forecast error at current block and average error
-- in daily offset window (same time 24h ago, 2h window)
-- Key: (time_of_day_block, horizon, roof)
-- Value: learned correlation coefficient
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_correlation_daily (
    time_of_day_block INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    forecast_horizon_block INTEGER NOT NULL,  -- Fibonacci horizon in blocks
    roof              VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    correlation       FLOAT NOT NULL,
    sample_count      INTEGER NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, forecast_horizon_block, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_daily_tod 
    ON solar_correlation_daily(time_of_day_block);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_daily_horizon 
    ON solar_correlation_daily(forecast_horizon_block);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_daily_roof 
    ON solar_correlation_daily(roof);

-- ============================================================
-- VIEWS FOR CORRELATION TABLES
-- ============================================================

-- View for local correlations
CREATE OR REPLACE VIEW vw_correlation_local AS
SELECT 
    time_of_day_block,
    forecast_horizon_block,
    roof,
    correlation,
    sample_count,
    last_updated
FROM solar_correlation_local
ORDER BY time_of_day_block, forecast_horizon_block, roof;

-- View for daily correlations
CREATE OR REPLACE VIEW vw_correlation_daily AS
SELECT 
    time_of_day_block,
    forecast_horizon_block,
    roof,
    correlation,
    sample_count,
    last_updated
FROM solar_correlation_daily
ORDER BY time_of_day_block, forecast_horizon_block, roof;

-- Combined view: all accuracy data with all correlations
DROP VIEW IF EXISTS vw_full_accuracy;
CREATE OR REPLACE VIEW vw_full_accuracy AS
SELECT 
    a.time_of_day_block,
    a.forecast_horizon_block,
    a.roof,
    a.mean_error,
    a.variance,
    a.sample_count as accuracy_count,
    c.covariance,
    c.correlation as cov_correlation,
    c.sample_count as covariance_count,
    cl.correlation as local_correlation,
    cl.sample_count as local_count,
    cd.correlation as daily_correlation,
    cd.sample_count as daily_count
FROM solar_accuracy a
LEFT JOIN solar_covariance c 
    ON a.time_of_day_block = c.time_of_day_block 
    AND a.forecast_horizon_block = c.forecast_horizon_block
LEFT JOIN solar_correlation_local cl
    ON a.time_of_day_block = cl.time_of_day_block
    AND a.forecast_horizon_block = cl.forecast_horizon_block
    AND a.roof = cl.roof
LEFT JOIN solar_correlation_daily cd
    ON a.time_of_day_block = cd.time_of_day_block
    AND a.forecast_horizon_block = cd.forecast_horizon_block
    AND a.roof = cd.roof
ORDER BY a.time_of_day_block, a.forecast_horizon_block, a.roof;

-- ============================================================
-- COMMENTS FOR NEW TABLES
-- ============================================================

COMMENT ON TABLE solar_actual_production IS 'Actual 15-minute energy production (kWh) for east and west roofs. UTC timestamps.';
COMMENT ON TABLE solar_correlation_local IS 'Local temporal correlation: error vs avg error in past 2h window';
COMMENT ON TABLE solar_correlation_daily IS 'Daily temporal correlation: error vs avg error at same time 24h ago';
COMMENT ON COLUMN solar_actual_production.energy_kwh IS 'Actual energy in kWh for the 15-minute block';
COMMENT ON COLUMN solar_correlation_local.correlation IS 'Correlation: current error vs avg error in local window (past 2h)';
COMMENT ON COLUMN solar_correlation_daily.correlation IS 'Correlation: current error vs avg error at same time 24h ago';


-- ============================================================
-- SHOWER BEHAVIOUR LEARNING
-- Hourly learning loop for binary_sensor.shower_bathroom_2nd.
-- All times UTC. Vector index: weekday*24 + hour (UTC), 0..167.
-- ============================================================

-- Debounced shower events (accepted positive off->on edges)
CREATE TABLE IF NOT EXISTS shower_events (
    timestamp       TIMESTAMPTZ NOT NULL,  -- UTC time of the accepted positive edge
    sensor          VARCHAR(50) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (timestamp, sensor)
);

CREATE INDEX IF NOT EXISTS idx_shower_events_sensor 
    ON shower_events(sensor);

-- 168-hour shower probability vector (EMA learned)
-- p_shower_168h[i] = P(shower in hour-of-week i), i = weekday*24 + hour (UTC)
CREATE TABLE IF NOT EXISTS shower_probability_168h (
    sensor            VARCHAR(50) NOT NULL PRIMARY KEY,
    p_shower_168h     FLOAT[] NOT NULL,     -- 168 hourly probabilities
    mean_x           FLOAT NOT NULL,        -- EMA of X (global shower frequency)
    sample_count     INTEGER NOT NULL DEFAULT 0,  -- hours learned
    last_x           INTEGER,              -- X of the most recent learned hour
    updated_hour_index INTEGER,            -- vector slot of the most recent update
    last_updated_hour TIMESTAMPTZ,          -- start of the most recent learned hour (UTC)
    last_updated     TIMESTAMPTZ DEFAULT NOW()
);

-- var24 lag correlation profile
-- For lag k: correlation between X(h) and X(h-k), computed from
-- EMA of the product (mean_xy) and EMA of X (mean_x in shower_probability_168h).
-- Binary variables: corr = (mean_xy - mean_x^2) / (mean_x * (1 - mean_x))
CREATE TABLE IF NOT EXISTS shower_correlation_lag (
    sensor          VARCHAR(50) NOT NULL,
    lag_hours       INTEGER NOT NULL CHECK (lag_hours >= 1 AND lag_hours <= 24),
    mean_xy         FLOAT NOT NULL,     -- EMA of X(h) * X(h - lag)
    correlation     FLOAT NOT NULL,     -- Pearson/phi correlation for binary variables
    sample_count    INTEGER NOT NULL DEFAULT 0,
    last_updated    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (sensor, lag_hours)
);

CREATE INDEX IF NOT EXISTS idx_shower_correlation_lag_lag 
    ON shower_correlation_lag(lag_hours);

-- Views for shower learning data
CREATE OR REPLACE VIEW vw_shower_probability AS
SELECT
    sensor,
    p_shower_168h,
    mean_x,
    sample_count,
    last_x,
    updated_hour_index,
    last_updated_hour,
    last_updated
FROM shower_probability_168h
ORDER BY sensor;

CREATE OR REPLACE VIEW vw_shower_correlation_lag AS
SELECT
    sensor,
    lag_hours,
    mean_xy,
    correlation,
    sample_count,
    last_updated
FROM shower_correlation_lag
ORDER BY sensor, lag_hours;

COMMENT ON TABLE shower_events IS 'Debounced shower events: accepted positive (off->on) edges of the shower binary sensor, 30-minute debounce. UTC timestamps.';
COMMENT ON TABLE shower_probability_168h IS '168-hour shower probability vector, EMA learned hourly. Index = UTC weekday*24 + hour.';
COMMENT ON TABLE shower_correlation_lag IS 'var24 lag correlation profile: correlation between shower X at hour h and X at hour h-lag, for lag 1..24 hours.';
COMMENT ON COLUMN shower_probability_168h.mean_x IS 'EMA of X (global hourly shower frequency), used by the lag correlation computation';
COMMENT ON COLUMN shower_correlation_lag.correlation IS 'Correlation (mean_xy - mean_x^2) / (mean_x * (1 - mean_x)); 0 when mean is degenerate';

-- ============================================================
-- MODEL VARIATIONAL MATRICES (append-only model snapshots)
-- Each hourly learning run appends the current nudge model state.
-- matrix_type 'shower_nudge_v1': log-odds nudge model for shower
-- prediction (blocks 0_3h, 6_18h, 0_30h_neg), see processing/nudge_model.py
-- ============================================================
CREATE SCHEMA IF NOT EXISTS model;

CREATE TABLE IF NOT EXISTS model.var_matrices (
    matrix_type     VARCHAR(50) NOT NULL,
    calculated_at   TIMESTAMPTZ NOT NULL,  -- UTC time the snapshot was written
    target_hour     TIMESTAMPTZ NOT NULL,  -- UTC start of the last learned hour
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (matrix_type, calculated_at, target_hour)
);

CREATE INDEX IF NOT EXISTS idx_var_matrices_type_time 
    ON model.var_matrices(matrix_type, calculated_at DESC);

COMMENT ON TABLE model.var_matrices IS 'Append-only model state snapshots. shower_nudge_v1: log-odds nudge model (nu per block with n/last_fired_at metadata, config, means_ref). price_var1_v1: VAR(1) residual correlation model of the NL day-ahead price forecast (phi/sigma per hour, day factor).';
COMMENT ON COLUMN model.var_matrices.payload IS 'JSON: {means_ref, nudges: {block: {value, n, last_fired_at, first_fired_at}}, config: {n0, alpha_min, alpha_max, k_shrink}}';

-- ============================================================
-- PRICE FORECAST (NL day-ahead wholesale / APX)
-- Daily snapshot from the external electricitypriceforecast pipeline
-- (Energy-Charts prices, Open-Meteo weather, TTF gas, CO2).
-- Hourly resolution, wholesale EUR/kWh, all times UTC.
-- ============================================================
CREATE TABLE IF NOT EXISTS price_forecast (
    issued_at           TIMESTAMPTZ NOT NULL,   -- UTC run time (hour floor)
    start_utc           TIMESTAMPTZ NOT NULL,   -- UTC target hour of mean[0]
    horizon_hours       INTEGER NOT NULL,       -- array length (192 = 8 days)
    mean_eur_kwh        FLOAT8[] NOT NULL,       -- wholesale EUR/kWh per hour
    variance_eur_kwh2   FLOAT8[] NOT NULL,       -- (EUR/kWh)^2 per hour
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (issued_at)
);

CREATE INDEX IF NOT EXISTS idx_price_forecast_issued 
    ON price_forecast(issued_at);

COMMENT ON TABLE price_forecast IS 'Daily NL day-ahead wholesale price forecast. mean = conformal p50, variance = ((p90 - p10) / (2 * 1.2816))^2 from the calibrated out-of-sample band; grows with lead time.';
COMMENT ON COLUMN price_forecast.variance_eur_kwh2 IS 'Per-hour variance (EUR/kWh)^2. 0 for hours whose day-ahead price is already published (mean = published price).';

-- ============================================================
-- PRICE FORECAST, ROLLING 15-MINUTE (published every 15 minutes)
-- Coming 168 hours as 672 x 15-min blocks, matching the solar
-- 672-block convention. Settled blocks = real auction prices
-- (variance 0); unsettled blocks = daily hourly forecast, each
-- hourly mean/variance repeated for its 4 quarters.
-- ============================================================
CREATE TABLE IF NOT EXISTS price_forecast_15min (
    issued_at           TIMESTAMPTZ NOT NULL,   -- UTC publish time (15-min floor)
    start_utc           TIMESTAMPTZ NOT NULL,   -- UTC start of block[0]
    horizon_blocks      INTEGER NOT NULL,       -- array length (672)
    mean_eur_kwh        FLOAT8[] NOT NULL,       -- wholesale EUR/kWh per 15-min block
    variance_eur_kwh2   FLOAT8[] NOT NULL,       -- (EUR/kWh)^2 per block; 0 for settled blocks
    published_blocks    INTEGER NOT NULL,       -- blocks with real auction data
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (issued_at)
);

CREATE INDEX IF NOT EXISTS idx_price_forecast_15min_issued 
    ON price_forecast_15min(issued_at);

COMMENT ON TABLE price_forecast_15min IS 'Rolling 168h NL wholesale price vector at 15-min resolution, republished every 15 min. Settled blocks are real EPEX/Energy-Charts 15-min auction prices; beyond settlement the daily hourly forecast is repeated 4x per hour.';
