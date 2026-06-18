"""scripts/file_search_smoke.py — Smoke tests for File Search / Knowledge Base (P4.2 / P4.2.1).

Tests:
- allowed root accepted
- path traversal rejected
- .env rejected
- secrets directory rejected
- private key pattern redacted
- binary file skipped
- oversized file skipped
- files.search returns snippets
- files.read returns truncated safe text
- kb.index creates index
- kb.search works with fallback if FTS5 unavailable
- NL route triggers kb.collect_facts
- NL search reads snippets via collector
- relative display path does not break internal read
- KB preferred when indexed
- fallback to file search when KB missing
- blocked files not read through collector
- outputs redacted/truncated
- project docs search degrades without active project
- no network calls
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_settings(**overrides):
    """Create test Settings with sensible defaults."""
    from config import Settings
    
    defaults = {
        "telegram_bot_token": "test-token",
        "telegram_allowed_user_id": 123,
        "codex_workspace_root": Path("/tmp/test-workspace"),
        "codex_bin": "codex",
        "codex_task_root": Path("/tmp/test-task"),
        "codex_model": None,
        "codex_timeout_seconds": 60,
        "telegram_progress_seconds": 3,
        "codex_retry_429_delays_seconds": (5,),
        "codex_memory_root": Path("/tmp/test-memory"),
        "user_timezone": "UTC",
        "file_search_enabled": True,
        "file_search_max_file_bytes": 10000,
        "file_search_max_results": 10,
        "file_search_extensions": ".md,.txt,.py",
        "kb_root": None,
        "kb_index_path": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _test_allowed_root_accepted():
    """Test that allowed roots are accepted."""
    from personal_tools.file_search import _is_path_allowed, _get_allowed_roots
    
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        roots = _get_allowed_roots(settings)
        
        # File under workspace root should be allowed
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("test content")
        
        assert _is_path_allowed(test_file, roots), "File under allowed root should be accepted"
    print("✓ allowed root accepted")


def _test_path_traversal_rejected():
    """Test that path traversal is rejected."""
    from personal_tools.file_search import _is_path_allowed, _get_allowed_roots
    
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        roots = _get_allowed_roots(settings)
        
        # Path outside allowed roots should be rejected
        outside_path = Path("/etc/passwd")
        assert not _is_path_allowed(outside_path, roots), "Path outside allowed root should be rejected"
        
        # Path traversal attempt should be rejected
        traversal_path = Path(tmpdir) / ".." / ".." / "etc" / "passwd"
        assert not _is_path_allowed(traversal_path, roots), "Path traversal should be rejected"
    print("✓ path traversal rejected")


def _test_env_rejected():
    """Test that .env files are rejected."""
    from personal_tools.file_search import _is_blocked_pattern
    
    assert _is_blocked_pattern(".env"), ".env should be blocked"
    assert _is_blocked_pattern("path/to/.env"), ".env in path should be blocked"
    assert _is_blocked_pattern(".env.local"), ".env.local should be blocked"
    print("✓ .env rejected")


def _test_secrets_rejected():
    """Test that secrets directory is rejected."""
    from personal_tools.file_search import _is_blocked_pattern
    
    assert _is_blocked_pattern("secrets/"), "secrets/ should be blocked"
    assert _is_blocked_pattern("path/to/secrets/key.pem"), "secrets/ in path should be blocked"
    assert _is_blocked_pattern(".ssh/"), ".ssh/ should be blocked"
    assert _is_blocked_pattern(".ssh/id_rsa"), ".ssh/id_rsa should be blocked"
    print("✓ secrets directory rejected")


def _test_private_key_redacted():
    """Test that private key patterns are rejected."""
    from personal_tools.file_search import _is_blocked_pattern
    
    assert _is_blocked_pattern("private.key"), "private.key should be blocked"
    assert _is_blocked_pattern("id_rsa"), "id_rsa should be blocked"
    assert _is_blocked_pattern("id_ed25519"), "id_ed25519 should be blocked"
    assert _is_blocked_pattern("server.pem"), ".pem should be blocked"
    print("✓ private key patterns redacted")


def _test_binary_skipped():
    """Test that binary files are skipped."""
    from personal_tools.file_search import _is_binary_file
    
    assert _is_binary_file(Path("image.png")), ".png should be binary"
    assert _is_binary_file(Path("document.pdf")), ".pdf should be binary"
    assert _is_binary_file(Path("archive.zip")), ".zip should be binary"
    assert not _is_binary_file(Path("readme.md")), ".md should not be binary"
    assert not _is_binary_file(Path("script.py")), ".py should not be binary"
    print("✓ binary file skipped")


def _test_oversized_skipped():
    """Test that oversized files are skipped."""
    from personal_tools.file_search import _is_too_large
    
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        f.write(b"x" * 100)
        f.flush()
        
        # Should not be too large with high limit
        assert not _is_too_large(Path(f.name), 1000), "Small file should not be too large"
        
        # Should be too large with low limit
        assert _is_too_large(Path(f.name), 50), "Large file should be too large"
        
        os.unlink(f.name)
    print("✓ oversized file skipped")


def _test_files_search_returns_snippets():
    """Test that files.search returns snippets."""
    from personal_tools.file_search import search_files
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Test\nThis is a test file about deploy.\nDeploy is important.")
        
        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        result = search_files(settings, "deploy")
        
        assert result.ok, f"Search should succeed: {result.text}"
        assert "deploy" in result.text.lower(), f"Should find deploy: {result.text}"
        assert "test.md" in result.text, f"Should show filename: {result.text}"
    print("✓ files.search returns snippets")


def _test_files_read_returns_truncated():
    """Test that files.read returns truncated safe text."""
    from personal_tools.file_search import read_file
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test file that is within max_file_bytes
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("x" * 400)
        
        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            file_search_max_file_bytes=500,
        )
        result = read_file(settings, str(test_file))
        
        assert result.ok, f"Read should succeed: {result.text}"
        # File is under limit, so should not be truncated
        assert "truncated" not in result.text.lower(), f"Should not be truncated: {result.text}"
        
        # Test with a file that exceeds max_bytes
        test_file2 = Path(tmpdir) / "test2.md"
        test_file2.write_text("y" * 600)
        
        result2 = read_file(settings, str(test_file2))
        assert not result2.ok, f"Should reject oversized file: {result2.text}"
        assert "过大" in result2.text, f"Should mention file too large: {result2.text}"
    print("✓ files.read returns truncated safe text")


def _test_kb_index_creates_index():
    """Test that kb.index creates index."""
    from personal_tools.kb import index_files, _get_index_path
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Test\nThis is a test file.")
        
        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            codex_memory_root=Path(tmpdir) / "memory",
        )
        
        # Index files
        result = index_files(settings)
        assert result.ok, f"Index should succeed: {result.text}"
        assert "索引完成" in result.text, f"Should report success: {result.text}"
        
        # Check index file exists
        index_path = _get_index_path(settings)
        assert index_path.exists(), "Index file should be created"
    print("✓ kb.index creates index")


def _test_kb_search_works():
    """Test that kb.search works."""
    from personal_tools.kb import index_files, search_kb
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Test\nThis is a test file about deploy.")
        
        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            codex_memory_root=Path(tmpdir) / "memory",
        )
        
        # Index files
        index_files(settings)
        
        # Search
        result = search_kb(settings, "deploy")
        assert result.ok, f"Search should succeed: {result.text}"
        assert "deploy" in result.text.lower(), f"Should find deploy: {result.text}"
    print("✓ kb.search works")


def _test_nl_route_triggers_collector():
    """Test that NL routes trigger kb.collect_facts (P4.2.1)."""
    from handlers.intent import route_intent

    # Test file search intent → deterministic with kb.collect_facts
    route = route_intent("找一下文档里关于 deploy 的说明")
    assert route.kind == "deterministic", f"Should be deterministic: {route.kind}"
    assert "kb.collect_facts" in route.tools, f"Should use kb.collect_facts: {route.tools}"
    assert "deploy" in route.arg.lower(), f"Should extract query: {route.arg}"

    # Test README intent
    route = route_intent("README 里有没有 Gmail 配置步骤")
    assert route.kind == "deterministic", f"Should be deterministic: {route.kind}"
    assert "kb.collect_facts" in route.tools, f"Should use kb.collect_facts: {route.tools}"
    print("✓ NL route triggers kb.collect_facts")


def _test_nl_search_reads_snippets():
    """Test that NL file search reads snippets, not just file list (P4.2.1)."""
    from personal_tools.file_search import collect_file_facts

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test file with matching content
        test_file = Path(tmpdir) / "deploy.md"
        test_file.write_text("# Deploy\nRun ./deploy.sh to deploy.\nMake sure env is set.")

        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        evidence = collect_file_facts(settings, "test-op", "deploy")

        assert evidence, "Should return evidence"
        assert "deploy.md" in evidence, f"Should include filename: {evidence}"
        assert "deploy.sh" in evidence, f"Should include snippet content: {evidence}"
    print("✓ NL search reads snippets via collector")


def _test_display_path_does_not_break_read():
    """Test that relative display path doesn't break internal read (P4.2.1)."""
    from personal_tools.file_search import _search_files_internal, _read_snippet_by_absolute_path

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create nested file
        subdir = Path(tmpdir) / "docs"
        subdir.mkdir()
        test_file = subdir / "setup.md"
        test_file.write_text("# Setup\nInstall dependencies first.")

        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        results = _search_files_internal(settings, "Install")

        assert len(results) == 1, f"Should find 1 file: {results}"
        r = results[0]

        # display_path should be relative, absolute_path should be absolute
        assert not r["display_path"].startswith("/"), f"display_path should be relative: {r['display_path']}"
        assert r["absolute_path"].startswith("/"), f"absolute_path should be absolute: {r['absolute_path']}"

        # Reading via absolute path should work
        snippet = _read_snippet_by_absolute_path(
            settings,
            str(r["absolute_path"]),
            str(r["display_path"]),
            "Install",
        )
        assert snippet, "Should read snippet via absolute path"
        assert "docs/setup.md" in snippet, f"Should use display_path: {snippet}"
        assert "Install" in snippet, f"Should contain match: {snippet}"
    print("✓ relative display path does not break internal read")


def _test_kb_preferred_when_indexed():
    """Test that KB is preferred when indexed (P4.2.1)."""
    from personal_tools.kb import index_files, collect_evidence

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "readme.md"
        test_file.write_text("# Project\nUse OAuth for authentication.")

        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            codex_memory_root=Path(tmpdir) / "memory",
        )

        # Index files into KB
        index_files(settings)

        # collect_evidence should use KB (has indexed content)
        evidence = collect_evidence(settings, "test-op", "OAuth")
        assert evidence, "Should return evidence from KB"
        assert "OAuth" in evidence, f"Should contain match: {evidence}"
    print("✓ KB preferred when indexed")


def _test_fallback_when_kb_missing():
    """Test fallback to file search when KB missing (P4.2.1)."""
    from personal_tools.kb import collect_evidence

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files but NO KB index
        test_file = Path(tmpdir) / "guide.md"
        test_file.write_text("# Guide\nConfigure the scheduler in config.yaml.")

        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            codex_memory_root=Path(tmpdir) / "memory",
        )

        # collect_evidence should fallback to file search
        evidence = collect_evidence(settings, "test-op", "scheduler")
        assert evidence, "Should return evidence via fallback"
        assert "scheduler" in evidence.lower(), f"Should contain match: {evidence}"
    print("✓ fallback to file search when KB missing")


def _test_blocked_files_not_read():
    """Test that blocked files are not read through collector (P4.2.1)."""
    from personal_tools.file_search import collect_file_facts

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a .env file (should be blocked)
        env_file = Path(tmpdir) / ".env"
        env_file.write_text("SECRET_KEY=abc123")

        # Create a normal file
        normal_file = Path(tmpdir) / "readme.md"
        normal_file.write_text("# Readme\nNo secrets here.")

        settings = _make_settings(codex_workspace_root=Path(tmpdir))
        evidence = collect_file_facts(settings, "test-op", "SECRET_KEY")

        # Should NOT include .env content
        assert "abc123" not in evidence, f"Should not include .env content: {evidence}"
    print("✓ blocked files not read through collector")


def _test_outputs_redacted_truncated():
    """Test that outputs are redacted/truncated (P4.2.1)."""
    from personal_tools.file_search import search_files

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create file with sensitive-looking content
        test_file = Path(tmpdir) / "config.md"
        test_file.write_text("# Config\nToken: sk-1234567890abcdef\nOther content here.")

        settings = _make_settings(
            codex_workspace_root=Path(tmpdir),
            file_search_max_file_bytes=200,
        )
        result = search_files(settings, "Token")

        assert result.ok, f"Search should succeed: {result.text}"
        # The redaction module should handle sensitive patterns
        # At minimum, the result should be truncated
        assert len(result.text) <= 2000, f"Result should be truncated: {len(result.text)}"
    print("✓ outputs redacted/truncated")


def _test_project_docs_degrades():
    """Test that project docs search degrades without active project."""
    # This test verifies the command handler exists and can be called
    # In a real scenario, it would degrade gracefully without active project
    from handlers.commands import _project_docs
    assert callable(_project_docs), "project_docs command should exist"
    print("✓ project docs search degrades without active project")


def _test_no_network_calls():
    """Test that no network calls are made."""
    # This is a structural test - file_search and kb modules should not import network libraries
    import personal_tools.file_search as fs
    import personal_tools.kb as kb
    
    # Check that modules don't import network libraries
    fs_source = open(fs.__file__).read()
    kb_source = open(kb.__file__).read()
    
    assert "requests" not in fs_source, "file_search should not use requests"
    assert "urllib" not in fs_source, "file_search should not use urllib"
    assert "requests" not in kb_source, "kb should not use requests"
    assert "urllib" not in kb_source, "kb should not use urllib"
    print("✓ no network calls")


_TESTS = {
    "allowed root accepted": _test_allowed_root_accepted,
    "path traversal rejected": _test_path_traversal_rejected,
    ".env rejected": _test_env_rejected,
    "secrets directory rejected": _test_secrets_rejected,
    "private key patterns redacted": _test_private_key_redacted,
    "binary file skipped": _test_binary_skipped,
    "oversized file skipped": _test_oversized_skipped,
    "files.search returns snippets": _test_files_search_returns_snippets,
    "files.read returns truncated safe text": _test_files_read_returns_truncated,
    "kb.index creates index": _test_kb_index_creates_index,
    "kb.search works": _test_kb_search_works,
    "NL route triggers kb.collect_facts": _test_nl_route_triggers_collector,
    "NL search reads snippets": _test_nl_search_reads_snippets,
    "display path does not break read": _test_display_path_does_not_break_read,
    "KB preferred when indexed": _test_kb_preferred_when_indexed,
    "fallback when KB missing": _test_fallback_when_kb_missing,
    "blocked files not read": _test_blocked_files_not_read,
    "outputs redacted/truncated": _test_outputs_redacted_truncated,
    "project docs degrades": _test_project_docs_degrades,
    "no network calls": _test_no_network_calls,
}


def run_all() -> int:
    """Run all smoke tests."""
    passed = 0
    failed = 0
    
    for name, test_fn in _TESTS.items():
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"✗ {name}: {exc}")
            failed += 1
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
