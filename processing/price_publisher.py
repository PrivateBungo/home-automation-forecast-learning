"""
Rolling 15-Minute Price Publisher
=================================

Every 15 minutes, publishes the coming 168 hours as a single
672-element vector of 15-minute blocks (matching the solar 672-block
convention):

- Settled blocks (rest of today + tomorrow after the day-ahead
  auction): the REAL 15-minute auction prices from Energy-Charts
  (native quarterly settlement data, token-free), variance = 0.
- Unsettled blocks: the daily hourly forecast (price_forecast), each
  hourly mean/variance repeated 4 times - the price is assumed
  constant within the hour, but represented at 15-min resolution.

The daily forecast runs once per day after the auction settles
(gated on 13:00 Europe/Amsterdam); this publisher only assembles
and re-publishes, so it is fast (~1-2s, one HTTP call).

Output: price_forecast_15min row per 15-minute run, wholesale
EUR/kWh, all times UTC. Rows older than PRICE_PUBLISHER_RETENTION_DAYS
are pruned each run.
"""

import logging
import time
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from solar_forecast.config import (
    ENERGY_CHARTS_PRICE_URL,
    PRICE_FORECAST_ZONE,
    PRICE_PUBLISHER_BLOCKS,
    PRICE_PUBLISHER_RETENTION_DAYS,
)
from solar_forecast.db.client import DatabaseClient

logger = logging.getLogger(__name__)

MWH_TO_KWH = 0.001
FETCH_RETRIES = 3
FETCH_TIMEOUT_S = 60


class PricePublisher:
    """Publishes the rolling 168h price vector at 15-min resolution."""

    def __init__(self):
        self.db = DatabaseClient()

    # ============================================================
    # MAIN ENTRY POINT
    # ============================================================

    def update(self, test_mode: bool = False) -> Dict[str, Any]:
        """Build and store the rolling 672-block price vector.

        Args:
            test_mode: If True, print to terminal, no database writes.

        Returns:
            Summary dict of the publish run.
        """
        now = pd.Timestamp.now(tz='UTC').floor('15min')
        start = now + timedelta(minutes=15)  # first block: the coming one

        with self.db as db:
            daily = db.get_latest_price_forecast()
        if daily is None:
            logger.info("No daily price forecast stored yet; "
                        "publisher waits for the first daily run")
            return {'status': 'skipped', 'reason': 'no_daily_forecast'}

        published = self._fetch_published_15min(start)
        mean_arr, var_arr, n_published, n_extended = self._build_vector(
            start, daily, published)

        summary = {
            'status': 'ok',
            'issued_at': now.isoformat(),
            'start_utc': start.isoformat(),
            'horizon_blocks': PRICE_PUBLISHER_BLOCKS,
            'published_blocks': n_published,
            'forecast_blocks': PRICE_PUBLISHER_BLOCKS - n_published,
            'extended_blocks': n_extended,
            'daily_issued_at': pd.Timestamp(daily['issued_at']).isoformat()
        }

        if test_mode:
            logger.info(f"TEST MODE: would publish {PRICE_PUBLISHER_BLOCKS} "
                        f"blocks ({n_published} settled, "
                        f"{n_extended} beyond daily horizon)")
        else:
            with self.db as db:
                db.save_price_forecast_15min(
                    issued_at=now.isoformat(),
                    start_utc=start.isoformat(),
                    horizon_blocks=PRICE_PUBLISHER_BLOCKS,
                    mean_eur_kwh=[float(v) for v in mean_arr],
                    variance_eur_kwh2=[float(v) for v in var_arr],
                    published_blocks=n_published
                )
                deleted = db.prune_price_forecast_15min(
                    PRICE_PUBLISHER_RETENTION_DAYS)
            logger.info(
                f"Published 672-block price vector: issued_at={now.isoformat()}, "
                f"{n_published} settled, {n_extended} beyond horizon, "
                f"pruned={deleted}")
        return summary

    # ============================================================
    # PUBLISHED (SETTLED) 15-MINUTE PRICES
    # ============================================================

    def _fetch_published_15min(self, start: pd.Timestamp) -> pd.Series:
        """Fetch settled day-ahead prices at native 15-min resolution.

        Energy-Charts returns the published day-ahead series
        (today + tomorrow) with quarterly timestamps; everything in
        the response is real auction data. EUR/MWh -> EUR/kWh.
        """
        params = {
            'bzn': PRICE_FORECAST_ZONE,
            'start': start.strftime('%Y-%m-%d'),
            'end': (start + pd.Timedelta(days=2)).strftime('%Y-%m-%d'),
        }
        last_exc: Optional[Exception] = None
        for attempt in range(FETCH_RETRIES):
            try:
                r = requests.get(ENERGY_CHARTS_PRICE_URL, params=params,
                                 timeout=FETCH_TIMEOUT_S,
                                 headers={'User-Agent':
                                          'solar_forecast price publisher'})
                r.raise_for_status()
                j = r.json()
                secs = j.get('unix_seconds')
                prices = j.get('price')
                if not secs or not prices:
                    raise ValueError('empty response')
                idx = pd.to_datetime(secs, unit='s', utc=True)
                s = pd.Series(
                    np.asarray(prices, dtype=float) * MWH_TO_KWH,
                    index=idx).dropna()
                s = s[~s.index.duplicated(keep='last')].sort_index()
                logger.info(f"Fetched {len(s)} settled price points "
                            f"({s.index[-1].isoformat() if len(s) else 'none'})")
                return s
            except (requests.RequestException, ValueError) as e:
                last_exc = e
                logger.warning(f"Energy-Charts fetch attempt {attempt + 1} "
                               f"failed: {e}")
                time.sleep(2)
        raise RuntimeError(
            f"Could not fetch settled prices from Energy-Charts: {last_exc}")

    # ============================================================
    # VECTOR ASSEMBLY
    # ============================================================

    def _build_vector(
        self,
        start: pd.Timestamp,
        daily: Dict[str, Any],
        published: pd.Series
    ) -> Tuple[np.ndarray, np.ndarray, int, int]:
        """Assemble the rolling 672-block vector.

        Settled blocks take the real 15-min auction price (variance 0).
        Unsettled blocks take the daily hourly forecast repeated 4x.
        Blocks beyond the daily forecast horizon extend its last hour.
        """
        fc_start = self._to_utc_timestamp(daily['start_utc'])
        fc_mean = np.asarray(daily['mean_eur_kwh'], dtype=float)
        fc_var = np.asarray(daily['variance_eur_kwh2'], dtype=float)
        fc_len = len(fc_mean)

        mean_arr = np.empty(PRICE_PUBLISHER_BLOCKS)
        var_arr = np.empty(PRICE_PUBLISHER_BLOCKS)
        n_published = 0
        n_extended = 0

        for i in range(PRICE_PUBLISHER_BLOCKS):
            t = start + pd.Timedelta(minutes=15 * i)
            if t in published.index:
                mean_arr[i] = float(published.loc[t])
                var_arr[i] = 0.0
                n_published += 1
                continue
            # unsettle: hourly forecast value, repeated 4x per hour
            h_idx = int((t.floor('h') - fc_start) / pd.Timedelta(hours=1))
            if h_idx < 0:
                h_idx = 0
            elif h_idx >= fc_len:
                h_idx = fc_len - 1
                n_extended += 1
            mean_arr[i] = fc_mean[h_idx]
            var_arr[i] = fc_var[h_idx]

        return mean_arr, var_arr, n_published, n_extended

    @staticmethod
    def _to_utc_timestamp(value: Any) -> pd.Timestamp:
        """Normalize a DB timestamp to UTC (defensive: may be naive)."""
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize('UTC')
        return ts.tz_convert('UTC')
