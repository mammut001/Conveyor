"""Unified web capability facade for Conveyor.

This module defines the stable abstraction Conveyor code should depend on:

    Search -> Fetch -> Browser -> Agent

Search and Fetch are wired to the existing safe implementations today.
Browser and Agent are provider slots only; callers can detect that they are
unavailable instead of reaching into a concrete implementation.

The goal is to keep orchestration code independent from vendors such as
Brave, Tavily, TinyFish, Browserbase, or a future in-house browser runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.web_fetch import fetch_text
from personal_tools.web_search import SearchResult, search_web


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
    """Adapter over Conveyor's SSRF-hardened read-only fetch path."""

    name = "safe-fetch"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch(self, url: str) -> ToolResult:
        return fetch_text(self.settings, url)


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
        """Build the default runtime without changing existing behavior."""

        search_provider: SearchProvider | None = None
        if getattr(settings, "web_search_backend", "disabled") != "disabled":
            search_provider = ConfiguredSearchProvider(settings)

        fetch_provider: FetchProvider | None = None
        if bool(getattr(settings, "web_fetch_enabled", False)):
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
            return [], "Web 搜索能力未配置"
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
