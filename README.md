# Solar Forecast Learning System

## Overview

A Python-based system that:
1. **Ingests** 15-minute resolution solar forecasts from Home Assistant PostgreSQL database
2. **Builds** clean forecast vectors (672 blocks = 7 days × 96 fifteen-minute blocks/day)
3. **Learns** accuracy models (mean error, variance, covariance) per forecast horizon and time-of-day
4. **Outputs** corrected forecasts with uncertainty quantification

## Architecture Rules

### 1. File Organization
- **One responsibility per file**
- **No business logic in `__main__`** - scripts only orchestrate
- **Separation of concerns**: Data access, processing, and learning are distinct modules

### 2. Data Standards
- **All energy values = kWh** (standardized)
- **All time blocks = 15-minute resolution** (672 blocks = 7 days)
- **East/West separation** maintained until final combination

### 3. Time Structure
- **672 forecast blocks**: 7 days × 96 fifteen-minute blocks per day
- **96 time-of-day blocks**: 24 hours × 4 blocks per hour (0-95)
- **672 forecast-horizon blocks**: 1-672 fifteen-minute blocks ahead

### 4. Database Standards
- PostgreSQL at `10.100.123.53:5432`
- Schema defined in `db/schema.sql`
- All timestamps in UTC

### 5. Naming Conventions
- **Snake case** for files and variables
- **PascalCase** for class names
- **lowercase_with_underscores** for functions and variables
- **Constants** in UPPER_SNAKE_CASE

### 6. Error Handling
- **Fail fast**: Let errors propagate with clear messages
- **Retry on transient errors** (DB connection issues)
- **Log everything** important to stdout

## Project Structure

```
solar_forecast/
├── README.md              # This file
├── DESIGN_covariance.md   # Detailed design document for temporal correlations
├── config.py              # All configuration constants
├── run.py                 # Entry point wrapper (handles Python path)
├── run_shower.py          # Hourly shower learning entry point
├── run_price.py           # Daily NL day-ahead price forecast entry point
├── run_price_publisher.py # Rolling 15-minute price vector publisher entry point
├── main.py               # Orchestration entry point
├── db/
│   ├── __init__.py
│   ├── client.py         # Database connection and queries
│   └── schema.sql        # Database schema definitions
├── sensors/
│   ├── __init__.py
│   └── forecast_fetcher.py # Fetch raw forecast data from HA DB
├── processing/
│   ├── __init__.py
│   ├── vector_builder.py  # Build 672-block forecast vectors
│   ├── accuracy_learner.py # Learn EMA + temporal correlations + cross-roof covariance
│   ├── shower_learner.py  # Learn 168h shower probability + log-odds nudges
│   ├── nudge_model.py     # Shower log-odds nudge model
│   ├── price_forecaster.py # NL day-ahead price forecast + VAR(1) residual learning
│   ├── price_publisher.py  # Rolling 672-block 15-min price vector publisher
│   ├── combined_builder.py # Combined east+west forecast (mean, variance, correlation)
│   └── backfill.py       # Rebuild learning tables from stored history
└── models/
    └── __init__.py

forecast_gui/               # Web GUI (see below)
├── server.py             # HTTP server: serves the page + /api/data from Postgres
└── static/index.html     # Single page: solar/price Gaussian plumes + shower bars
```

## Quick Start

```bash
# Install dependencies
python3 -m pip install --break-system-packages psycopg2-binary

# Initialize database (already done)
psql -h 10.100.123.53 -p 5432 -U postgres -d homeassistant -f /home/gijs/solar_forecast/db/schema.sql

# Test the full pipeline
python3 /home/gijs/solar_forecast/run.py --test --force

# Run full pipeline manually (stores to DB)
python3 /home/gijs/solar_forecast/run.py --force

# Schedule every 15 minutes (add to crontab)
*/15 * * * * /usr/bin/python3 /home/gijs/solar_forecast/run.py

# Daily NL day-ahead price forecast (11:15 and 12:15 UTC; the script's
# local-time gate runs it once, after the 13:00 Europe/Amsterdam auction)
15 11,12 * * * /home/gijs/venvs/forecast/bin/python /home/gijs/solar_forecast/run_price.py

# Rolling 15-minute price vector publisher (every 15 minutes)
*/15 * * * * /home/gijs/venvs/forecast/bin/python /home/gijs/solar_forecast/run_price_publisher.py
```

## Modules

### `config.py`
Central configuration: DB credentials, sensor names, constants.

### `db/client.py`
PostgreSQL client with connection pooling and standard queries.

### `sensors/forecast_fetcher.py`
Fetches raw forecast data from Home Assistant database. Converts Wh → kWh.

### `processing/vector_builder.py`
Combines 7-day sensor data into 672-element forecast vectors (15-min resolution).

### `processing/accuracy_learner.py`
Learns:
- **EMA Mean/Variance** per (time_of_day_block, forecast_horizon_block, roof)
- **Cross-roof Covariance** per (time_of_day_block, forecast_horizon_block)
- **Local Temporal Correlation** per (time_of_day_block, forecast_horizon_block, roof)
  - Correlates current error with average error in past 2h window
- **Daily Temporal Correlation** per (time_of_day_block, forecast_horizon_block, roof)
  - Correlates current error with average error at same time 24h ago

### `main.py`
Orchestrates the pipeline:
1. Fetch latest forecasts
2. Build vectors
3. Store vectors
4. Update accuracy model

### `processing/price_forecaster.py`
Daily NL day-ahead wholesale (APX/EPEX) price forecast. Wraps the
external [electricitypriceforecast](https://github.com/pefijnvandraat/electricitypriceforecast)
pipeline (MIT, cloned to `/home/gijs/electricitypriceforecast`), which
trains a quantile GBM on public data (Energy-Charts prices, Open-Meteo
weather, TTF gas, CO2). Requires the `venvs/forecast` virtualenv
(pandas, numpy, scikit-learn, requests, PyYAML, holidays).

On top of the external pipeline it:
- Converts the calibrated conformal band to per-hour mean and variance:
  mean = p50, variance = ((p90 - p10) / (2 * 1.2816))^2, growing with lead time
- Overlays hours whose day-ahead price is already published (variance = 0)
- Learns a VAR(1) residual correlation model (day factor + per-hour
  AR(1) + innovation variance, all shrunk by sample count) from the
  pipeline's honest out-of-sample prediction log, stored as a
  `price_var1_v1` snapshot in `model.var_matrices` for Monte Carlo
  path generation

Output: `price_forecast` row with 192 hourly values (8 days), wholesale
EUR/kWh, all times UTC. State lives in `price_state/` (gitignored).

### `processing/price_publisher.py`
Every 15 minutes, republishes the coming 168 hours as a single
672-element vector of 15-minute blocks (matching the solar 672-block
convention) in `price_forecast_15min`:
- **Settled blocks** (rest of today + tomorrow): the real 15-minute
  auction prices from Energy-Charts (native quarterly settlement data,
  token-free), variance = 0
- **Unsettled blocks**: the daily hourly forecast, each hourly
  mean/variance repeated 4 times (price assumed constant within the
  hour), variance grows with lead time

The daily forecast runs once per day, gated on 13:00 Europe/Amsterdam
(after the ~12:40 auction settlement); the publisher only assembles,
so it is fast. Rows older than `PRICE_PUBLISHER_RETENTION_DAYS` (30)
are pruned each run.

## Web GUI (`forecast_gui/`)

Single-page visualization of the sliding 168h window
(`http://<host>:8788`):

- **Solar plume**: mean + P10/P90 from `solar_combined_forecast`
  (15-min blocks, kWh/15min)
- **APX price plume**: mean + P10/P90 from `price_forecast_15min`
  (15-min blocks, EUR/kWh; settled blocks are real auction prices,
  band is flat there)
- **Shower probability bars**: weekly UTC profile mapped onto the window

P10/P90 = mean -/+ 1.2816 * sqrt(variance), matching how the stored
bands were built. Run it with:
`/home/gijs/venvs/forecast/bin/python /home/gijs/solar_forecast/forecast_gui/server.py [port]`

## Database Schema

See `db/schema.sql` for full definitions.

### Core Tables

- `solar_forecast_vectors`: Raw 672-element forecast vectors (kWh per 15-min block)
- `solar_actual_production`: Actual 15-min production data (kWh per block, UTC timestamped)
- `solar_accuracy`: Mean and variance per (time_of_day_block, forecast_horizon_block, roof)
- `solar_covariance`: Cross-roof covariance per (time_of_day_block, forecast_horizon_block)
- `solar_correlation_local`: Local temporal correlation (±2h window)
- `solar_correlation_daily`: Daily temporal correlation (same time 24h ago)
- `solar_reference_magnitude`: Typical production per (time_of_day_block, roof), used to normalize errors
- `solar_combined_forecast`: Combined east+west forecast per issue time: 672-element mean (kWh), variance (kWh^2) and cross-roof correlation arrays
- `price_forecast`: Daily NL day-ahead wholesale price forecast: 192-element hourly mean (EUR/kWh) and variance ((EUR/kWh)^2) arrays per issue time
- `price_forecast_15min`: Rolling 168h price vector at 15-min resolution (672-element mean/variance arrays, republished every 15 minutes; settled blocks are real auction prices)

## Design Decisions

### Why 15-minute blocks?
- Matches Home Assistant sensor resolution
- Finer granularity captures solar variability better (e.g., cloud patterns, tree shadows)
- Still manageable: 672 blocks vs 168 hourly

### Why Fibonacci horizons?
- Computationally efficient: only 12 horizons instead of 672
- Covers full range: 1h to 7 days
- Follows Fibonacci sequence: [4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672] blocks

### Why separate files?
- **Testability**: Each module can be tested independently
- **Maintainability**: Changes to one component don't require touching others
- **Clarity**: Clear separation between "what" (orchestration) and "how" (implementation)

### Why PostgreSQL?
- Already available in infrastructure
- Handles arrays natively (for forecast vectors)
- Reliable, transactional, supports complex queries

### What We Learn

We learn **FORECAST ERROR**, not the forecast itself:
- The external API provides forecasts
- We learn how wrong they typically are:
  - **Absolute error** (kWh) per (time-of-day, horizon, roof): for corrections
  - **Normalized error** = absolute error / reference magnitude, where the
    reference is a slow EMA of typical production at that time of day.
    Comparable across blocks, used for correlations
  - **Variance** of absolute errors (kWh^2): for uncertainty quantification
- We learn how errors correlate across time (temporal correlations)
- This captures local factors the API doesn't know (trees, shadows, orientation, etc.)

Why not divide by the forecast itself? Relative-to-forecast errors explode at
dawn/dusk where the forecast is near zero - a 0.01 kWh error on a 0.01 kWh
forecast reads as 100%. Dividing by the (floored) reference magnitude keeps
errors bounded and meaningful at low light. Normalized learning waits until
the reference for that time-of-day block has at least 3 samples.

### Temporal Correlation Strategy

**Local Correlation (±2h):** Captures short-term patterns like cloud movements.
If we're wrong now, we're likely wrong in the next 2 hours.

**Daily Correlation (24h offset):** Captures systematic daily patterns.
If the model was consistently wrong at this time yesterday, 
it's likely to be wrong today too.

**Horizon Handling:** For horizons < 24h, we still compute both correlations,
using whatever historical data is available.

See `DESIGN_covariance.md` for detailed pseudo-code and rationale.

## Future Enhancements

- [x] Temporal correlation (local ±2h, daily 24h offset)
- [x] Fibonacci horizon selection for computational efficiency
- [ ] Dynamic horizon grouping (for sparse data)
- [ ] Anomaly detection
- [ ] Forecast combination with covariance
- [ ] REST API for serving predictions

## Current Status

✅ **IMPLEMENTED AND TESTED:**
- Database schema with all 6 tables
- Forecast vector building (672 × 15-min blocks)
- EMA mean/variance learning for Fibonacci horizons
- Local temporal correlation learning (±2h window)
- Daily temporal correlation learning (24h offset)
- Modular architecture with clean separation of concerns
- UTC timestamp handling throughout
- Complete pipeline orchestration
- Fix for accuracy learner to use database-stored actuals instead of sensor values
- Comprehensive unit tests (`test_minimal.py`, `test_fibonacci.py`, `test_end_to_end.py`)

✅ **VERIFIED WORKING:**
- Database connection to PostgreSQL at `10.100.123.53:5432`
- Accuracy learning with correct error calculations
- Proper Fibonacci horizon handling (4, 8, 12, 20, 32, 52, 84, 136, 220, 356, 576, 672 blocks)
- Test mode working with `--test --force` flags

⚠️ **BLOCKED ON SENSOR DATA:**
- Accuracy learner requires `sensor.solar_quaterly_energy_east_roof` and `sensor.solar_quaterly_energy_west_roof`
- These sensors don't exist yet in Home Assistant
- Falls back to hourly sensors (`sensor.solar_hourly_energy_east_roof`, `sensor.solar_hourly_energy_west_roof`)
- If no sensors available: **no data, no learning, live moves on** (silently skips)
- Once quarterly sensors are created, system will automatically use them

📋 **NEXT STEPS:**
1. Create the quarterly sensors in Home Assistant (or verify hourly sensors are working)
2. Deploy to production: `*/15 * * * * /usr/bin/python3 /home/gijs/solar_forecast/run.py`
3. Monitor accuracy learning over time
4. Test temporal correlation learning with real data
5. Validate with historical data analysis
