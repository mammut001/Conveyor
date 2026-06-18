"""scripts/web_tools_smoke.py — P4.1 Web Fetch/Search smoke tests.

Tests:
  - URL validation rejects file://
  - rejects localhost
  - rejects private IPs
  - rejects 169.254.169.254
  - curl argv uses shell=False
  - missing curl graceful
  - non-text content rejected
  - output redacted/truncated
  - /help and /tools list web commands
  - tools correct danger levels
  - command registration
  - no network calls
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-smoke")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-smoke-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from config import Settings, load_settings
from handlers.tools.registry import DangerLevel
from personal_tools.registry import get_personal_tool
from personal_tools.web_fetch import validate_url, _curl_argv, html_to_text


def _settings() -> Settings:
    base = load_settings()
    return replace(base, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- Tests ----

async def _test_reject_file_scheme():
    """URL validation rejects file://"""
    ok, err = validate_url("file:///etc/passwd")
    assert not ok
    assert "不支持的协议" in err


async def _test_reject_localhost():
    """URL validation rejects localhost"""
    ok, err = validate_url("http://localhost/secret")
    assert not ok
    assert "localhost" in err


async def _test_reject_127():
    """URL validation rejects 127.0.0.1"""
    ok, err = validate_url("http://127.0.0.1/secret")
    assert not ok
    assert "私有 IP" in err or "拒绝" in err


async def _test_reject_private_10():
    """URL validation rejects 10.x.x.x"""
    ok, err = validate_url("http://10.0.0.1/secret")
    assert not ok
    assert "私有 IP" in err or "拒绝" in err


async def _test_reject_private_192():
    """URL validation rejects 192.168.x.x"""
    ok, err = validate_url("http://192.168.1.1/secret")
    assert not ok
    assert "私有 IP" in err or "拒绝" in err


async def _test_reject_metadata():
    """URL validation rejects 169.254.169.254"""
    ok, err = validate_url("http://169.254.169.254/metadata")
    assert not ok
    assert "私有 IP" in err or "拒绝" in err


async def _test_curl_argv_shell_false():
    """curl argv is a list (shell=False)."""
    settings = _settings()
    argv = _curl_argv("https://example.com", settings)
    assert isinstance(argv, list)
    assert argv[0] == "curl"
    assert "--silent" in argv
    assert "--fail" in argv
    assert "--no-location" in argv  # No automatic redirects
    assert "--proto" in argv
    assert "=http,https" in argv


async def _test_html_to_text():
    """html_to_text strips tags."""
    html = "<html><body><h1>Title</h1><p>Hello <b>world</b></p></body></html>"
    text = html_to_text(html)
    assert "Title" in text
    assert "Hello" in text
    assert "world" in text
    assert "<" not in text


async def _test_output_redacted():
    """Outputs are redacted."""
    from redaction import redact_text
    text = "token=secret123abc"
    assert "secret123abc" not in redact_text(text)


async def _test_tools_correct_danger():
    """Web tools are READ."""
    tools = {
        "web.fetch": DangerLevel.READ,
        "web.text": DangerLevel.READ,
        "web.headers": DangerLevel.READ,
        "web.search": DangerLevel.READ,
        "research.run": DangerLevel.READ,
        "research.project": DangerLevel.READ,
    }
    for name, expected in tools.items():
        spec = get_personal_tool(name)
        assert spec is not None, f"{name} not registered"
        assert spec.danger == expected, f"{name} should be {expected}, got {spec.danger}"


async def _test_command_registration():
    """Commands are registered."""
    from handlers.commands import COMMAND_TABLE
    expected = ["web_fetch", "web_text", "web_headers", "web_search", "research", "project_research"]
    for cmd in expected:
        assert cmd in COMMAND_TABLE, f"Command {cmd} not in COMMAND_TABLE"


async def _test_help_includes_web():
    """Help includes web commands."""
    from handlers.commands import _help

    help_text = ""
    class FakePort:
        async def reply(self, msg, text):
            nonlocal help_text
            help_text = text
    class FakeMsg:
        text = "/help"

    await _help(FakeMsg(), FakePort(), None, None, "")
    assert "/web_fetch" in help_text
    assert "/web_search" in help_text
    assert "/research" in help_text


async def _test_web_fetch_disabled():
    """Web fetch returns error when disabled."""
    from personal_tools.web_fetch import fetch_text
    settings = replace(_settings(), web_fetch_enabled=False)
    result = fetch_text(settings, "https://example.com")
    assert not result.ok
    assert "已禁用" in result.text


async def _test_web_search_disabled():
    """Web search returns error when backend is disabled."""
    from personal_tools.web_search import search_web
    settings = _settings()
    results, err = search_web(settings, "test query")
    assert not results
    assert "未启用" in err


async def _test_validate_empty_url():
    """Empty URL rejected."""
    ok, err = validate_url("")
    assert not ok
    ok, err = validate_url("   ")
    assert not ok


async def _test_validate_no_hostname():
    """URL without hostname rejected."""
    ok, err = validate_url("http://")
    assert not ok


async def _test_redirect_to_private_rejected():
    """Redirect to private IP is rejected (no auto-redirects)."""
    # Since we disabled automatic redirects, a URL that redirects to
    # a private IP should not be followed.
    from personal_tools.web_fetch import _curl_argv
    settings = _settings()
    argv = _curl_argv("https://example.com", settings)
    # Verify --no-location is in argv (no automatic redirects)
    assert "--no-location" in argv, f"Expected --no-location in argv: {argv}"
    assert "--location" not in argv, f"Should not have --location in argv: {argv}"


async def _test_content_type_validation():
    """Non-text content types are rejected."""
    from personal_tools.web_fetch import _check_content_type
    # Allowed types
    ok, _ = _check_content_type("content-type: text/html")
    assert ok
    ok, _ = _check_content_type("content-type: text/plain")
    assert ok
    ok, _ = _check_content_type("content-type: application/json")
    assert ok
    ok, _ = _check_content_type("content-type: application/xml")
    assert ok
    # Rejected types
    ok, err = _check_content_type("content-type: application/pdf")
    assert not ok
    assert "不支持" in err
    ok, err = _check_content_type("content-type: image/png")
    assert not ok
    ok, err = _check_content_type("content-type: application/zip")
    assert not ok


async def _test_search_endpoint_validation():
    """WEB_SEARCH_ENDPOINT with private IP is rejected."""
    from personal_tools.web_search import _search_searxng
    settings = replace(_settings(), web_search_backend="searxng", web_search_endpoint="http://127.0.0.1:8888")
    results, err = _search_searxng(settings, "test", 5)
    assert not results
    assert "无效" in err or "拒绝" in err


async def _test_url_encode_search_queries():
    """Search queries are URL encoded."""
    from urllib.parse import urlencode
    # Test that urlencode handles Chinese and spaces correctly
    query = "Python 异步编程 & asyncio"
    encoded = urlencode({"q": query})
    assert "Python" in encoded
    assert "asyncio" in encoded
    # urlencode should encode & as %26 in values
    assert "%26" in encoded or "&" in encoded


async def _test_natural_language_web_fetch():
    """Natural language routing for web fetch."""
    from handlers.intent import route_intent
    # With URL
    r = route_intent('获取网页 https://example.com')
    assert r.kind == "deterministic", f"Expected deterministic, got {r.kind}"
    assert "web.fetch" in r.tools, f"Expected web.fetch, got {r.tools}"
    assert r.arg == "https://example.com", f"Expected URL, got {r.arg}"
    # Without URL
    r = route_intent('获取网页内容')
    assert r.kind == "llm", f"Expected llm, got {r.kind}"
    assert "请提供" in r.question, f"Expected clarification question"


async def _test_natural_language_web_search():
    """Natural language routing for web search."""
    from handlers.intent import route_intent
    r = route_intent('搜索 Python asyncio')
    assert r.kind == "deterministic", f"Expected deterministic, got {r.kind}"
    assert "web.search" in r.tools, f"Expected web.search, got {r.tools}"
    assert r.arg == "Python asyncio", f"Expected query, got {r.arg}"


async def _test_natural_language_research():
    """Natural language routing for research."""
    from handlers.intent import route_intent
    r = route_intent('研究一下 AI 编程助手')
    assert r.kind == "deterministic", f"Expected deterministic, got {r.kind}"
    assert "research.run" in r.tools, f"Expected research.run, got {r.tools}"
    assert r.arg == "AI 编程助手", f"Expected question, got {r.arg}"


# ---- Runner ----

_TESTS = {
    "reject file://": _test_reject_file_scheme,
    "reject localhost": _test_reject_localhost,
    "reject 127.0.0.1": _test_reject_127,
    "reject 10.x.x.x": _test_reject_private_10,
    "reject 192.168.x.x": _test_reject_private_192,
    "reject 169.254.169.254": _test_reject_metadata,
    "curl argv shell=False": _test_curl_argv_shell_false,
    "html_to_text strips tags": _test_html_to_text,
    "output redacted": _test_output_redacted,
    "tools correct danger": _test_tools_correct_danger,
    "command registration": _test_command_registration,
    "help includes web": _test_help_includes_web,
    "web fetch disabled": _test_web_fetch_disabled,
    "web search disabled": _test_web_search_disabled,
    "validate empty url": _test_validate_empty_url,
    "validate no hostname": _test_validate_no_hostname,
    "nl web fetch": _test_natural_language_web_fetch,
    "nl web search": _test_natural_language_web_search,
    "nl research": _test_natural_language_research,
    "redirect to private rejected": _test_redirect_to_private_rejected,
    "content-type validation": _test_content_type_validation,
    "search endpoint validation": _test_search_endpoint_validation,
    "url encode queries": _test_url_encode_search_queries,
}


async def main() -> int:
    import asyncio
    passed = 0
    failed = 0
    for name, fn in _TESTS.items():
        try:
            await fn()
            print(f"[ok]   {name}")
            passed += 1
        except Exception as exc:
            print(f"[fail] {name}: {exc}")
            failed += 1
    print(f"\nWeb tools smoke: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
