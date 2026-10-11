#!/usr/bin/env python3
"""
Run Script for Shower Behaviour Learning System
================================================

Wrapper script to run the shower behaviour learning system hourly.
Handles the Python path setup, mirroring run.py.

Usage:
    # Run hourly learning
    python3 run_shower.py

    # Run in test mode
    python3 run_shower.py --test

    # Force run immediately
    python3 run_shower.py --force
"""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(project_dir)
sys.path.insert(0, parent_dir)
sys.path.insert(0, project_dir)

# Import and run shower main
from solar_forecast.shower_main import main

if __name__ == '__main__':
    main()
