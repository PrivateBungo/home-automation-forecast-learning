#!/usr/bin/env python3
"""
Run Script for Rolling 15-Minute Price Publisher
================================================

Wrapper script to run the rolling price vector publisher.
Handles the Python path setup, mirroring run.py.

Usage:
    # Publish the rolling 672-block vector
    run_price_publisher.py

    # Run in test mode (no DB writes)
    run_price_publisher.py --test

Schedule with cron (every 15 minutes):
    */15 * * * * /home/gijs/venvs/forecast/bin/python /home/gijs/solar_forecast/run_price_publisher.py
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

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(asctime)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

from solar_forecast.processing.price_publisher import PricePublisher


def main():
    parser = argparse.ArgumentParser(
        description='Rolling 15-minute price publisher')
    parser.add_argument('--test', action='store_true',
                        help='Test mode: print to terminal, no database writes')
    args = parser.parse_args()

    publisher = PricePublisher()
    summary = publisher.update(test_mode=args.test)
    logging.info(f"Price publisher run summary: {summary}")
    if summary.get('status') == 'failed':
        sys.exit(1)


if __name__ == '__main__':
    main()
