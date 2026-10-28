#!/usr/bin/env python3
"""Launcher used by services and the eris-cctv command (no PYTHONPATH needed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eriscctv.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
