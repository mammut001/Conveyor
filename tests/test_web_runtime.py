from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from personal_tools.base import ToolResult
from personal_tools.web_runtime import (
    SafeFetchProvider,
    TinyFishFetchProvider,
    WebRuntime,
)
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

    def test_from_settings_preserves_disabled_switches(self):
        disabled = SimpleNamespace(
            web_search_backend="disabled",
            web_fetch_enabled=False,
        )
        status = WebRuntime.from_settings(disabled).capabilities()
        self.assertFalse(status.search)
        self.assertFalse(status.fetch)
        self.assertFalse(status.browser)
        self.assertFalse(status.agent)

    def test_non_tinyfish_search_keeps_local_safe_fetch(self):
        settings = SimpleNamespace(
            web_search_backend="brave",
            web_fetch_enabled=True,
        )
        runtime = WebRuntime.from_settings(settings)
        self.assertIsInstance(runtime.fetch_provider, SafeFetchProvider)

    def test_tinyfish_search_pairs_with_tinyfish_fetch(self):
        settings = SimpleNamespace(
            web_search_backend="tinyfish",
            web_fetch_enabled=True,
            web_search_api_key="tinyfish-key",
        )
        runtime = WebRuntime.from_settings(settings)
        self.assertIsInstance(runtime.fetch_provider, TinyFishFetchProvider)

    def test_tinyfish_fetch_normalizes_clean_text(self):
        settings = SimpleNamespace(
            web_fetch_enabled=True,
            web_search_backend="tinyfish",
            web_search_api_key="tinyfish-key",
            web_fetch_timeout_seconds=10,
            web_fetch_max_bytes=2_000_000,
            web_user_agent="ConveyorTest/1.0",
        )
        provider = TinyFishFetchProvider(settings)
        payload = {
            "results": [
                {
                    "url": "https://example.com/article",
                    "title": "Example",
                    "format": "markdown",
                    "text": "# Example\n\nClean rendered content.",
                }
            ],
            "errors": [],
        }

        with patch("personal_tools.web_runtime.validate_url", return_value=(True, "")):
            with patch.object(provider, "_request_json", return_value=(payload, "")) as request_json:
                result = provider.fetch("https://example.com/article")

        self.assertTrue(result.ok)
        self.assertIn("Clean rendered content", result.text)
        request_json.assert_called_once_with({"urls": ["https://example.com/article"]})

    def test_tinyfish_fetch_keeps_ssrf_gate(self):
        settings = SimpleNamespace(
            web_fetch_enabled=True,
            web_search_backend="tinyfish",
            web_search_api_key="tinyfish-key",
        )
        provider = TinyFishFetchProvider(settings)
        with patch(
            "personal_tools.web_runtime.validate_url",
            return_value=(False, "拒绝访问: private target"),
        ):
            result = provider.fetch("http://127.0.0.1/secret")
        self.assertFalse(result.ok)
        self.assertIn("URL 验证失败", result.text)

    def test_tinyfish_fetch_missing_key_fails_before_network(self):
        settings = SimpleNamespace(
            web_fetch_enabled=True,
            web_search_backend="tinyfish",
            web_search_api_key=None,
        )
        provider = TinyFishFetchProvider(settings)
        data, err = provider._request_json({"urls": ["https://example.com"]})
        self.assertIsNone(data)
        self.assertIn("API key", err)


if __name__ == "__main__":
    unittest.main()
