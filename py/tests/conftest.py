"""Tests for the snr_sweep package.

Run from the repo root:

    .venv/bin/python -m pytest py/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `snr_sweep` importable when tests are run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
