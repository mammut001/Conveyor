#!/usr/bin/env python3
"""Offline loopback smoke: HTTP, HTTPS CONNECT, DNS guard and taint approval."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    suite = unittest.TestLoader().discover(str(ROOT / "tests"),
                                            pattern="test_egress_*.py",
                                            top_level_dir=str(ROOT / "tests"))
    passed = unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()
    raise SystemExit(0 if passed else 1)
