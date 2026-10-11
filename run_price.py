#!/usr/bin/env python3
"""
Run Script for NL Day-Ahead Price Forecast
==========================================

Wrapper script to run the daily price forecast pipeline.
Handles the Python path setup, mirroring run.py.

Usage:
    # Run full pipeline (train external model, write to DB)
    run_price.py

    # Run in test mode (no DB writes)
    run_price.py --test

    # Skip the 13:00-local auction gate (manual runs)
    run_price.py --force

Schedule with cron (fires at 11:15 AND 12:15 UTC; the script's
local-time gate lets only the post-auction firing run):
    15 11,12 * * * /home/gijs/venvs/forecast/bin/python /home/gijs/solar_forecast/run_price.py
"""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(project_dir)
sys.path.insert(0, parent_dir)
sys.path.insert(0, project_dir)

import argparse
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

from solar_forecast.config import (
    PRICE_DAILY_RUN_TZ,
    PRICE_DAILY_RUN_LOCAL_HOUR,
)
from solar_forecast.processing.price_forecaster import PriceForecaster


def main():
    parser = argparse.ArgumentParser(description='NL day-ahead price forecast')
    parser.add_argument('--test', action='store_true',
                        help='Test mode: print to terminal, no database writes')
    parser.add_argument('--force', action='store_true',
                        help='Skip the local-time gate (for manual runs)')
    args = parser.parse_args()

    # The machine runs in UTC; the day-ahead auction settles around
    # 12:40-13:00 Europe/Amsterdam. Cron fires this script at 11:15 and
    # 12:15 UTC; only the firing at/after 13:00 local does the work.
    local_now = datetime.now(ZoneInfo(PRICE_DAILY_RUN_TZ))
    if not args.force and local_now.hour < PRICE_DAILY_RUN_LOCAL_HOUR:
        logging.info(
            f"Local time {local_now:%H:%M} is before "
            f"{PRICE_DAILY_RUN_LOCAL_HOUR}:00 {PRICE_DAILY_RUN_TZ}; "
            f"auction has not settled yet, skipping")
        return

    forecaster = PriceForecaster()
    summary = forecaster.update(test_mode=args.test)
    logging.info(f"Price forecast run summary: {summary}")
    if summary.get('status') != 'ok':
        sys.exit(1)


if __name__ == '__main__':
    main()
