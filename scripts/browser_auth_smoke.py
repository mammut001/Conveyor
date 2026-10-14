#!/usr/bin/env python3
"""G4 offline smoke: no network or Chromium needed."""
import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "unittest", "-v",
        "tests.test_browser_auth_sessions.G4OfflineTests",
    ]))
