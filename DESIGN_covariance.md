# Covariance Learning Design Document

## Status: IMPLEMENTED

**Decision**: Use Fibonacci horizons for both EMA learning and temporal correlation learning.
Temporal correlation computed in the same loop as EMA learning.

---

## Overview

We learn **forecast ERROR**, not the forecast itself. The external API provides forecasts; 
we learn how wrong they are and how those errors correlate across time.

---

## What We Are Learning

### Core Data Flow (Every 15 Minutes)

```
1. Store actual production (15-min kWh) in solar_actual_production
2. For each Fibonacci horizon H:
   a. Compute error_H = (actual - forecast_H) / forecast_H
   b. Update EMA: mean_error, variance for (time_of_day_block, H, roof)
   c. Update LOCAL correlation: error_H vs avg_error ±2h
   d. Update DAILY correlation: error_H vs avg_error ±2h from 24h ago
```

---

## Implementation Details

### Constants

```python
FIBONACCI_HORIZONS_BLOCKS = [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672]
ALPHA = 0.1
LOCAL_WINDOW_BLOCKS = [-8, -7, -6, -5, -4, -3, -2, -1, 0]  # 9 blocks = 2h15m
DAILY_OFFSET_BLOCKS = 96  # 24 hours
```

### Pseudo-Code (MATLAB Style)

```matlab
% ============================================================
% EVERY 15 MINUTES AT TIME T (UTC)
% ============================================================

FIB_HORIZONS = [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672];
ALPHA = 0.1;
LOCAL_WINDOW = -8:0;        % Past 8 blocks + current
DAILY_OFFSET = 96;          % 24 hours = 96 blocks

tod = get_block_in_day(T);  % 0-95

store_actual(T, east, actual_east);
store_actual(T, west, actual_west);

for H = FIB_HORIZONS
    % --- 1. GET FORECAST AND ACTUAL ---
    forecast_T = get_forecast(T, east, H);
    actual_T = get_actual(T, east);
    
    % --- 2. COMPUTE ERROR (RELATIVE) ---
    error_H = (actual_T - forecast_T) / forecast_T;
    
    % --- 3. UPDATE EMA (MEAN + VARIANCE) ---
    [old_mean, old_var, count] = db.get_accuracy(tod, H, east);
    new_mean = (1-ALPHA) * old_mean + ALPHA * error_H;
    new_var  = (1-ALPHA) * old_var  + ALPHA * (error_H - new_mean)^2;
    db.update_accuracy(tod, H, east, new_mean, new_var, count+1);
    
    % --- 4. LOCAL CORRELATION: +/- 2 HOURS ---
    local_errors = [];
    for offset = LOCAL_WINDOW
        t_local = T + offset;
        forecast_local = get_forecast(t_local, east, H);
        actual_local = get_actual(t_local, east);
        error_local = (actual_local - forecast_local) / forecast_local;
        local_errors = [local_errors, error_local];
    end
    avg_local = mean(local_errors);
    [old_corr, count] = db.get_local_correlation(tod, H, east);
    new_corr = (1-ALPHA) * old_corr + ALPHA * (error_H * avg_local);
    db.update_local_correlation(tod, H, east, new_corr, count+1);
    
    % --- 5. DAILY CORRELATION: SAME TIME 24H AGO +/- 2H ---
    daily_errors = [];
    for offset = LOCAL_WINDOW
        t_daily = T - DAILY_OFFSET + offset;
        forecast_daily = get_forecast(t_daily, east, H);
        actual_daily = get_actual(t_daily, east);
        error_daily = (actual_daily - forecast_daily) / forecast_daily;
        daily_errors = [daily_errors, error_daily];
    end
    avg_daily = mean(daily_errors);
    [old_corr, count] = db.get_daily_correlation(tod, H, east);
    new_corr = (1-ALPHA) * old_corr + ALPHA * (error_H * avg_daily);
    db.update_daily_correlation(tod, H, east, new_corr, count+1);
end
```

### Helper Functions

```matlab
function forecast = get_forecast(T, roof, H)
    % Forecast vector from (T - H) blocks ago
    % Block H in that vector is the forecast for time T
    forecast_vector = db.get_forecast_vector(T - H, roof);
    forecast = forecast_vector(H);
end

function actual = get_actual(T, roof)
    actual = db.get_actual(T, roof);
end

function tod = get_block_in_day(T)
    hour = T.hour;
    minute = T.minute;
    block_in_hour = floor(minute / 15);
    tod = hour * 4 + block_in_hour;  % 0-95
end
```

---

## Database Schema

### New Tables

```sql
-- ============================================================
-- ACTUAL PRODUCTION
-- 15-minute actual energy, UTC timestamped
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_actual_production (
    timestamp       TIMESTAMPTZ NOT NULL,
    roof            VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    energy_kwh      FLOAT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (timestamp, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_actual_production_timestamp 
    ON solar_actual_production(timestamp);

-- ============================================================
-- LOCAL CORRELATION
-- Correlation: error_H vs avg_error_local (2h window)
-- Key: (time_of_day_block, horizon, roof)
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_correlation_local (
    time_of_day_block INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    horizon           INTEGER NOT NULL,
    roof              VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    correlation       FLOAT NOT NULL,
    sample_count      INTEGER NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, horizon, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_local_tod 
    ON solar_correlation_local(time_of_day_block);

-- ============================================================
-- DAILY CORRELATION  
-- Correlation: error_H vs avg_error_daily (24h offset, 2h window)
-- Key: (time_of_day_block, horizon, roof)
-- ============================================================
CREATE TABLE IF NOT EXISTS solar_correlation_daily (
    time_of_day_block INTEGER NOT NULL CHECK (time_of_day_block >= 0 AND time_of_day_block < 96),
    horizon           INTEGER NOT NULL,
    roof              VARCHAR(10) NOT NULL CHECK (roof IN ('east', 'west')),
    correlation       FLOAT NOT NULL,
    sample_count      INTEGER NOT NULL DEFAULT 0,
    last_updated      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time_of_day_block, horizon, roof)
);

CREATE INDEX IF NOT EXISTS idx_solar_correlation_daily_tod 
    ON solar_correlation_daily(time_of_day_block);
```

---

## What This Learns

| Correlation Type | Captures | Example |
|-----------------|----------|---------|
| **EMA Mean/Var** | Typical error magnitude at each horizon | "My 3h forecasts are usually 10% too high" |
| **Local** | Are errors clustered in time? | "If I'm wrong now, I'm likely wrong in 2 hours" (cloud patterns) |
| **Daily** | Does today's error pattern repeat yesterday's? | "The model consistently overestimates mornings" (systematic bias) |

---

## Implementation Checklist

- [x] Document decision and pseudo-code
- [x] Add tables to `db/schema.sql`
- [x] Add config constants to `config.py`
- [x] Update `db/client.py` with new methods
- [x] Update `accuracy_learner.py` with full implementation
- [ ] Test with real data
- [ ] Verify writes to database

---

## Test Results

Expected output when running test:
```
[INFO] Starting pipeline at 2026-10-05 14:30:00+00:00
[INFO] Step 1/2: Building forecast vectors...
[INFO] Step 2/2: Updating accuracy model...
[INFO] Stored east vector: 672 blocks, 45.23 kWh
[INFO] Stored west vector: 672 blocks, 52.14 kWh
[INFO] Accuracy model updated: 12 cells (6 for east, 6 for west)
[INFO] Correlation model updated: 12 local + 12 daily = 24 cells
```
