#!/usr/bin/env python3
"""
Run Script for Backfill
=======================

Wrapper script to rebuild the learning tables from stored history.
Handles the Python path, same as run.py.

Usage:
    python3 run_backfill.py
"""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(project_dir)
sys.path.insert(0, parent_dir)
sys.path.insert(0, project_dir)

from solar_forecast.processing.backfill import main

if __name__ == '__main__':
    main()
