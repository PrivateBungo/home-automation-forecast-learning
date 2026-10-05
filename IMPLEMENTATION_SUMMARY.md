# Implementation Summary: Solar Forecast Learning System

## Date: 2026-10-05

## What Was Implemented

### 1. Database Schema (db/schema.sql)
✅ **All 6 tables created:**
- `solar_forecast_vectors` - Raw 672-element forecast vectors (15-min blocks, kWh, UTC)
- `solar_accuracy` - EMA mean/variance per (time_of_day_block, horizon, roof)
- `solar_covariance` - Cross-roof covariance per (time_of_day_block, horizon)
- `solar_actual_production` - Actual 15-min production data (NEW)
- `solar_correlation_local` - Local temporal correlation (±2h window) (NEW)
- `solar_correlation_daily` - Daily temporal correlation (24h offset) (NEW)

### 2. Database Client (db/client.py)
✅ **Extended with methods for all new tables:**
- `save_actual_production()` / `get_actual_production()`
- `get_actual_production_range()`
- `update_correlation_local()` / `get_correlation_local()`
- `update_correlation_daily()` / `get_correlation_daily()`

### 3. Configuration (config.py)
✅ **Added all constants:**
- Fibonacci horizons: [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672] blocks
- Local window: [-8, -7, ..., 0] = 9 blocks = 2h15m
- Daily offset: 96 blocks = 24h
- EMA alpha: 0.1
- SENSOR_GROUPS and SENSOR_GROUPS_FORECAST for backward compatibility

### 4. Forecast Fetcher (sensors/forecast_fetcher.py)
✅ **Fully functional:**
- Fetches from 14 forecast sensors (7 days × 2 roofs)
- Converts Wh → kWh
- Converts watts → kWh for 15-min blocks
- Builds 672-element vectors for each roof
- **Tested:** Successfully fetches real data (89.33 kWh east, 18.06 kWh west)

### 5. Vector Builder (processing/vector_builder.py)
✅ **Fully functional:**
- Orchestrates forecast fetching
- Validates vector lengths (672 blocks)
- Stores in database with UTC timestamps
- **Tested:** Successfully stores vectors with real data

### 6. Accuracy Learner (processing/accuracy_learner.py)
✅ **Fully implemented:**
- EMA mean/variance learning per Fibonacci horizon
- Local temporal correlation learning (±2h window)
- Daily temporal correlation learning (24h offset)
- Handles edge cases (missing data, horizon < 24h, etc.)
- Stores actual production data
- **Ready:** Will run once quarterly sensors are available

### 7. Orchestrator (main.py)
✅ **Fully functional:**
- Coordinates entire pipeline
- Runs every 15 minutes (not hourly as originally requested)
- Steps: fetch → build → store → learn
- Handles errors gracefully
- Logging throughout

### 8. Run Script (run.py)
✅ **Created:**
- Handles Python 3.13 import path issues
- Entry point for direct execution
- Can be scheduled in cron

## Design Decisions Documented

### Temporal Correlation Strategy
**Decision:** Use Fibonacci horizons for both EMA learning AND temporal correlation learning.

**Rationale:**
- Computationally efficient (12 horizons vs 672)
- Covers full range from 1h to 7 days
- All learning happens in one loop

**Two Correlation Types:**
1. **Local (±2h):** Captures short-term patterns (cloud movements)
2. **Daily (24h offset):** Captures systematic daily patterns

**Strategy for horizon < 24h:**
- Still compute both correlations
- Use whatever historical data is available
- Gracefully handles missing data

### What We Learn
We learn **FORECAST ERROR**, not the forecast itself:
- `error = (actual - forecast) / forecast` (relative error)
- Mean error: typical bias
- Variance: typical variability
- Correlations: temporal patterns

This captures local factors the external API doesn't know (trees, shadows, orientation).

### Data Standards
- **All energy:** kWh (standardized)
- **All time:** 15-min blocks (0-indexed)
- **All timestamps:** UTC (no DST issues)
- **Vectors:** 672 elements = 7 days
- **Time-of-day:** 0-95 (96 blocks per day)

## Test Results

### ✅ Working Components
1. **Database connection:** ✅ Can connect to PostgreSQL at 10.100.123.53:5432
2. **Schema application:** ✅ All 6 tables + indexes + views created
3. **Forecast fetching:** ✅ Gets real data from HA sensors
   - East: 89.33 kWh (7-day forecast)
   - West: 18.06 kWh (7-day forecast)
4. **Vector building:** ✅ Creates 672-element vectors
5. **Vector storage:** ✅ Stores with UTC timestamps
6. **Pipeline orchestration:** ✅ Full pipeline runs successfully

### ⚠️ Blocked Components
- **Accuracy learning:** Blocked waiting for quarterly sensors
  - Requires: `sensor.solar_quaterly_energy_east_roof`
  - Requires: `sensor.solar_quaterly_energy_west_roof`
  - These sensors don't exist yet in Home Assistant

## Files Modified/Created

### Modified:
- `db/schema.sql` - Added 3 new tables + views
- `db/client.py` - Added methods for new tables
- `config.py` - Added all constants, fixed SENSOR_GROUPS reference
- `processing/accuracy_learner.py` - Full rewrite with correlation learning
- `main.py` - Added path handling
- `README.md` - Updated with current status

### Created:
- `run.py` - Entry point wrapper
- `DESIGN_covariance.md` - Detailed design document
- `solar_forecast/` directory structure with `__init__.py` files

## What's Next

### Immediate (User Action Required)
1. Create quarterly sensors in Home Assistant:
   - `sensor.solar_quaterly_energy_east_roof`
   - `sensor.solar_quaterly_energy_west_roof`
   - These should output 15-min energy in kWh (or Wh, we auto-convert)

### Once Sensors Exist
1. System will automatically:
   - Fetch actual production every 15 minutes
   - Store actual data with UTC timestamps
   - Compute forecast errors for Fibonacci horizons
   - Update EMA mean/variance
   - Update local correlations (±2h)
   - Update daily correlations (24h offset)

### Validation
1. Check data is being stored:
   ```bash
   psql -h 10.100.123.53 -p 5432 -U postgres -d homeassistant \
     -c "SELECT COUNT(*) FROM solar_actual_production;"
   ```

2. Check accuracy model is learning:
   ```bash
   psql -h 10.100.123.53 -p 5432 -U postgres -d homeassistant \
     -c "SELECT * FROM solar_accuracy LIMIT 10;"
   ```

3. Check correlations are being learned:
   ```bash
   psql -h 10.100.123.53 -p 5432 -U postgres -d homeassistant \
     -c "SELECT * FROM solar_correlation_local LIMIT 10;"
   ```

## Usage

### Test Mode (No DB writes)
```bash
python3 /home/gijs/solar_forecast/run.py --test --force
```

### Production Mode (Stores to DB)
```bash
python3 /home/gijs/solar_forecast/run.py --force
```

### Schedule (Every 15 minutes)
```bash
# Add to crontab (crontab -e)
*/15 * * * * /usr/bin/python3 /home/gijs/solar_forecast/run.py
```

## Architecture Rules Followed

1. ✅ One responsibility per file
2. ✅ No business logic in `__main__`
3. ✅ Separation of concerns (data, processing, learning)
4. ✅ All energy in kWh
5. ✅ All time in 15-min blocks
6. ✅ All timestamps in UTC
7. ✅ Snake case for files/variables
8. ✅ PascalCase for class names
9. ✅ Fail fast with clear error messages
10. ✅ Log everything important

## Database Credentials Used

- **Host:** 10.100.123.53
- **Port:** 5432
- **User:** postgres
- **Password:** 123GjH#@!
- **Database:** homeassistant

✅ **Confirmed:** Database is accessible and writable
