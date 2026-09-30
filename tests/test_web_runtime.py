from __future__ import annotations

import unittest
from types import SimpleNamespace

from personal_tools.base import ToolResult
from personal_tools.web_runtime import WebRuntime
from personal_tools.web_search import SearchResult


class _FakeSearch:
    name = "fake-search"

    def search(self, query: str, limit: int | None = None):
        return [
            SearchResult(
                title="Example",
                url="https://example.com",
                snippet=query,
                source=self.name,
                rank=1,
            )
        ], ""


class _FakeFetch:
    name = "fake-fetch"

    def fetch(self, url: str) -> ToolResult:
        return ToolResult(ok=True, text=f"fetched:{url}")


class _FakeBrowser:
    name = "fake-browser"

    def open(self, url: str) -> ToolResult:
        return ToolResult(ok=True, text=f"opened:{url}")


class _FakeAgent:
    name = "fake-agent"

    def run(self, goal: str, *, start_url: str | None = None) -> ToolResult:
        return ToolResult(ok=True, text=f"{goal}|{start_url or ''}")


class WebRuntimeTests(unittest.TestCase):
    def test_capability_slots_and_delegation(self):
        runtime = WebRuntime(
            search_provider=_FakeSearch(),
            fetch_provider=_FakeFetch(),
            browser_provider=_FakeBrowser(),
            agent_provider=_FakeAgent(),
        )

        status = runtime.capabilities()
        self.assertTrue(status.search)
        self.assertTrue(status.fetch)
        self.assertTrue(status.browser)
        self.assertTrue(status.agent)

        results, err = runtime.search("latest facts", 3)
        self.assertEqual(err, "")
        self.assertEqual(results[0].snippet, "latest facts")
        self.assertTrue(runtime.fetch("https://example.com").ok)
        self.assertTrue(runtime.open_browser("https://example.com").ok)
        self.assertTrue(runtime.run_agent("compare prices", start_url="https://example.com").ok)

    def test_missing_browser_and_agent_fail_closed(self):
        runtime = WebRuntime()
        self.assertFalse(runtime.open_browser("https://example.com").ok)
        self.assertFalse(runtime.run_agent("do something").ok)

    def test_from_settings_preserves_current_search_fetch_switches(self):
        disabled = SimpleNamespace(
            web_search_backend="disabled",
            web_fetch_enabled=False,
        )
        status = WebRuntime.from_settings(disabled).capabilities()
        self.assertFalse(status.search)
        self.assertFalse(status.fetch)
        self.assertFalse(status.browser)
        self.assertFalse(status.agent)

        enabled = SimpleNamespace(
            web_search_backend="tinyfish",
            web_fetch_enabled=True,
        )
        status = WebRuntime.from_settings(enabled).capabilities()
        self.assertTrue(status.search)
        self.assertTrue(status.fetch)
        self.assertFalse(status.browser)
        self.assertFalse(status.agent)


if __name__ == "__main__":
    unittest.main()
