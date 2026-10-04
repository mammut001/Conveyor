"""Unified web capability facade for Conveyor.

This module defines the stable abstraction Conveyor code should depend on:

    Search -> Fetch -> Browser -> Agent

Search and Fetch have concrete providers today. Browser and Agent are provider
slots only; callers can detect that they are unavailable instead of reaching
into a vendor-specific implementation.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.web_fetch import fetch_text, validate_url
from personal_tools.web_search import SearchResult, search_web
from redaction import redact_text, truncate


class SearchProvider(Protocol):
    """Provider contract for web discovery."""

    name: str

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> tuple[list[SearchResult], str]: ...


class FetchProvider(Protocol):
    """Provider contract for turning a known URL into readable content."""

    name: str

    def fetch(self, url: str) -> ToolResult: ...


class BrowserProvider(Protocol):
    """Provider contract for deterministic interactive browser control."""

    name: str

    def open(self, url: str) -> ToolResult: ...


class AgentProvider(Protocol):
    """Provider contract for goal-driven, multi-step web execution."""

    name: str

    def run(self, goal: str, *, start_url: str | None = None) -> ToolResult: ...


@dataclass(frozen=True)
class WebCapabilityStatus:
    """Availability snapshot for the four web primitives."""

    search: bool
    fetch: bool
    browser: bool
    agent: bool


class ConfiguredSearchProvider:
    """Adapter over Conveyor's existing WEB_SEARCH_BACKEND switch."""

    name = "configured-search"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> tuple[list[SearchResult], str]:
        return search_web(self.settings, query, limit)


class SafeFetchProvider:
    """Adapter over Conveyor's SSRF-hardened read-only local fetch path."""

    name = "local-safe-fetch"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch(self, url: str) -> ToolResult:
        return fetch_text(self.settings, url)


class TinyFishFetchProvider:
    """Fetch clean rendered page content through TinyFish.

    TinyFish uses one API key for Search and Fetch. Conveyor only selects this
    provider when ``WEB_SEARCH_BACKEND=tinyfish``; this prevents accidentally
    sending another search provider's credential to TinyFish.

    The target URL and the fixed TinyFish endpoint both pass Conveyor's URL
    validation before a request is made, preserving the public-web boundary.
    """

    name = "tinyfish-fetch"
    DEFAULT_ENDPOINT = "https://api.fetch.tinyfish.ai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _request_json(self, payload: dict) -> tuple[dict | None, str]:
        api_key = getattr(self.settings, "web_search_api_key", None)
        if not api_key:
            return None, "TinyFish API key 未配置"

        endpoint = self.DEFAULT_ENDPOINT
        ok, err = validate_url(endpoint)
        if not ok:
            return None, f"TinyFish Fetch endpoint 无效: {err}"

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("User-Agent", getattr(self.settings, "web_user_agent", "ConveyorBot/0.1"))
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-Key", api_key)
        ctx = ssl.create_default_context()
        timeout = getattr(self.settings, "web_fetch_timeout_seconds", 10)
        max_bytes = max(1, int(getattr(self.settings, "web_fetch_max_bytes", 2_000_000)))

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    return None, "TinyFish Fetch 响应超过大小限制"
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    return None, f"TinyFish Fetch JSON 解析失败: {exc}"
                if not isinstance(parsed, dict):
                    return None, "TinyFish Fetch 返回格式无效"
                return parsed, ""
        except urllib.error.HTTPError as exc:
            return None, f"TinyFish Fetch HTTP 错误: {exc.code}"
        except urllib.error.URLError as exc:
            return None, f"TinyFish Fetch URL 错误: {redact_text(str(exc.reason))}"
        except TimeoutError:
            return None, "TinyFish Fetch 请求超时"
        except Exception as exc:
            return None, f"TinyFish Fetch 请求失败: {redact_text(str(exc))}"

    def fetch(self, url: str) -> ToolResult:
        if not bool(getattr(self.settings, "web_fetch_enabled", False)):
            return ToolResult(ok=False, text="⚠️ Web Fetch 已禁用")

        ok, err = validate_url(url)
        if not ok:
            return ToolResult(ok=False, text=f"⚠️ URL 验证失败: {err}")

        data, err = self._request_json({"urls": [url]})
        if err or data is None:
            return ToolResult(ok=False, text=f"⚠️ {err or 'TinyFish Fetch 失败'}")

        results = data.get("results")
        if not isinstance(results, list) or not results:
            errors = data.get("errors")
            if isinstance(errors, list) and errors:
                detail = redact_text(str(errors[0]))
                return ToolResult(ok=False, text=f"⚠️ TinyFish Fetch 失败: {detail}")
            return ToolResult(ok=False, text="⚠️ TinyFish Fetch 未返回内容")

        first = results[0]
        if not isinstance(first, dict):
            return ToolResult(ok=False, text="⚠️ TinyFish Fetch 返回格式无效")
        text = first.get("text") or first.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            return ToolResult(ok=False, text="⚠️ TinyFish Fetch 返回空内容")
        return ToolResult(ok=True, text=truncate(redact_text(text)))


class WebRuntime:
    """Vendor-neutral facade used by higher-level Conveyor workflows."""

    def __init__(
        self,
        *,
        search_provider: SearchProvider | None = None,
        fetch_provider: FetchProvider | None = None,
        browser_provider: BrowserProvider | None = None,
        agent_provider: AgentProvider | None = None,
    ) -> None:
        self.search_provider = search_provider
        self.fetch_provider = fetch_provider
        self.browser_provider = browser_provider
        self.agent_provider = agent_provider

    @classmethod
    def from_settings(cls, settings: Settings) -> "WebRuntime":
        """Build the default runtime while preserving legacy defaults.

        TinyFish is the one paired provider in this phase: when it backs Search,
        WebRuntime also uses TinyFish Fetch. Other search backends keep the
        existing local SSRF-hardened fetch implementation.
        """

        search_backend = getattr(settings, "web_search_backend", "disabled")
        search_provider: SearchProvider | None = None
        if search_backend != "disabled":
            search_provider = ConfiguredSearchProvider(settings)

        fetch_provider: FetchProvider | None = None
        if bool(getattr(settings, "web_fetch_enabled", False)):
            if search_backend == "tinyfish":
                fetch_provider = TinyFishFetchProvider(settings)
            else:
                fetch_provider = SafeFetchProvider(settings)

        return cls(
            search_provider=search_provider,
            fetch_provider=fetch_provider,
        )

    def capabilities(self) -> WebCapabilityStatus:
        return WebCapabilityStatus(
            search=self.search_provider is not None,
            fetch=self.fetch_provider is not None,
            browser=self.browser_provider is not None,
            agent=self.agent_provider is not None,
        )

    def search(
        self,
        query: str,
        limit: int | None = None,
    ) -> tuple[list[SearchResult], str]:
        if self.search_provider is None:
            return [], "Web 搜索能力未配置（WEB_SEARCH_BACKEND=disabled；可选 searxng / brave / tavily / serper / tinyfish）"
        return self.search_provider.search(query, limit)

    def fetch(self, url: str) -> ToolResult:
        if self.fetch_provider is None:
            return ToolResult(ok=False, text="⚠️ Web Fetch 能力未配置")
        return self.fetch_provider.fetch(url)

    def open_browser(self, url: str) -> ToolResult:
        if self.browser_provider is None:
            return ToolResult(ok=False, text="⚠️ Browser 能力未配置")
        return self.browser_provider.open(url)

    def run_agent(self, goal: str, *, start_url: str | None = None) -> ToolResult:
        if self.agent_provider is None:
            return ToolResult(ok=False, text="⚠️ Web Agent 能力未配置")
        return self.agent_provider.run(goal, start_url=start_url)
