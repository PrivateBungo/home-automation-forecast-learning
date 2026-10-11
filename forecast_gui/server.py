#!/usr/bin/env python3
"""
Forecast GUI server
===================

Serves a single page that visualizes the next 168h of forecast data
from the shared Postgres database:

- Solar production (combined east+west) Gaussian plume:
  mean + P10/P90 band, from solar_combined_forecast (15-min blocks).
- NL day-ahead (APX) price Gaussian plume:
  mean + P10/P90 band, from price_forecast_15min (15-min blocks,
  republished every 15 minutes; settled blocks are the real auction
  prices). Falls back to the daily hourly price_forecast if no
  15-minute row exists yet.
- Shower probability bars per hour, from shower_probability_168h
  (weekly UTC profile, indexed by weekday*24 + hour).

Run with the existing venv:
    /home/gijs/venvs/forecast/bin/python /home/gijs/forecast_gui/server.py [port]

P10/P90 are mean -/+ Z * sqrt(variance) with Z = 1.2816 (80% central
mass), the same z-score the stored bands were built with.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2

# Same env overrides / defaults as solar_forecast.config
DB_HOST = os.environ.get('SOLAR_FORECAST_DB_HOST', '10.100.123.53')
DB_PORT = int(os.environ.get('SOLAR_FORECAST_DB_PORT', '5432'))
DB_USER = os.environ.get('SOLAR_FORECAST_DB_USER', 'postgres')
DB_PASSWORD = os.environ.get('SOLAR_FORECAST_DB_PASSWORD', '123GjH#@!')
DB_DATABASE = os.environ.get('SOLAR_FORECAST_DB_DATABASE', 'homeassistant')

Z80 = 1.2816
HORIZON_HOURS = 168
STATIC_DIR = Path(__file__).resolve().parent / 'static'


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _plume(times, mean, var):
    """mean/var series -> mean, p10, p90 lists."""
    p10, p90 = [], []
    for m, v in zip(mean, var):
        sd = Z80 * math.sqrt(max(float(v), 0.0))
        p10.append(m - sd)
        p90.append(m + sd)
    return {
        'times': [_ms(t) for t in times],
        'mean': [float(x) for x in mean],
        'p10': p10,
        'p90': p90,
    }


def fetch_data():
    now = datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=HORIZON_HOURS)

    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                            password=DB_PASSWORD, database=DB_DATABASE)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT issued_at, mean_kwh, variance_kwh2 "
                    "FROM solar_combined_forecast ORDER BY issued_at DESC LIMIT 1")
                solar_row = cur.fetchone()
                cur.execute(
                    "SELECT issued_at, start_utc, mean_eur_kwh, variance_eur_kwh2, "
                    "published_blocks "
                    "FROM price_forecast_15min ORDER BY issued_at DESC LIMIT 1")
                price_row = cur.fetchone()
                price_15min = price_row is not None
                if not price_15min:
                    # graceful fallback: daily hourly forecast
                    cur.execute(
                        "SELECT issued_at, start_utc, mean_eur_kwh, "
                        "variance_eur_kwh2, NULL "
                        "FROM price_forecast ORDER BY issued_at DESC LIMIT 1")
                    price_row = cur.fetchone()
                cur.execute(
                    "SELECT sensor, p_shower_168h, last_updated "
                    "FROM shower_probability_168h ORDER BY sensor")
                shower_rows = cur.fetchall()
    finally:
        conn.close()

    data = {'now_utc': _iso(now), 'window_start': _iso(start),
            'window_end': _iso(end), 'horizon_hours': HORIZON_HOURS,
            'z': Z80, 'solar': None, 'price': None, 'showers': []}

    # ---- solar: 15-min blocks from issued_at, clipped to [start, end) ----
    if solar_row:
        issued, mean, var = solar_row
        t0 = issued.replace(second=0, microsecond=0)
        t0 = t0.replace(minute=(t0.minute // 15) * 15)
        times, mm, vv = [], [], []
        for i, (m, v) in enumerate(zip(mean, var)):
            t = t0 + timedelta(minutes=15 * i)
            if start <= t < end:
                times.append(t)
                mm.append(float(m))
                vv.append(float(v))
        if times:
            data['solar'] = {'issued_at': _iso(issued),
                             'unit': 'kWh/15min', **_plume(times, mm, vv)}

    # ---- price: 15-min blocks from start_utc, clipped to [start, end) ----
    # Latest rolling vector first (real auction prices for settled
    # blocks, hourly forecast repeated 4x beyond settlement); daily
    # hourly forecast as fallback.
    if price_row:
        issued, p_start, mean, var, settled = price_row
        step_min = 15 if price_15min else 60
        times, mm, vv = [], [], []
        for i, (m, v) in enumerate(zip(mean, var)):
            t = p_start + timedelta(minutes=step_min * i)
            if start <= t < end:
                times.append(t)
                mm.append(float(m))
                vv.append(float(v))
        if times:
            price_data = {'issued_at': _iso(issued),
                          'unit': 'EUR/kWh', **_plume(times, mm, vv)}
            if settled is not None:
                price_data['settled_blocks'] = int(settled)
                price_data['resolution_min'] = 15
            data['price'] = price_data

    # ---- showers: weekly UTC profile mapped onto [start, end) ----
    for sensor, p_vec, last_upd in shower_rows:
        if not p_vec or len(p_vec) < 168:
            continue
        hours, probs = [], []
        for i in range(HORIZON_HOURS):
            t = start + timedelta(hours=i)
            idx = (t.weekday() * 24 + t.hour) % len(p_vec)
            hours.append(t)
            probs.append(float(p_vec[idx]))
        data['showers'].append({'sensor': sensor,
                                'last_updated': _iso(last_upd),
                                'times': [_ms(t) for t in hours],
                                'p': probs})
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._serve_file(STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
        elif self.path == '/api/data':
            try:
                payload = fetch_data()
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # DB down, etc.
                body = json.dumps({'error': str(exc)}).encode()
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_error(404)

    def _serve_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' %
                         (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                          fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'Forecast GUI on http://0.0.0.0:{port} (Ctrl-C to stop)')
    server.serve_forever()


if __name__ == '__main__':
    main()
