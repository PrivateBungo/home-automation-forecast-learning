#!/usr/bin/env python3
"""
Run Script for Solar Forecast Learning System
=============================================

Wrapper script to run the solar forecast learning system.
This script handles the Python path setup for Python 3.13+.

Usage:
    # Run full pipeline
    python3 run.py
    
    # Run in test mode
    python3 run.py --test
    
    # Force run immediately
    python3 run.py --force
"""

import sys
import os

# Add the project directory to Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(project_dir)
sys.path.insert(0, parent_dir)
sys.path.insert(0, project_dir)

# Import and run main
from solar_forecast.main import main

if __name__ == '__main__':
    main()
