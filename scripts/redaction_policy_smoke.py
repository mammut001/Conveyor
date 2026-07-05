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
    
    # Test expanded redaction patterns
    t_github = "github token ghp_1234567890abcdef1234567890abcdef"
    red_github = redact_text(t_github)
    results.append(CheckResult("github_token_redacts", "ghp_[REDACTED]" in red_github and "123456" not in red_github, f"got={red_github}"))

    t_aws = "aws key AKIA1234567890ABCDEF"
    red_aws = redact_text(t_aws)
    results.append(CheckResult("aws_key_redacts", "AKIA[REDACTED]" in red_aws and "123456" not in red_aws, f"got={red_aws}"))

    t_google_ya = "google oauth ya29.abcdefghijklmnopqrstuvwxyz12345"
    red_google_ya = redact_text(t_google_ya)
    results.append(CheckResult("google_ya_redacts", "ya29.[REDACTED]" in red_google_ya and "abcdef" not in red_google_ya, f"got={red_google_ya}"))

    t_google_aiza = "google api AIzaSy1234567890abcdef1234567890abcdef"
    red_google_aiza = redact_text(t_google_aiza)
    results.append(CheckResult("google_aiza_redacts", "AIza[REDACTED]" in red_google_aiza and "123456" not in red_google_aiza, f"got={red_google_aiza}"))

    t_feishu_cli = "feishu app cli_a1b2c3d4e5f6g7h8"
    red_feishu_cli = redact_text(t_feishu_cli)
    results.append(CheckResult("feishu_cli_redacts", "cli_[REDACTED]" in red_feishu_cli and "a1b2c3" not in red_feishu_cli, f"got={red_feishu_cli}"))

    # New broader cli_ checks
    t_cli_short = "cli_12345678"
    red_cli_short = redact_text(t_cli_short)
    results.append(CheckResult("cli_short_redacts", red_cli_short == "cli_[REDACTED]", f"got={red_cli_short}"))

    t_cli_mixed = "cli_ABCdef_123456"
    red_cli_mixed = redact_text(t_cli_mixed)
    results.append(CheckResult("cli_mixed_redacts", red_cli_mixed == "cli_[REDACTED]", f"got={red_cli_mixed}"))

    t_cli_normal = "this client is a cyclist"
    red_cli_normal = redact_text(t_cli_normal)
    results.append(CheckResult("cli_normal_unredacted", red_cli_normal == t_cli_normal, f"got={red_cli_normal}"))

    t_feishu_t = "feishu token t-1234567890abcdef1234567890abcdef"
    red_feishu_t = redact_text(t_feishu_t)
    results.append(CheckResult("feishu_t_redacts", "t-[REDACTED]" in red_feishu_t and "123456" not in red_feishu_t, f"got={red_feishu_t}"))

    t_anthropic = "anthropic key sk-ant-api1234567890abcdef1234567890abcdef"
    red_anthropic = redact_text(t_anthropic)
    results.append(CheckResult("anthropic_redacts", "sk-ant-api[REDACTED]" in red_anthropic and "123456" not in red_anthropic, f"got={red_anthropic}"))

    # Test non-redacting of token_count and other metric fields
    metrics_text = "token_count: 150, total_tokens: 300, input_tokens = 100"
    red_metrics = redact_text(metrics_text)
    results.append(CheckResult("usage_metrics_not_redacted", "150" in red_metrics and "300" in red_metrics and "100" in red_metrics, f"got={red_metrics}"))
    
    fake_environ = {
        "HOME": "/home/user",
        "PATH": "/usr/bin:/bin",
        "USER": "user",
        "SECRET_ENV": "leak",
        "OPENAI_API_KEY": "sk-key",
        "MINIMAX_API_KEY": "m-key",
        "CONVEYOR_DESKTOP_AGENT_TOKEN": "desktop-token",
        "UNUSED_VAR": "value",
        "GMAIL_APP_PASSWORD": "gmail-password",
        "GITHUB_TOKEN": "gh-token",
        "GOOGLE_TOKEN_PATH": "/path/to/token",
        "MY_CUSTOM_SECRET": "custom-secret",
    }
    child_env = child_env_from(fake_environ)
    results.append(CheckResult("child_env_preserves_home", child_env.get("HOME") == "/home/user", f"got={child_env}"))
    results.append(CheckResult("child_env_preserves_openai", child_env.get("OPENAI_API_KEY") == "sk-key", f"got={child_env}"))
    results.append(CheckResult("child_env_preserves_minimax", child_env.get("MINIMAX_API_KEY") == "m-key", f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_secret_env", "SECRET_ENV" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_unused_var", "UNUSED_VAR" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_desktop_token", "CONVEYOR_DESKTOP_AGENT_TOKEN" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_gmail_password", "GMAIL_APP_PASSWORD" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_github_token", "GITHUB_TOKEN" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_google_token", "GOOGLE_TOKEN_PATH" not in child_env, f"got={child_env}"))
    results.append(CheckResult("child_env_excludes_custom_secret_by_default", "MY_CUSTOM_SECRET" not in child_env, f"got={child_env}"))
    
    custom_environ = {
        **fake_environ,
        "CONVEYOR_CHILD_ENV_EXTRA_PREFIXES": "MY_CUSTOM_",
    }
    custom_child_env = child_env_from(custom_environ)
    results.append(CheckResult("child_env_allows_custom_prefix", custom_child_env.get("MY_CUSTOM_SECRET") == "custom-secret", f"got={custom_child_env}"))
    
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
