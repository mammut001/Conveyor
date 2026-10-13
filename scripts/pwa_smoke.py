#!/usr/bin/env python3
"""Offline P3-5 PWA security smoke: authenticated routes, cache boundaries, push."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-p", "test_pwa.py", "-v"], cwd=str(ROOT)
    ))
