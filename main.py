#!/usr/bin/env python3
"""
Main entry point for the OWON SPE6103 power supply controller.

Usage:
    python main.py [options]       # Windows / virtual environment
    python3 main.py [options]      # Raspberry Pi OS / Linux

    See --help for all available options.
"""

import sys
from python_controller.cli import main

if __name__ == "__main__":
    sys.exit(main())
