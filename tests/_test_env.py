"""tests/_test_env.py — make unit tests runnable without a configured env.

Several tests call ``config.load_settings()``, which requires the core
variables below. CI exports them; locally they are often unset, which made
the suite fail for reasons unrelated to the code under test. Import this
module first in such tests: it fills in isolated temp-directory defaults
and never overrides values that are already set.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="conveyor-unittest-"))

_DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_ALLOWED_USER_ID": "12345",
    "CODEX_WORKSPACE_ROOT": str(_ROOT / "workspace"),
    "CODEX_TASK_ROOT": str(_ROOT / "tasks"),
    "CODEX_MEMORY_ROOT": str(_ROOT / "memory"),
}

for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)
for _key in ("CODEX_WORKSPACE_ROOT", "CODEX_TASK_ROOT", "CODEX_MEMORY_ROOT"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)
