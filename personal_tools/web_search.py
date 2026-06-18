"""personal_tools/web_search.py — Web Search for Conveyor (P4.1 Phase B).

Supports multiple backends: disabled, searxng, brave, tavily, serper.
Degrades gracefully when backend is disabled or unconfigured.
All output passes redact_text + truncate.

Security: Uses urllib.request instead of subprocess to avoid exposing
API keys in process command-line arguments.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlencode

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.web_fetch import validate_url
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

SUPPORTED_BACKENDS = ("disabled", "searxng", "brave", "tavily", "serper")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    rank: int


def _normalize_result(raw: dict, rank: int, backend: str) -> SearchResult:
    """Normalize a raw search result dict to SearchResult."""
    return SearchResult(
        title=raw.get("title", ""),
        url=raw.get("url", raw.get("link", "")),
        snippet=raw.get("snippet", raw.get("description", "")),
        source=backend,
        rank=rank,
    )


def _fetch_json(url: str, settings: Settings, headers: dict[str, str] | None = None, 
                data: bytes | None = None, method: str = "GET") -> tuple[int, dict | list | None, str]:
    """Fetch JSON via urllib.request (no subprocess, no API keys in argv).
    
    Returns (status_code, parsed_json, error).
    """
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", settings.web_user_agent)
    req.add_header("Accept", "application/json")
    
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    
    if data is not None:
        req.data = data
        if "Content-Type" not in (headers or {}):
            req.add_header("Content-Type", "application/json")
    
    # Create SSL context that verifies certificates
    ctx = ssl.create_default_context()
    
    try:
        with urllib.request.urlopen(req, timeout=settings.web_fetch_timeout_seconds, context=ctx) as resp:
            status = resp.status
            body = resp.read()
            try:
                parsed = json.loads(body)
                return status, parsed, ""
            except json.JSONDecodeError as exc:
                return status, None, f"JSON 解析失败: {exc}"
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP 错误: {exc.code}"
    except urllib.error.URLError as exc:
        return -1, None, f"URL 错误: {redact_text(str(exc.reason))}"
    except TimeoutError:
        return -1, None, "请求超时"
    except Exception as exc:
        return -1, None, redact_text(str(exc))


def _search_brave(settings: Settings, query: str, limit: int) -> tuple[list[SearchResult], str]:
    """Search via Brave Search API."""
    if not settings.web_search_api_key:
        return [], "Brave API key 未配置"
    endpoint = settings.web_search_endpoint or "https://api.search.brave.com/res/v1/web/search"
    
    # Validate endpoint URL
    ok, err = validate_url(endpoint)
    if not ok:
        return [], f"Brave endpoint 无效: {err}"
    
    # URL encode query
    url = f"{endpoint}?{urlencode({'q': query, 'count': limit})}"
    headers = {
        "X-Subscription-Token": settings.web_search_api_key,
    }
    rc, data, err = _fetch_json(url, settings, headers)
    if rc != 200 or data is None:
        return [], f"Brave 搜索失败: {redact_text(err)}"
    results = data.get("web", {}).get("results", [])
    return [_normalize_result(r, i + 1, "brave") for i, r in enumerate(results[:limit])], ""


def _search_tavily(settings: Settings, query: str, limit: int) -> tuple[list[SearchResult], str]:
    """Search via Tavily Search API."""
    if not settings.web_search_api_key:
        return [], "Tavily API key 未配置"
    endpoint = settings.web_search_endpoint or "https://api.tavily.com/search"
    
    # Validate endpoint URL
    ok, err = validate_url(endpoint)
    if not ok:
        return [], f"Tavily endpoint 无效: {err}"
    
    payload = json.dumps({
        "api_key": settings.web_search_api_key,
        "query": query,
        "max_results": limit,
    }).encode("utf-8")
    
    rc, data, err = _fetch_json(endpoint, settings, data=payload, method="POST")
    if rc != 200 or data is None:
        return [], f"Tavily 搜索失败: {redact_text(err)}"
    results = data.get("results", [])
    return [_normalize_result(r, i + 1, "tavily") for i, r in enumerate(results[:limit])], ""


def _search_serper(settings: Settings, query: str, limit: int) -> tuple[list[SearchResult], str]:
    """Search via Serper.dev API."""
    if not settings.web_search_api_key:
        return [], "Serper API key 未配置"
    endpoint = settings.web_search_endpoint or "https://google.serper.dev/search"
    
    # Validate endpoint URL
    ok, err = validate_url(endpoint)
    if not ok:
        return [], f"Serper endpoint 无效: {err}"
    
    payload = json.dumps({"q": query, "num": limit}).encode("utf-8")
    headers = {"X-API-KEY": settings.web_search_api_key}
    
    rc, data, err = _fetch_json(endpoint, settings, headers=headers, data=payload, method="POST")
    if rc != 200 or data is None:
        return [], f"Serper 搜索失败: {redact_text(err)}"
    results = data.get("organic", [])
    return [_normalize_result(r, i + 1, "serper") for i, r in enumerate(results[:limit])], ""


def _search_searxng(settings: Settings, query: str, limit: int) -> tuple[list[SearchResult], str]:
    """Search via SearXNG instance."""
    endpoint = settings.web_search_endpoint
    if not endpoint:
        return [], "SearXNG endpoint 未配置"
    
    # Validate endpoint URL
    ok, err = validate_url(endpoint)
    if not ok:
        return [], f"SearXNG endpoint 无效: {err}"
    
    # URL encode query
    url = f"{endpoint}/search?{urlencode({'q': query, 'format': 'json', 'pageno': 1})}"
    rc, data, err = _fetch_json(url, settings)
    if rc != 200 or data is None:
        return [], f"SearXNG 搜索失败: {redact_text(err)}"
    results = data.get("results", [])
    return [_normalize_result(r, i + 1, "searxng") for i, r in enumerate(results[:limit])], ""


def search_web(settings: Settings, query: str, limit: int | None = None) -> tuple[list[SearchResult], str]:
    """Search the web using the configured backend. Returns (results, error)."""
    if limit is None:
        limit = settings.web_search_max_results
    backend = settings.web_search_backend

    if backend == "disabled":
        return [], "Web 搜索后端未启用（WEB_SEARCH_BACKEND=disabled）"
    if backend not in SUPPORTED_BACKENDS:
        return [], f"不支持的搜索后端: {backend}"

    query = query.strip()
    if not query:
        return [], "搜索词不能为空"

    dispatchers = {
        "brave": _search_brave,
        "tavily": _search_tavily,
        "serper": _search_serper,
        "searxng": _search_searxng,
    }
    dispatcher = dispatchers.get(backend)
    if dispatcher is None:
        return [], f"搜索后端 {backend} 未实现"
    return dispatcher(settings, query, limit)


def format_results(results: list[SearchResult]) -> str:
    """Format search results for display."""
    if not results:
        return "无搜索结果"
    lines = []
    for r in results:
        lines.append(f"{r.rank}. {r.title}")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {redact_text(r.snippet[:200])}")
        lines.append("")
    return "\n".join(lines)


# --- Adapter for personal_tools/registry.py ---

async def web_search_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    if not arg.strip():
        return ToolResult(ok=False, text="⚠️ 用法: /web_search <查询词>")
    results, err = search_web(settings, arg)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")
    return ToolResult(ok=True, text=truncate(format_results(results)))
