#!/usr/bin/env python3
"""Legacy wrapper for seeding Agent Supervisor skill into Conveyor skills store."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.seed_agent_supervisor_skill import main

if __name__ == "__main__":
    sys.exit(main())
