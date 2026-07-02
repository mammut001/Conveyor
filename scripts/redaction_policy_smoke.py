#!/usr/bin/env python3
"""redaction_policy_smoke.py — unit tests for secrets policy and redaction.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results
from security.secrets import redact_text, redact_obj, child_env_from, is_sensitive_key

def test_redaction_policy() -> list[CheckResult]:
    results = []
    
    # 1. Sensitive object keys redact
    obj = {
        "api_key": "sk-1234567890abcdef123456",
        "username": "bob",
        "my_token": "token-value",
        "password": "pass",
    }
    redacted = redact_obj(obj)
    results.append(CheckResult("api_key_key_redacts", redacted.get("api_key") == "[REDACTED]", f"got={redacted}"))
    results.append(CheckResult("username_preserved", redacted.get("username") == "bob", f"got={redacted}"))
    results.append(CheckResult("my_token_key_redacts", redacted.get("my_token") == "[REDACTED]", f"got={redacted}"))
    results.append(CheckResult("password_key_redacts", redacted.get("password") == "[REDACTED]", f"got={redacted}"))
    
    # 2. Sensitive values in raw text redact
    text = "Here is my openai key: sk-abcdefghijklmnopqrstuvwx and telegram bot12345:ABCdefghijkLMnoPQRstuvwx"
    red_text = redact_text(text)
    results.append(CheckResult("sk_key_in_text_redacts", "sk-" not in red_text or "[REDACTED]" in red_text, f"got={red_text}"))
    results.append(CheckResult("telegram_token_in_text_redacts", "bot12345:" not in red_text or "[REDACTED]" in red_text, f"got={red_text}"))
    
    # 3. Child env filtering
    fake_environ = {
        "HOME": "/home/user",
        "PATH": "/usr/bin:/bin",
        "USER": "user",
        "SECRET_ENV": "leak",
        "OPENAI_API_KEY": "sk-key",
        "MINIMAX_API_KEY": "m-key",
        "CONVEYOR_DESKTOP_AGENT_TOKEN": "desktop-token",
        "UNUSED_VAR": "value",
    }
    child_env = child_env_from(fake_environ)
    results.append(CheckResult("child_env_preserves_home", child_env.get("HOME") == "/home/user", f"got={child_env}"))
    results.append(CheckResult("child_env_preserves_openai", child_env.get("OPENAI_API_KEY") == "sk-key", f"got={child_env}"))
    results.append(CheckResult("child_env_preserves_minimax", child_env.get("MINIMAX_API_KEY") == "m-key", f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_secret_env", "SECRET_ENV" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_unused_var", "UNUSED_VAR" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_desktop_token", "CONVEYOR_DESKTOP_AGENT_TOKEN" not in child_env, f"got={child_env}"))
    
    # 4. conveyor_desktop_agent_token is sensitive
    results.append(CheckResult("desktop_agent_token_key_is_sensitive", is_sensitive_key("conveyor_desktop_agent_token"), ""))
    
    return results

def main() -> int:
    results = test_redaction_policy()
    print_results(results)
    ok = all(r.ok for r in results)
    print("redaction policy smoke ok" if ok else "redaction policy smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
