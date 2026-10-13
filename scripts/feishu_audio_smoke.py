#!/usr/bin/env python3
"""Offline Feishu voice smoke (fake Feishu resource API and STT)."""
from __future__ import annotations

import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "unittest", "discover", "-s", "tests",
        "-p", "test_feishu_audio.py", "-v",
    ]))
