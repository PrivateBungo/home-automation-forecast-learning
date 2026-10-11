"""
Shower Behaviour Learner
=======================

Learns shower behaviour from the Home Assistant recorder states of a
binary shower sensor.

Signal:
- A shower event is a positive (off -> on) edge of the sensor.
- Debounce: edges within SHOWER_DEBOUNCE_MINUTES (30 min) of a previously
  accepted edge are ignored (sensor chatter / re-triggering).

Hourly learning loop (processes every completed hour h):
- X(h) = 1 if a debounced event falls inside hour h, else 0
- 168h probability vector (the means): P[i] = P[i]*(1-a) + X*a with
  i = weekday*24 + hour (UTC), EMA alpha = SHOWER_EMA_ALPHA (0.2)
- Log-odds nudge model (processing/nudge_model.py):
  logit(P_h) = logit(M_slot) + applied nudges for fired blocks
  (block_0_3h, block_6_18h, block_0_30h_neg). Updated on block firing with
  alpha_eff * (y - P_h) against the combined model. Every run appends a
  'shower_nudge_v1' snapshot to model.var_matrices.

The legacy var24 lag correlation (shower_correlation_lag) is no longer
written; the table is left in place, frozen.

All times UTC. Index convention: Monday=0, i = weekday*24 + hour (0..167).
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone

from solar_forecast.config import (
    SHOWER_SENSOR_ENTITY,
    SHOWER_SENSOR_NAME,
    SHOWER_EMA_ALPHA,
    SHOWER_DEBOUNCE_MINUTES,
    SHOWER_VECTOR_HOURS,
    MATRIX_TYPE_SHOWER_NUDGE
)
from solar_forecast.db.client import DatabaseClient
from solar_forecast.processing import nudge_model

logger = logging.getLogger(__name__)


class ShowerLearner:
    """
    Learns shower behaviour: 168h probability means + log-odds nudges.
    
    Called hourly. Idempotent per hour: hours already learned
    (shower_probability_168h.last_updated_hour) are never reprocessed.
    On first run it backfills the means from the recorder history; the
    nudge model bootstraps by replaying shower_events history with a
    reduced alpha_max (only if no shower_nudge_v1 snapshot exists).
    """

    def __init__(self):
        self.db = DatabaseClient()

    # ============================================================
    # MAIN ENTRY POINT
    # ============================================================

    def update(self, test_mode: bool = False) -> Dict[str, Any]:
        """
        Update all shower models from recorder states.

        Args:
            test_mode: If True, print to terminal, no database writes.

        Returns:
            Summary dict of the learning run.
        """
        now_utc = self._now()

        with self.db as db:
            # Earliest recorder state of the sensor defines data coverage:
            # X(h) is only "known" for hours at or after the first full hour.
            first_state = self._get_first_state_time(db)
            if first_state is None:
                logger.info(f"No recorder history for {SHOWER_SENSOR_ENTITY}, nothing to learn")
                return {'status': 'skipped', 'reason': 'no_history'}

            first_hour = self._ceil_hour(first_state)
            last_completed_hour = self._floor_hour(now_utc) - timedelta(hours=1)

            prob = db.get_shower_probability(SHOWER_SENSOR_NAME)
            matrix = db.get_latest_var_matrix(MATRIX_TYPE_SHOWER_NUDGE)
            nudge_bootstrap = matrix is None

            # --------------------------------------------------------
            # Events + hours to learn
            # --------------------------------------------------------
            if prob is None:
                # Fresh install: replay full recorder history (means + nudges)
                logger.info("No shower vector found, backfilling from recorder history")
                states = db.get_binary_sensor_states_between(
                    SHOWER_SENSOR_ENTITY,
                    first_state.isoformat(),
                    now_utc.isoformat()
                )
                events = self._debounce(self._positive_edges(states), None)
                if not test_mode:
                    db.save_shower_events(
                        SHOWER_SENSOR_NAME,
                        [t.isoformat() for t in events]
                    )
                p_vector = [0.0] * SHOWER_VECTOR_HOURS
                mean_x = None
                sample_count = 0
                last_updated_hour = None
                hours = self._hour_range(first_hour, last_completed_hour)
                all_events = list(events)
                save_probability = True
            else:
                p_vector = prob['p_shower_168h']
                mean_x = prob['mean_x']
                sample_count = prob['sample_count']
                last_updated_hour = prob['last_updated_hour']

                if len(p_vector) != SHOWER_VECTOR_HOURS:
                    logger.error(
                        f"Stored vector has {len(p_vector)} elements, "
                        f"expected {SHOWER_VECTOR_HOURS}"
                    )
                    return {'status': 'error', 'reason': 'vector_length_mismatch'}

                # Detect new edges since the last learned hour. The window
                # starts at the last learned hour (for prior-state context),
                # but only edges after it are new; events inside the last
                # learned hour were already detected and stored by that run.
                window_start = last_updated_hour
                states = db.get_binary_sensor_states_between(
                    SHOWER_SENSOR_ENTITY,
                    window_start.isoformat(),
                    now_utc.isoformat()
                )
                new_from = last_updated_hour + timedelta(hours=1)
                edges = [
                    t for t in self._positive_edges(states) if t >= new_from
                ]
                events = self._debounce(
                    edges, db.get_last_shower_event(SHOWER_SENSOR_NAME)
                )
                if not test_mode:
                    db.save_shower_events(
                        SHOWER_SENSOR_NAME,
                        [t.isoformat() for t in events]
                    )

                hours = self._hour_range(
                    last_updated_hour + timedelta(hours=1), last_completed_hour
                )

                if nudge_bootstrap:
                    # Nudge bootstrap with existing means: replay ALL history
                    # (nudges only). Means are replayed in-memory from zeros
                    # so the pre-tick P_h trajectory is faithful, but the
                    # stored shower_probability_168h row is left untouched.
                    stored_events = db.get_shower_events_between(
                        SHOWER_SENSOR_NAME,
                        first_hour.isoformat(),
                        now_utc.isoformat()
                    )
                    all_events = sorted(set(stored_events) | set(events))
                    p_vector = [0.0] * SHOWER_VECTOR_HOURS
                    mean_x = None
                    sample_count = 0
                    last_updated_hour = None
                    hours = self._hour_range(first_hour, last_completed_hour)
                    save_probability = False
                else:
                    if not hours:
                        logger.info("All completed hours already learned, nothing to do")
                        return {
                            'status': 'up_to_date',
                            'timestamp': now_utc.isoformat(),
                            'hours_processed': 0
                        }
                    # X per hour for the nudge lookback windows
                    stored_events = db.get_shower_events_between(
                        SHOWER_SENSOR_NAME,
                        (hours[0] - timedelta(
                            hours=nudge_model.MAX_LOOKBACK_HOURS)).isoformat(),
                        now_utc.isoformat()
                    )
                    all_events = sorted(set(stored_events) | set(events))
                    save_probability = True

            if not hours:
                logger.info("No completed hours to learn yet")
                return {
                    'status': 'up_to_date',
                    'timestamp': now_utc.isoformat(),
                    'hours_processed': 0
                }

            # --------------------------------------------------------
            # Nudge model state
            # --------------------------------------------------------
            if nudge_bootstrap:
                nudge_state = nudge_model.new_state()
                nudge_cfg = nudge_model.NudgeConfig.bootstrap()
            else:
                nudge_state = nudge_model.state_from_payload(matrix['payload'])
                nudge_cfg = nudge_model.NudgeConfig()

            # --------------------------------------------------------
            # Per-hour learning loop
            # --------------------------------------------------------
            x_by_hour = {}
            for t in all_events:
                x_by_hour[self._floor_hour(t)] = 1
            event_hours = set(x_by_hour.keys())

            known_from = first_hour
            last_x = None
            updated_hour_index = None

            for hour_start in hours:
                hour_index = hour_start.weekday() * 24 + hour_start.hour
                x = x_by_hour.get(hour_start, 0)

                # 1. Pre-tick combined prediction P_h (old means, old nudges).
                #    Blocks whose lookback window is outside data coverage
                #    are treated as not fired (complement contributes 0).
                fired = nudge_model.fired_flags(
                    event_hours, hour_start, known_from=known_from
                )
                p_h = nudge_model.predict(
                    p_vector[hour_index], nudge_state, fired, nudge_cfg
                )

                # 2. Mean update (EMA, unchanged)
                p_vector[hour_index] = p_vector[hour_index] * (1 - SHOWER_EMA_ALPHA) \
                    + x * SHOWER_EMA_ALPHA
                if mean_x is None:
                    mean_x = float(x)
                else:
                    mean_x = mean_x * (1 - SHOWER_EMA_ALPHA) + x * SHOWER_EMA_ALPHA

                # 3. Nudge update: every fired block takes an
                #    alpha_eff * (y - P_h) gradient step vs the combined model
                if any(fired.values()):
                    nudge_model.update(
                        nudge_state, fired, x, p_h,
                        hour_start + timedelta(hours=1), nudge_cfg
                    )

                sample_count += 1
                last_x = x
                updated_hour_index = hour_index
                logger.debug(
                    f"Hour {hour_start.isoformat()} learned: X={x}, "
                    f"P_h={p_h:.3f}, P[{hour_index}]={p_vector[hour_index]:.3f}"
                )

            # --------------------------------------------------------
            # Persist
            # --------------------------------------------------------
            means_ref = None
            if save_probability:
                if not test_mode:
                    db.save_shower_probability(
                        sensor=SHOWER_SENSOR_NAME,
                        p_shower_168h=[round(p, 6) for p in p_vector],
                        mean_x=mean_x,
                        sample_count=sample_count,
                        last_x=last_x,
                        updated_hour_index=updated_hour_index,
                        last_updated_hour=hours[-1].isoformat()
                    )
                    means_ref = hours[-1].isoformat()
            else:
                # Means untouched: reference the existing stored row
                stored_prob = db.get_shower_probability(SHOWER_SENSOR_NAME)
                if stored_prob is not None:
                    means_ref = stored_prob['last_updated_hour'].isoformat()

            if not test_mode:
                db.insert_var_matrix(
                    matrix_type=MATRIX_TYPE_SHOWER_NUDGE,
                    calculated_at=now_utc.isoformat(),
                    target_hour=hours[-1].isoformat(),
                    payload={
                        'means_ref': means_ref,
                        'nudges': nudge_model.state_to_payload_section(nudge_state),
                        'config': {
                            **nudge_cfg.as_dict(),
                            'bootstrap_replay': nudge_bootstrap
                        }
                    }
                )
            elif means_ref is None:
                means_ref = hours[-1].isoformat()

            if nudge_bootstrap:
                logger.info("Nudge model bootstrap replay complete, per-cell state:")
                for name in nudge_model.BLOCK_NAMES:
                    cell = nudge_state[name]
                    logger.info(
                        f"  {name}: nu={cell['value']:+.4f}, n_fired={cell['n']}, "
                        f"last_fired_at={cell['last_fired_at']}"
                    )

            result = {
                'status': 'completed',
                'timestamp': now_utc.isoformat(),
                'hours_processed': len(hours),
                'new_events': len(events),
                'sample_count': sample_count,
                'last_x': last_x,
                'updated_hour_index': updated_hour_index,
                'nudge_bootstrap': nudge_bootstrap,
                'nudges': {
                    name: {
                        'value': nudge_state[name]['value'],
                        'n': nudge_state[name]['n'],
                        'last_fired_at': nudge_state[name]['last_fired_at']
                    }
                    for name in nudge_model.BLOCK_NAMES
                },
                'means_ref': means_ref
            }

            logger.info(
                f"Shower learning completed: {len(hours)} hours processed, "
                f"{len(events)} new events, nudges "
                f"{ {n: round(nudge_state[n]['value'], 3) for n in nudge_model.BLOCK_NAMES} }"
            )
            return result

    # ============================================================
    # SIGNAL PROCESSING
    # ============================================================

    def _positive_edges(self, states: List[Dict[str, Any]]) -> List[datetime]:
        """
        Extract positive (off -> on) edge timestamps from a state sequence.

        The first list element is the state entering the window (may be
        'on' if the sensor was already on; that is not an edge).
        """
        edges = []
        prev = None
        for entry in states:
            if entry['state'] == 'on' and prev == 'off':
                edges.append(entry['timestamp'])
            prev = entry['state']
        return edges

    def _debounce(
        self,
        edges: List[datetime],
        last_accepted: Optional[datetime]
    ) -> List[datetime]:
        """
        Apply the debounce: ignore edges within SHOWER_DEBOUNCE_MINUTES
        of the previously accepted edge.

        Args:
            edges: Chronologically ordered edge timestamps.
            last_accepted: Last accepted event before this window (continuity).

        Returns:
            Accepted (debounced) edge timestamps.
        """
        accepted = []
        last = last_accepted
        for t in sorted(edges):
            if last is None or (t - last) >= timedelta(minutes=SHOWER_DEBOUNCE_MINUTES):
                accepted.append(t)
                last = t
        return accepted

    def _get_first_state_time(self, db) -> Optional[datetime]:
        """Get the first recorder state timestamp of the shower sensor (UTC)."""
        result = db.query_one(
            """
                SELECT to_timestamp(MIN(s.last_updated_ts))
                FROM states s
                JOIN states_meta sm ON s.metadata_id = sm.metadata_id
                WHERE sm.entity_id = %s
                  AND s.state IN ('on', 'off')
            """,
            (SHOWER_SENSOR_ENTITY,)
        )
        return result[0] if result and result[0] else None

    # ============================================================
    # TIME HELPERS
    # ============================================================

    def _now(self) -> datetime:
        """Current UTC time (instance-override hook for tests)."""
        return datetime.now(timezone.utc)

    def _floor_hour(self, ts: datetime) -> datetime:
        """Floor a datetime to the start of its hour."""
        return ts.replace(minute=0, second=0, microsecond=0)

    def _ceil_hour(self, ts: datetime) -> datetime:
        """Ceil a datetime to the start of the next full hour."""
        floored = self._floor_hour(ts)
        if ts == floored:
            return floored
        return floored + timedelta(hours=1)

    def _hour_range(self, start: datetime, end: datetime) -> List[datetime]:
        """List of hour starts from start to end inclusive."""
        hours = []
        h = start
        while h <= end:
            hours.append(h)
            h += timedelta(hours=1)
        return hours


# ============================================================
# STANDALONE SCRIPT
# ============================================================
if __name__ == '__main__':
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description='Update shower behaviour learning')
    parser.add_argument('--test', action='store_true', help='Run in test mode (no DB writes)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        learner = ShowerLearner()
        result = learner.update(test_mode=args.test)
        print(f"\nStatus: {result.get('status')}")
        if args.test:
            print(f"Result: {result}")
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
