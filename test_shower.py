#!/usr/bin/env python3
"""
Test suite for the shower learner with the log-odds nudge model.

Part 1 - pure nudge model unit tests (in-memory, deterministic RNG):
- update formula: step is exactly alpha_eff * (y - P_h); no (1-alpha)*nu decay
- convergence: synthetic refractory process -> nu_1 strongly negative
- holiday gate: absence -> effective P drops well below the mean (factor reported)
- mean gating: M = 0.005 with trained positive nudges -> P stays below 0.05
- backtest: 14-day holdout, hourly log-loss means-only vs means+nudges

Part 2 - end-to-end tests against an ISOLATED database (solar_test_shower)
with a synthetic sensor and a fake clock (production data never touched):
- bootstrap: debounced events, 168h means, nudge bootstrap replay,
  shower_nudge_v1 snapshot structure
- idempotency: rerun with no new data changes nothing (no extra snapshot)
- incremental: new shower -> new snapshot row, vector slot update,
  nudge cell growth; shower_correlation_lag stays empty (no writes)

Usage:
    python3 test_shower.py
"""

import sys
import os
import math
import random

project_dir = os.path.dirname(os.path.abspath(__file__))
package_root = os.path.dirname(project_dir)
sys.path.insert(0, project_dir)
sys.path.insert(0, package_root)

# Isolated database + synthetic sensor BEFORE importing config
os.environ['SOLAR_FORECAST_DB_DATABASE'] = 'solar_test_shower'
os.environ['SOLAR_FORECAST_SHOWER_ENTITY'] = 'binary_sensor.shower_test'
os.environ['SOLAR_FORECAST_SHOWER_NAME'] = 'shower_test'

import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING)

from solar_forecast.db.client import DatabaseClient
from solar_forecast.processing import nudge_model
from solar_forecast.processing.nudge_model import NudgeConfig
from solar_forecast.config import (
    SHOWER_EMA_ALPHA,
    SHOWER_VECTOR_HOURS,
    SHOWER_DEBOUNCE_MINUTES,
    SHOWER_NUDGE_K_SHRINK,
    SHOWER_NUDGE_BOOTSTRAP_ALPHA_MAX,
    MATRIX_TYPE_SHOWER_NUDGE
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


# ============================================================
# PART 1: PURE NUDGE MODEL TESTS
# ============================================================

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)  # fixed reference time


def train_hours(start, end, event_hours, cfg=None, alpha_mean=SHOWER_EMA_ALPHA):
    """
    In-memory replica of the learner's per-hour loop (means EMA +
    nudge updates). known_from = start, mirroring the learner's
    coverage rule.
    """
    cfg = cfg or NudgeConfig()
    p_vector = [0.0] * SHOWER_VECTOR_HOURS
    state = nudge_model.new_state()
    h = start
    while h < end:
        idx = h.weekday() * 24 + h.hour
        x = 1 if h in event_hours else 0
        fired = nudge_model.fired_flags(event_hours, h, known_from=start)
        p_h = nudge_model.predict(p_vector[idx], state, fired, cfg)
        p_vector[idx] = p_vector[idx] * (1 - alpha_mean) + x * alpha_mean
        nudge_model.update(state, fired, x, p_h, h + timedelta(hours=1), cfg)
        h += timedelta(hours=1)
    return p_vector, state


def generate_process(start, days, rng, base_p, refractory_p=None,
                     holiday_days=(), morning_hour=None, morning_p=1.0,
                     evening_hour=None, evening_p=0.0):
    """
    Synthetic shower process. Hourly hazard = refractory_p if an event
    occurred within the last 3 hours (and refractory_p is set), else the
    schedule hazard (morning/evening slots or base_p). Holidays: no events.
    Returns the set of event hours.
    """
    event_hours = set()
    h = start
    for _ in range(days * 24):
        d = (h - start).days
        if d in holiday_days:
            h += timedelta(hours=1)
            continue
        if refractory_p is not None and any(
            (h - e) <= timedelta(hours=3) and (h - e) > timedelta(hours=0)
            for e in event_hours
        ):
            p = refractory_p
        elif morning_hour is not None and h.hour == morning_hour:
            p = morning_p
        elif evening_hour is not None and h.hour == evening_hour:
            p = evening_p
        else:
            p = base_p
        if rng.random() < p:
            event_hours.add(h)
        h += timedelta(hours=1)
    return event_hours


def test_update_formula():
    print("\n=== Unit: update formula (no degeneracy) ===")
    cfg = NudgeConfig()
    now = T0 + timedelta(hours=100)

    # (a) step is exactly alpha_eff * (y - P_h)
    state = nudge_model.new_state()
    state['block_0_3h'] = {
        'value': 1.0, 'n': 20,
        'last_fired_at': now - timedelta(hours=10),
        'first_fired_at': now - timedelta(hours=100)
    }
    cell = state['block_0_3h']
    expected_alpha = nudge_model.alpha_eff(cell, now, cfg)
    y, p_h = 1, 0.6
    expected_nu = 1.0 + expected_alpha * (y - p_h)
    nudge_model.update(state, {'block_0_3h': True}, y, p_h, now, cfg)
    check("step is exactly alpha_eff * (y - P_h)",
          abs(cell['value'] - expected_nu) < 1e-12,
          f"expected {expected_nu}, got {cell['value']}")
    check("alpha_eff in [alpha_min, alpha_max]",
          cfg.alpha_min <= expected_alpha <= cfg.alpha_max,
          f"got {expected_alpha}")

    # (b) no (1 - alpha) * nu decay: zero surprise -> nu unchanged
    state = nudge_model.new_state()
    state['block_0_3h'] = {
        'value': 2.0, 'n': 50,
        'last_fired_at': now - timedelta(hours=1),
        'first_fired_at': now - timedelta(hours=200)
    }
    nudge_model.update(state, {'block_0_3h': True}, y=1, p_h=1.0,
                       fired_at=now, cfg=cfg)
    check("zero surprise (y == P_h) leaves nu unchanged (no (1-a)*nu decay)",
          state['block_0_3h']['value'] == 2.0,
          f"got {state['block_0_3h']['value']}")

    # (c) repeated positive surprise grows nu (does not decay toward 0)
    state = nudge_model.new_state()
    cfg2 = NudgeConfig()
    nu_history = []
    for i in range(60):
        fired = {'block_0_3h': True, 'block_6_18h': False,
                 'block_0_30h_neg': False}
        p_h = nudge_model.predict(0.1, state, fired, cfg2)  # mean held fixed
        nudge_model.update(state, fired, y=1, p_h=p_h,
                           fired_at=now + timedelta(hours=i), cfg=cfg2)
        nu_history.append(state['block_0_3h']['value'])
    check("repeated evidence grows nu away from 0",
          nu_history[-1] > 0.5 and nu_history[-1] > abs(nu_history[10]),
          f"nu after 60 firings = {nu_history[-1]:.3f}")


def test_convergence_refractory():
    print("\n=== Unit: convergence on synthetic refractory process ===")
    rng = random.Random(42)
    days = 60
    # Known effect: P(shower) = 0.3 baseline, 0.02 if showered <= 3h ago
    event_hours = generate_process(T0, days, rng, base_p=0.3, refractory_p=0.02)
    p_vector, state = train_hours(T0, T0 + timedelta(days=days), event_hours)

    nu1 = state['block_0_3h']['value']
    n1 = state['block_0_3h']['n']
    applied = nu1 * nudge_model.shrink(n1, SHOWER_NUDGE_K_SHRINK)
    print(f"  nu_1 = {nu1:+.3f} (n={n1}), applied = {applied:+.3f}")
    print(f"  nu_2 = {state['block_6_18h']['value']:+.3f} (n={state['block_6_18h']['n']})")
    print(f"  nu_3 = {state['block_0_30h_neg']['value']:+.3f} (n={state['block_0_30h_neg']['n']})")

    check("nu_1 strongly negative (refractory learned)",
          nu1 < -1.0, f"got {nu1:+.3f}")
    check("applied nu_1 plausible magnitude (logit-distance 0.3 -> 0.02)",
          -4.5 < applied < -1.0, f"got {applied:+.3f}")


def test_holiday_gate():
    print("\n=== Unit: holiday gate (10-day absence) ===")
    rng = random.Random(7)
    days = 120
    # Morning + evening showers, 5-day absences every ~2 weeks
    holidays = set()
    for hd in range(20, days - 5, 15):
        holidays.update(range(hd, hd + 5))
    event_hours = generate_process(
        T0, days, rng, base_p=0.0, morning_hour=7, morning_p=0.97,
        evening_hour=20, evening_p=0.5,
        holiday_days=holidays
    )
    start = T0
    end_train = T0 + timedelta(days=days)
    p_vector, state = train_hours(start, end_train, event_hours)

    # Means at morning slots before the simulated absence
    morning_slots = [wd * 24 + 7 for wd in range(7)]
    m_before = {s: p_vector[s] for s in morning_slots if p_vector[s] > 0.3}
    check("morning slots learned (M > 0.3)", len(m_before) >= 5,
          str({s: round(p_vector[s], 2) for s, p in m_before.items()}))

    # 10 days with zero events: continue LEARNING through the absence,
    # carrying over the trained state and means
    state_c = {k: dict(v) for k, v in state.items()}
    pv = list(p_vector)
    h = end_train
    end_abs = end_train + timedelta(days=10)
    cfg = NudgeConfig()
    while h < end_abs:
        idx = h.weekday() * 24 + h.hour
        fired = nudge_model.fired_flags(set(), h, known_from=start)
        p_h = nudge_model.predict(pv[idx], state_c, fired, cfg)
        pv[idx] = pv[idx] * (1 - SHOWER_EMA_ALPHA)  # y = 0 every hour
        nudge_model.update(state_c, fired, 0, p_h, h + timedelta(hours=1), cfg)
        h += timedelta(hours=1)

    fired_gate = {'block_0_3h': False, 'block_6_18h': False,
                  'block_0_30h_neg': True}
    worst_factor = 0.0
    for slot, m0 in m_before.items():
        p_eff = nudge_model.predict(pv[slot], state_c, fired_gate, cfg)
        factor = p_eff / m0
        worst_factor = max(worst_factor, factor)
        print(f"  slot {slot}: M_before={m0:.3f}, M_now={pv[slot]:.3f}, "
              f"P_eff={p_eff:.3f}, factor={factor:.3f}")
    print(f"  nu_3 after absence = {state_c['block_0_30h_neg']['value']:+.3f} "
          f"(n={state_c['block_0_30h_neg']['n']})")
    check("effective P for all morning slots well below the pre-absence mean",
          worst_factor < 0.6, f"worst factor {worst_factor:.3f}")


def test_mean_gating():
    print("\n=== Unit: mean gating (near-zero slot cannot be resurrected) ===")
    rng = random.Random(11)
    days = 100
    # Positive refractory block: after a shower, MORE likely to shower
    # within 3 hours (double-shower habit: 0.4 vs 0.08 baseline)
    event_hours = generate_process(T0, days, rng, base_p=0.08, refractory_p=0.4)
    p_vector, state = train_hours(T0, T0 + timedelta(days=days), event_hours)

    nu1 = state['block_0_3h']['value']
    n1 = state['block_0_3h']['n']
    print(f"  trained nu_1 = {nu1:+.3f} (n={n1}), "
          f"nu_2 = {state['block_6_18h']['value']:+.3f}, "
          f"nu_3 = {state['block_0_30h_neg']['value']:+.3f}")
    check("positive refractory habit learned (nu_1 > 0.5)", nu1 > 0.5,
          f"got {nu1:+.3f}")

    # Rare slot M = 0.005 with maximum positive fired nudges
    fired = {'block_0_3h': True, 'block_6_18h': True,
             'block_0_30h_neg': False}
    p_eff = nudge_model.predict(0.005, state, fired, NudgeConfig())
    print(f"  P_eff(M=0.005, max positive nudges) = {p_eff:.4f}")
    check("P stays below 0.05 (multiplicative gating)",
          p_eff < 0.05, f"got {p_eff:.4f}")


def test_backtest():
    print("\n=== Unit: backtest, 14-day holdout log-loss ===")
    rng = random.Random(3)
    total_days = 74
    holdout_days = 14
    train_days = total_days - holdout_days
    # Realistic structure: morning showers, evening showers,
    # refractory, and a 5-day holiday inside the holdout
    holiday_in_holdout = set(range(train_days + 4, train_days + 9))
    all_events = generate_process(
        T0, total_days, rng, base_p=0.01, refractory_p=0.02,
        morning_hour=7, morning_p=0.8,
        evening_hour=20, evening_p=0.3,
        holiday_days=holiday_in_holdout
    )
    train_events = {e for e in all_events if e < T0 + timedelta(days=train_days)}

    p_vector, state = train_hours(
        T0, T0 + timedelta(days=train_days), train_events)
    cfg = NudgeConfig()

    def logloss(p, y):
        p = min(max(p, 1e-4), 1 - 1e-4)
        return -(y * math.log(p) + (1 - y) * math.log(1 - p))

    ll_means = 0.0
    ll_combined = 0.0
    n_eval = 0
    h = T0 + timedelta(days=train_days)
    while h < T0 + timedelta(days=total_days):
        idx = h.weekday() * 24 + h.hour
        y = 1 if h in all_events else 0
        ll_means += logloss(p_vector[idx], y)
        fired = nudge_model.fired_flags(all_events, h, known_from=T0)
        p_h = nudge_model.predict(p_vector[idx], state, fired, cfg)
        ll_combined += logloss(p_h, y)
        n_eval += 1
        h += timedelta(hours=1)

    ll_means /= n_eval
    ll_combined /= n_eval
    print(f"  holdout: {n_eval} hours, {sum(1 for e in all_events if e >= T0 + timedelta(days=train_days))} showers, "
          f"holiday days {sorted(holiday_in_holdout)}")
    print(f"  mean hourly log-loss  means-only : {ll_means:.4f}")
    print(f"  mean hourly log-loss  combined   : {ll_combined:.4f}")
    check("means+nudges beats means-only on holdout",
          ll_combined < ll_means,
          f"{ll_combined:.4f} vs {ll_means:.4f}")


def run_unit_tests():
    test_update_formula()
    test_convergence_refractory()
    test_holiday_gate()
    test_mean_gating()
    test_backtest()


# ============================================================
# PART 2: ISOLATED DATABASE INTEGRATION TESTS
# ============================================================

def seed_states(db, entries):
    """Insert (datetime, state) pairs as recorder states for the test sensor."""
    metadata_id = db.query_one(
        "SELECT metadata_id FROM states_meta WHERE entity_id = %s",
        (os.environ['SOLAR_FORECAST_SHOWER_ENTITY'],)
    )[0]
    for dt, state in entries:
        db.query(
            """
            INSERT INTO states (metadata_id, state, last_updated_ts)
            VALUES (%s, %s, %s)
            """,
            (metadata_id, state, dt.timestamp()),
            fetch=False
        )


def get_all_events(db):
    return db.get_shower_events_between(
        os.environ['SOLAR_FORECAST_SHOWER_NAME'],
        '2000-01-01T00:00:00+00:00', '2100-01-01T00:00:00+00:00'
    )


def count_var_matrices(db):
    return db.query_one(
        "SELECT COUNT(*) FROM model.var_matrices WHERE matrix_type = %s",
        (MATRIX_TYPE_SHOWER_NUDGE,)
    )[0]


def setup_isolated_db():
    """Recreate the isolated database with schema + minimal recorder tables."""
    import psycopg2
    from solar_forecast.config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD

    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                            password=DB_PASSWORD, database='postgres')
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS solar_test_shower")
        cur.execute("CREATE DATABASE solar_test_shower")
    conn.close()

    with DatabaseClient() as db:
        schema = open(os.path.join(project_dir, 'db', 'schema.sql')).read()
        with db.connection.cursor() as cur:
            cur.execute(schema)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS states_meta (
                    metadata_id SERIAL PRIMARY KEY,
                    entity_id VARCHAR(255) UNIQUE NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS states (
                    state_id BIGSERIAL PRIMARY KEY,
                    metadata_id INTEGER NOT NULL,
                    state VARCHAR(255),
                    last_updated_ts DOUBLE PRECISION NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_states_meta_ts
                    ON states(metadata_id, last_updated_ts)
            """)
            cur.execute(
                "INSERT INTO states_meta (entity_id) VALUES (%s)",
                (os.environ['SOLAR_FORECAST_SHOWER_ENTITY'],)
            )
        db.connection.commit()


def run_integration_tests():
    from solar_forecast.processing.shower_learner import ShowerLearner

    # Fake clock: learning runs use T1, then T2 = T1 + 3h
    real_now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    T1 = real_now
    T2 = real_now + timedelta(hours=3)

    # Shower history (all UTC, relative to the fake clock):
    #   tA at T1-37h, chatter at tA+15min (must be debounced)
    #   tB at T1-26h, tC at T1-11h (well separated: no double-shower
    #   evidence, so the refractory nudge must stay negative)
    #   tD at T1+1h  (new in the incremental run)
    tA = T1 - timedelta(hours=37)
    tB = T1 - timedelta(hours=26)
    tC = T1 - timedelta(hours=11)
    tD = T1 + timedelta(hours=1)
    history_start = T1 - timedelta(hours=70)

    print("\n=== Integration: setting up isolated database ===")
    setup_isolated_db()

    with DatabaseClient() as db:
        seed_states(db, [
            (history_start, 'unknown'),
            (history_start + timedelta(minutes=1), 'off'),
            (tA, 'on'), (tA + timedelta(minutes=10), 'off'),
            (tA + timedelta(minutes=15), 'on'),   # chatter
            (tA + timedelta(minutes=23), 'off'),
            (tB, 'on'), (tB + timedelta(minutes=11), 'off'),
            (tC, 'on'), (tC + timedelta(minutes=12), 'off'),
        ])

    print("\n=== Integration run 1: bootstrap (fake now = T1) ===")
    learner = ShowerLearner()
    learner._now = lambda: T1
    r1 = learner.update()
    check("status completed", r1['status'] == 'completed', str(r1))
    check("nudge bootstrap ran", r1.get('nudge_bootstrap') is True)

    with DatabaseClient() as db:
        events = get_all_events(db)
        check("3 debounced events stored", len(events) == 3,
              f"got {len(events)}")
        check("chatter edge debounced", all(
            (events[i + 1] - events[i]) >= timedelta(minutes=SHOWER_DEBOUNCE_MINUTES)
            for i in range(len(events) - 1)))

        prob = db.get_shower_probability(os.environ['SOLAR_FORECAST_SHOWER_NAME'])
        vec = prob['p_shower_168h']
        check("vector length 168", len(vec) == 168)
        nonzero = [i for i, p in enumerate(vec) if p > 0]
        expected = {t.weekday() * 24 + t.hour for t in (tA, tB, tC)}
        check("3 non-zero slots at UTC weekday*24+hour",
              set(nonzero) == expected,
              f"got {nonzero}, expected {expected}")

        matrix = db.get_latest_var_matrix(MATRIX_TYPE_SHOWER_NUDGE)
        check("shower_nudge_v1 snapshot written", matrix is not None)
        pay = matrix['payload']
        check("snapshot has all 3 nudge cells",
              set(pay['nudges'].keys()) == set(nudge_model.BLOCK_NAMES),
              str(pay['nudges'].keys()))
        check("snapshot config has all constants",
              {'n0', 'alpha_min', 'alpha_max', 'k_shrink'} <= set(pay['config'].keys()),
              str(pay['config']))
        check("bootstrap replay used reduced alpha_max",
              pay['config']['alpha_max'] == SHOWER_NUDGE_BOOTSTRAP_ALPHA_MAX,
              str(pay['config']))
        check("means_ref points at the saved means row",
              pay['means_ref'] == (T1 - timedelta(hours=1)).isoformat(),
              f"got {pay['means_ref']}")
        nu1, nu2, nu3 = (pay['nudges'][n] for n in nudge_model.BLOCK_NAMES)
        check("nu_1 fired during replay with negative value (no double showers)",
              nu1['n'] >= 6 and nu1['value'] < 0,
              f"n={nu1['n']}, value={nu1['value']:+.4f}")
        check("nu_2 fired during replay",
              nu2['n'] >= 12, f"n={nu2['n']}")
        check("cells carry n/last_fired metadata",
              all('n' in c and 'last_fired_at' in c
                  for c in pay['nudges'].values()))
        lag_rows = db.query_one(
            "SELECT COUNT(*) FROM shower_correlation_lag")[0]
        check("shower_correlation_lag stays empty (no writes)",
              lag_rows == 0, f"got {lag_rows} rows")

    print("\n=== Integration run 2: idempotency (fake now = T1) ===")
    learner2 = ShowerLearner()
    learner2._now = lambda: T1
    r2 = learner2.update()
    check("status up_to_date", r2['status'] == 'up_to_date', str(r2))
    with DatabaseClient() as db:
        check("no extra snapshot on up-to-date rerun",
              count_var_matrices(db) == 1, f"got {count_var_matrices(db)}")
        prob2 = db.get_shower_probability(os.environ['SOLAR_FORECAST_SHOWER_NAME'])
        check("vector unchanged", prob2['p_shower_168h'] == vec)

    print("\n=== Integration run 3: incremental (fake now = T2) ===")
    with DatabaseClient() as db:
        seed_states(db, [
            (tD, 'on'), (tD + timedelta(minutes=9), 'off'),
        ])

    learner3 = ShowerLearner()
    learner3._now = lambda: T2
    r3 = learner3.update()
    check("status completed", r3['status'] == 'completed', str(r3))
    check("3 hours processed", r3['hours_processed'] == 3,
          str(r3.get('hours_processed')))
    check("no bootstrap in incremental run",
          r3.get('nudge_bootstrap') is False)

    with DatabaseClient() as db:
        events = get_all_events(db)
        check("4 events after incremental", len(events) == 4, f"got {len(events)}")

        prob3 = db.get_shower_probability(os.environ['SOLAR_FORECAST_SHOWER_NAME'])
        vec3 = prob3['p_shower_168h']
        slot_d = tD.weekday() * 24 + tD.hour
        check("new slot updated via EMA",
              abs(vec3[slot_d] - SHOWER_EMA_ALPHA) < 1e-9,
              f"slot {slot_d} = {vec3[slot_d]}")
        check("old slots untouched", all(vec3[i] == vec[i] for i in nonzero))

        check("new snapshot appended", count_var_matrices(db) == 2,
              f"got {count_var_matrices(db)}")
        matrix3 = db.get_latest_var_matrix(MATRIX_TYPE_SHOWER_NUDGE)
        pay3 = matrix3['payload']
        check("incremental snapshot uses production alpha_max",
              pay3['config']['alpha_max'] == 0.3, str(pay3['config']))
        check("means_ref advanced to the last learned hour",
              pay3['means_ref'] == (T2 - timedelta(hours=1)).isoformat(),
              f"got {pay3['means_ref']}")
        nu1_b, nu3_b = pay3['nudges']['block_0_3h'], pay3['nudges']['block_0_30h_neg']
        check("nu_1 grew (refractory fired after the new shower)",
              nu1_b['n'] > nu1['n'],
              f"{nu1['n']} -> {nu1_b['n']}")
        check("nu_3 unchanged (no 30h gap)",
              nu3_b['n'] == nu3['n'], f"{nu3['n']} -> {nu3_b['n']}")


def main():
    run_unit_tests()
    run_integration_tests()
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
