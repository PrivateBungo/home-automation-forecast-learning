"""
Shower Nudge Model (log-odds)
=============================

Combined hour-by-hour shower probability model:

    logit(P_h) = logit(M_slot) + sum_i applied_nu_i
    applied_nu_i = nu_i * n_i / (n_i + k_shrink)     (only if block i is fired)
    P_h = sigmoid(logit(M_slot) + applied_sum)

M_slot is the existing 168h EMA probability vector (UTC weekday*24+hour).
The nudges are pooled across all days (day-of-week specificity lives in
the means only). A near-zero mean gates the nudges multiplicatively:
log-odds addition is odds multiplication, so a "never showers Wednesday
morning" slot cannot be resurrected by a shared positive nudge.

Blocks (conditions looked back from the start of the hour being predicted):

- block_0_3h      fired if a shower edge occurred in the last 3 hours
                  (refractory: repeated showers unlikely -> expect negative)
- block_6_18h     fired if a shower edge occurred 6-18 hours ago
                  (evening<->morning anti-correlation)
- block_0_30h_neg fired if NO shower edge in the last 30 hours
                  (occupancy/holiday gate -> expect strong negative)

Only the listed condition carries a stored nudge; the complement
contributes 0 to the applied sum.

Update rule (per completed hour H, after event ingest):

1. P_h computed from PRE-TICK state (old means, old nudges).
2. The mean slot is updated as usual (EMA, unchanged).
3. For each block whose window is fully in the past: if the block is
   in the fired state this hour (the listed condition holds):

       alpha_eff = clamp( max(1/(n_i + n0), alpha_min) * (1 + ramp_i), alpha_max )
       ramp_i    = min(dt_since_last_fired / typical_firing_interval, 3)
       nu_i     += alpha_eff * (y - P_h)      # gradient vs the COMBINED model
       n_i      += 1 ; last_fired_at = now

Note on "fired state": the update applies every hour the condition
holds (per-hour online gradient), not only on the not-fired -> fired
transition. The transition reading cannot satisfy the holiday-gate
behaviour: a strict transition fires exactly once per gap at
last_event + 30h, which is structurally a low-mean hour, so the gate
could never learn the required strong negative. Per-hour firing lets
the occupancy gate adapt continuously during a long absence (and
re-adapt after it) and is what makes the nudges converge like a
running average.

The (y - P_h) term is the surprise of the observation given the full
combined model: overlapping blocks co-adapt instead of double-counting,
and the mean and nudges never absorb the same evidence twice.

Numerical guards:
- M clipped to [0.005, 0.995] before logit
- applied nudge sum clipped to +-6
- final P clipped to [0.001, 0.999]

All math is pure (no DB access) so it is directly unit-testable.
"""

import math
from datetime import datetime, timedelta
from typing import Dict, Set, Optional, Any

from solar_forecast.config import (
    SHOWER_NUDGE_N0,
    SHOWER_NUDGE_ALPHA_MIN,
    SHOWER_NUDGE_ALPHA_MAX,
    SHOWER_NUDGE_BOOTSTRAP_ALPHA_MAX,
    SHOWER_NUDGE_K_SHRINK,
    SHOWER_NUDGE_SUM_CLIP,
    SHOWER_NUDGE_MEAN_CLIP_LOW,
    SHOWER_NUDGE_MEAN_CLIP_HIGH,
    SHOWER_NUDGE_P_CLIP_LOW,
    SHOWER_NUDGE_P_CLIP_HIGH
)

# ============================================================
# BLOCK DEFINITIONS
# ============================================================
# Window for hour H (predicted hour start): events in
# [H - max_hours, H - min_hours). Negated blocks fire on ABSENCE.
# event_hours: set of hour-start datetimes that contain a debounced edge.

NUDGE_BLOCKS = [
    {'name': 'block_0_3h', 'min_hours': 0, 'max_hours': 3, 'negated': False},
    {'name': 'block_6_18h', 'min_hours': 6, 'max_hours': 18, 'negated': False},
    {'name': 'block_0_30h_neg', 'min_hours': 0, 'max_hours': 30, 'negated': True},
]

BLOCK_NAMES = [b['name'] for b in NUDGE_BLOCKS]
MAX_LOOKBACK_HOURS = max(b['max_hours'] for b in NUDGE_BLOCKS) + 1  # +1 for transition check


class NudgeConfig:
    """Nudge model hyper-parameters (bootstrap replay uses a lower alpha_max)."""

    def __init__(self, n0=SHOWER_NUDGE_N0,
                 alpha_min=SHOWER_NUDGE_ALPHA_MIN,
                 alpha_max=SHOWER_NUDGE_ALPHA_MAX,
                 k_shrink=SHOWER_NUDGE_K_SHRINK):
        self.n0 = n0
        self.alpha_min = alpha_min
        self.alpha_max = alpha_max
        self.k_shrink = k_shrink

    def as_dict(self):
        return {
            'n0': self.n0,
            'alpha_min': self.alpha_min,
            'alpha_max': self.alpha_max,
            'k_shrink': self.k_shrink
        }

    @classmethod
    def bootstrap(cls):
        """Config for history replay: reduced alpha_max for stability."""
        return cls(alpha_max=SHOWER_NUDGE_BOOTSTRAP_ALPHA_MAX)


def new_state() -> Dict[str, Dict[str, Any]]:
    """Fresh nudge state: {block_name: {value, n, last_fired_at, first_fired_at}}."""
    return {
        name: {
            'value': 0.0,
            'n': 0,
            'last_fired_at': None,
            'first_fired_at': None
        }
        for name in BLOCK_NAMES
    }


def state_from_payload(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Parse nudge state from a shower_nudge_v1 payload."""
    state = new_state()
    for name, cell in payload.get('nudges', {}).items():
        if name not in state:
            continue
        state[name] = {
            'value': float(cell.get('value', 0.0)),
            'n': int(cell.get('n', 0)),
            'last_fired_at': _parse_dt(cell.get('last_fired_at')),
            'first_fired_at': _parse_dt(cell.get('first_fired_at'))
        }
    return state


def state_to_payload_section(state: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Serialize nudge state into the payload 'nudges' section."""
    return {
        name: {
            'value': cell['value'],
            'n': cell['n'],
            'last_fired_at': cell['last_fired_at'].isoformat()
                             if cell['last_fired_at'] else None,
            'first_fired_at': cell['first_fired_at'].isoformat()
                              if cell['first_fired_at'] else None
        }
        for name, cell in state.items()
    }


# ============================================================
# MATH HELPERS
# ============================================================

def logit(p: float) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def shrink(n: int, k: float) -> float:
    """Prediction-time shrinkage: cold cells contribute ~nothing."""
    return n / (n + k)


# ============================================================
# BLOCK FIRING
# ============================================================

def block_fired(block: Dict[str, Any],
                event_hours: Set[datetime],
                hour_start: datetime) -> bool:
    """Is the block's condition met for the hour starting at hour_start?"""
    lo = hour_start - timedelta(hours=block['max_hours'])
    hi = hour_start - timedelta(hours=block['min_hours'])
    # An event in hour k (covering [k, k+1)) is in [lo, hi) iff
    # k >= lo and k < hi  (integer-hour windows)
    hit = any(lo <= eh < hi for eh in event_hours)
    return (not hit) if block['negated'] else hit


def fired_flags(event_hours: Set[datetime],
                hour_start: datetime,
                known_from: Optional[datetime] = None) -> Dict[str, bool]:
    """
    Fired state of all blocks for hour_start.

    A block whose lookback window is not fully inside known data
    coverage (known_from) is reported as not fired; the complement
    contributes 0 to the model.
    """
    flags = {}
    for b in NUDGE_BLOCKS:
        if known_from is not None and \
                hour_start - timedelta(hours=b['max_hours']) < known_from:
            flags[b['name']] = False
            continue
        flags[b['name']] = block_fired(b, event_hours, hour_start)
    return flags


# ============================================================
# PREDICTION
# ============================================================

def predict(mean_p: float,
            state: Dict[str, Dict[str, Any]],
            fired: Dict[str, bool],
            cfg: NudgeConfig) -> float:
    """
    Combined probability for one hour.

    Args:
        mean_p: M_slot (pre-tick mean for this hour-of-week slot)
        state: nudge state
        fired: fired flags for this hour (unevaluable blocks -> False)
        cfg: config (k_shrink used)

    Returns:
        P_h in [0.001, 0.999]
    """
    m = logit(min(max(mean_p, SHOWER_NUDGE_MEAN_CLIP_LOW),
                  SHOWER_NUDGE_MEAN_CLIP_HIGH))
    applied = 0.0
    for b in NUDGE_BLOCKS:
        if not fired.get(b['name'], False):
            continue  # complement contributes 0
        cell = state[b['name']]
        applied += cell['value'] * shrink(cell['n'], cfg.k_shrink)
    applied = min(max(applied, -SHOWER_NUDGE_SUM_CLIP), SHOWER_NUDGE_SUM_CLIP)
    p = sigmoid(m + applied)
    return min(max(p, SHOWER_NUDGE_P_CLIP_LOW), SHOWER_NUDGE_P_CLIP_HIGH)


# ============================================================
# UPDATE
# ============================================================

def typical_firing_interval_h(cell: Dict[str, Any],
                              now: datetime) -> Optional[float]:
    """
    Typical interval between firings for a cell, in hours.

    Derived from first/last firing timestamps and the firing count.
    Returns None if unknown (never fired).
    """
    first = cell['first_fired_at']
    last = cell['last_fired_at']
    n = cell['n']
    if first is None or last is None:
        return None
    if n >= 2:
        interval = (last - first).total_seconds() / 3600.0 / (n - 1)
    else:
        interval = (now - first).total_seconds() / 3600.0
    return max(interval, 1.0)


def alpha_eff(cell: Dict[str, Any], now: datetime, cfg: NudgeConfig) -> float:
    """Adaptive step size: fresh cells learn fast, rare cells catch up."""
    base = max(1.0 / (cell['n'] + cfg.n0), cfg.alpha_min)

    ramp = 0.0
    if cell['last_fired_at'] is not None:
        dt_h = (now - cell['last_fired_at']).total_seconds() / 3600.0
        typical = typical_firing_interval_h(cell, now)
        if typical is not None and typical > 0:
            ramp = min(dt_h / typical, 3.0)

    return min(base * (1.0 + ramp), cfg.alpha_max)


def update(state: Dict[str, Dict[str, Any]],
           fired: Dict[str, bool],
           y: int,
           p_h: float,
           fired_at: datetime,
           cfg: NudgeConfig) -> Dict[str, Dict[str, Any]]:
    """
    Apply the update rule for one completed hour.

    Every block that is in the fired state this hour takes a gradient
    step against the combined model:

        nu_i += alpha_eff * (y - p_h)

    Because p_h already contains the mean AND all applied nudges, the
    step is the surprise of the observation given the full model:
    overlapping blocks co-adapt instead of double-counting, and once the
    mean alone explains the evidence the nudges stop absorbing it.
    There is no (1 - alpha) * nu_old decay term.

    Args:
        state: current nudge state (mutated and returned)
        fired: fired flags for this hour
        y: X of this hour (0/1)
        p_h: combined pre-tick prediction for this hour
        fired_at: timestamp of this completed hour (hour end)
        cfg: config

    Returns:
        Updated state (same dict, mutated).
    """
    for b in NUDGE_BLOCKS:
        name = b['name']
        if not fired.get(name, False):
            continue  # complement contributes 0 this hour
        cell = state[name]
        step = alpha_eff(cell, fired_at, cfg) * (y - p_h)
        cell['value'] += step
        cell['n'] += 1
        if cell['first_fired_at'] is None:
            cell['first_fired_at'] = fired_at
        cell['last_fired_at'] = fired_at
    return state


# ============================================================
# INTERNAL
# ============================================================

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)
