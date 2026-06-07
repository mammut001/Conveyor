"""runner/_paths.py — split out of runner.py.

The original runner.py was 2005 lines and 5 big
responsibilities. This file is one slice.

runner/core.py attaches each function on this module
to the CodexRunner class as a method at import time,
so callers see the same public surface.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
RUNNER_HOME = Path(__file__).resolve().parent
