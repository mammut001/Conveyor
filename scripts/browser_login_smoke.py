#!/usr/bin/env python3
"""Offline G4 autofill policy, secret store and audit smoke."""
import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "unittest", "discover", "-s", "tests",
        "-p", "test_browser_autofill_login.py", "-v",
    ]))
