"""Opt-in acceptance checks reuse explicit synthetic fixture builders from the unit suite."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
