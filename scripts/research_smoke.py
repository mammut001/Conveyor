"""scripts/research_smoke.py — P4.1 Research smoke tests.

Tests:
  - missing web search backend graceful
  - fake search results normalize
  - fake fetch creates evidence pack
  - research uses only READ tools
  - project_research degrades without active project
  - no network calls
  - output redacted
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

import tempfile
from config import Settings, load_settings
from handlers.tools.registry import DangerLevel
from personal_tools.registry import get_personal_tool
from personal_tools.web_search import SearchResult, format_results, _normalize_result


def _settings(tmp_path: Path | None = None) -> Settings:
    base = load_settings()
    if tmp_path:
        return replace(base, codex_memory_root=tmp_path, telegram_allowed_user_id=12345, user_timezone="UTC")
    return replace(base, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- Tests ----

async def _test_search_disabled_graceful():
    """Web search returns empty when backend is disabled."""
    from personal_tools.web_search import search_web
    settings = _settings()
    results, err = search_web(settings, "test query")
    assert not results
    assert "未启用" in err


async def _test_normalize_result():
    """Normalize raw search result."""
    raw = {
        "title": "Test Title",
        "url": "https://example.com",
        "snippet": "Test snippet",
    }
    result = _normalize_result(raw, 1, "test")
    assert result.title == "Test Title"
    assert result.url == "https://example.com"
    assert result.snippet == "Test snippet"
    assert result.source == "test"
    assert result.rank == 1


async def _test_normalize_result_alt_keys():
    """Normalize result with alternative keys (link/description)."""
    raw = {
        "title": "Test",
        "link": "https://example.com",
        "description": "Description text",
    }
    result = _normalize_result(raw, 2, "brave")
    assert result.url == "https://example.com"
    assert result.snippet == "Description text"


async def _test_format_results():
    """Format results for display."""
    results = [
        SearchResult("Title 1", "https://a.com", "Snippet 1", "test", 1),
        SearchResult("Title 2", "https://b.com", "Snippet 2", "test", 2),
    ]
    text = format_results(results)
    assert "Title 1" in text
    assert "https://a.com" in text
    assert "Title 2" in text


async def _test_format_results_empty():
    """Format empty results."""
    text = format_results([])
    assert "无搜索结果" in text


async def _test_research_only_read_tools():
    """Research tools are READ-only."""
    tools = {
        "research.run": DangerLevel.READ,
        "research.project": DangerLevel.READ,
    }
    for name, expected in tools.items():
        spec = get_personal_tool(name)
        assert spec is not None, f"{name} not registered"
        assert spec.danger == expected, f"{name} should be {expected}, got {spec.danger}"


async def _test_research_degrades_no_search():
    """Research degrades when search backend is disabled."""
    from personal_tools.research import research_collect
    settings = _settings()
    result = research_collect(settings, "test question")
    assert not result.ok
    assert "搜索失败" in result.text


async def _test_project_research_no_project():
    """project_research degrades without active project."""
    from personal_tools.research import project_research_collect
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = project_research_collect(settings, "op1", "test question")
        assert not result.ok
        assert "搜索失败" in result.text


async def _test_project_research_empty_question():
    """project_research rejects empty question."""
    from personal_tools.research import project_research_collect
    settings = _settings()
    result = project_research_collect(settings, "op1", "")
    assert not result.ok
    assert "用法" in result.text


async def _test_evidence_pack():
    """Evidence pack builds correctly."""
    from personal_tools.research import EvidenceItem, _build_evidence_pack
    evidence = [
        EvidenceItem("Title 1", "https://a.com", "Snippet 1", "Text 1"),
        EvidenceItem("Title 2", "https://b.com", "Snippet 2", "Text 2"),
    ]
    pack = _build_evidence_pack(evidence)
    assert "Title 1" in pack
    assert "https://a.com" in pack
    assert "Text 1" in pack
    assert "来源 1" in pack
    assert "来源 2" in pack


async def _test_dedupe_domains():
    """Domain deduplication works."""
    from personal_tools.research import _dedupe_domains
    results = [
        SearchResult("A", "https://example.com/1", "S1", "test", 1),
        SearchResult("B", "https://example.com/2", "S2", "test", 2),
        SearchResult("C", "https://other.com/1", "S3", "test", 3),
    ]
    deduped = _dedupe_domains(results, 10)
    assert len(deduped) == 2
    assert deduped[0].url == "https://example.com/1"
    assert deduped[1].url == "https://other.com/1"


async def _test_output_redacted():
    """Outputs are redacted."""
    from redaction import redact_text
    text = "token=secret123abc"
    assert "secret123abc" not in redact_text(text)


async def _test_research_returns_hybrid_prompt():
    """Research returns [HYBRID_PROMPT] marker for Codex synthesis."""
    from personal_tools.research import research_collect
    # Mock search_web to return fake results
    from unittest.mock import patch, MagicMock
    from personal_tools.web_search import SearchResult
    
    fake_results = [
        SearchResult("Test Title", "https://example.com", "Test snippet", "test", 1),
    ]
    
    with patch("personal_tools.research.search_web", return_value=(fake_results, "")):
        with patch("personal_tools.research.fetch_text", return_value=MagicMock(ok=True, text="Test content")):
            settings = _settings()
            result = research_collect(settings, "test question")
            assert result.ok, f"Expected ok=True, got {result.ok}"
            assert result.text.startswith("[HYBRID_PROMPT]"), f"Expected [HYBRID_PROMPT] prefix, got {result.text[:50]}"


async def _test_project_research_returns_hybrid_prompt():
    """Project research returns [HYBRID_PROMPT] marker for Codex synthesis."""
    from personal_tools.research import project_research_collect
    from unittest.mock import patch, MagicMock
    from personal_tools.web_search import SearchResult
    
    fake_results = [
        SearchResult("Test Title", "https://example.com", "Test snippet", "test", 1),
    ]
    
    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        with patch("personal_tools.research.search_web", return_value=(fake_results, "")):
            with patch("personal_tools.research.fetch_text", return_value=MagicMock(ok=True, text="Test content")):
                result = project_research_collect(settings, "op1", "test question")
                assert result.ok, f"Expected ok=True, got {result.ok}"
                assert result.text.startswith("[HYBRID_PROMPT]"), f"Expected [HYBRID_PROMPT] prefix, got {result.text[:50]}"


# ---- Runner ----

_TESTS = {
    "search disabled graceful": _test_search_disabled_graceful,
    "normalize result": _test_normalize_result,
    "normalize result alt keys": _test_normalize_result_alt_keys,
    "format results": _test_format_results,
    "format results empty": _test_format_results_empty,
    "research only READ tools": _test_research_only_read_tools,
    "research degrades no search": _test_research_degrades_no_search,
    "project_research no project": _test_project_research_no_project,
    "project_research empty question": _test_project_research_empty_question,
    "evidence pack": _test_evidence_pack,
    "dedupe domains": _test_dedupe_domains,
    "output redacted": _test_output_redacted,
    "research returns hybrid prompt": _test_research_returns_hybrid_prompt,
    "project research returns hybrid prompt": _test_project_research_returns_hybrid_prompt,
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
    print(f"\nResearch smoke: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
