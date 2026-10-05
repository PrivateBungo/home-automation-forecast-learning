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
    mean_error           FLOAT NOT NULL,
    variance             FLOAT NOT NULL,
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
COMMENT ON COLUMN solar_accuracy.mean_error IS 'Mean relative error: (actual - forecast) / forecast';
COMMENT ON COLUMN solar_accuracy.variance IS 'Variance of relative errors';
COMMENT ON COLUMN solar_covariance.covariance IS 'Covariance between east and west relative errors';
COMMENT ON COLUMN solar_covariance.correlation IS 'Correlation coefficient between east and west (covariance / (sigma_east * sigma_west))';

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

