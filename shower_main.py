#!/usr/bin/env python3
"""
Shower Learning Orchestration Script
====================================

Entry point for the shower behaviour learning system.
Runs hourly, learns the 168h shower probability vector (EMA) and the
log-odds nudge model from the Home Assistant recorder states.

Usage:
    # Run hourly learning
    python3 shower_main.py

    # Run in test mode (print to terminal, no DB writes)
    python3 shower_main.py --test

    # Force run (ignore hourly timing)
    python3 shower_main.py --force

Schedule with cron (hourly, at the top of the hour):
    0 * * * * /usr/bin/python3 /home/gijs/solar_forecast/run_shower.py
"""

import sys
import os
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

import argparse
import logging
from datetime import datetime, timezone

from solar_forecast.config import SHOWER_SENSOR_ENTITY
from solar_forecast.processing.shower_learner import ShowerLearner

logger = logging.getLogger(__name__)


class ShowerOrchestrator:
    """
    Orchestrates the shower behaviour learning loop.

    Runs hourly (at :00), learning every hour that completed since the
    last run:
    1. Detect debounced positive (off -> on) edges of the shower sensor
    2. Update the 168h probability vector via EMA (alpha = 0.2)
    3. Update the log-odds nudge model (blocks 0_3h, 6_18h, 0_30h_neg)
       and append a shower_nudge_v1 snapshot to model.var_matrices

    All times UTC. Idempotent per hour.
    """

    def __init__(self):
        self.learner = ShowerLearner()

    def run(self, test_mode: bool = False, force: bool = False) -> dict:
        timestamp = datetime.now(timezone.utc)

        if not force and not self._is_learning_time(timestamp):
            logger.info(f"Not top of the hour (minute={timestamp.minute}), skipping")
            return {'status': 'skipped', 'reason': 'not_hour_boundary'}

        logger.info(f"Starting shower learning at {timestamp.isoformat()} UTC")
        result = self.learner.update(test_mode=test_mode)
        result.setdefault('timestamp', timestamp.isoformat())
        return result

    def _is_learning_time(self, timestamp: datetime) -> bool:
        """Cron fires at :00; allow a small jitter window."""
        return timestamp.minute == 0


# ============================================================
# MAIN ENTRY POINT
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Shower Behaviour Learning System - Main Orchestrator'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode: print to terminal, no database writes'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force run immediately (ignore hourly timing)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose logging'
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    try:
        orchestrator = ShowerOrchestrator()
        result = orchestrator.run(
            test_mode=args.test,
            force=args.force
        )

        if args.test:
            print(f"\n[TEST MODE] Result for {SHOWER_SENSOR_ENTITY}:")
            print(f"  Status: {result.get('status')}")
            print(f"  Hours processed: {result.get('hours_processed', 0)}")
            print(f"  New events: {result.get('new_events', 0)}")
            if result.get('nonzero_slots') is not None:
                print(f"  Non-zero vector slots: {result['nonzero_slots']}")
            if result.get('lag_correlations') is not None:
                print(f"  Lag correlations: {result['lag_correlations']}")
        else:
            logger.info(f"Shower learning finished: {result.get('status')}")

    except Exception as e:
        logger.error(f"Shower learning failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
