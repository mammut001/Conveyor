"""personal_tools/research.py — Research tool for Conveyor (P4.1 Phase C).

Hybrid web.search + fetch + Codex synthesis.
Collects evidence from web search, fetches top sources, builds
evidence pack, then passes to Codex for structured analysis.
All READ-only, no WRITE tools.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.web_fetch import validate_url, fetch_text
from personal_tools.web_search import search_web, SearchResult
from redaction import redact_text, truncate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceItem:
    """A single piece of evidence from a web source."""
    title: str
    url: str
    snippet: str
    text_excerpt: str


def _dedupe_domains(results: list[SearchResult], max_results: int) -> list[SearchResult]:
    """Deduplicate by domain, keeping first occurrence."""
    seen_domains: set[str] = set()
    deduped: list[SearchResult] = []
    for r in results:
        try:
            from urllib.parse import urlparse
            domain = urlparse(r.url).netloc.lower()
        except Exception:
            domain = r.url
        if domain not in seen_domains:
            seen_domains.add(domain)
            deduped.append(r)
        if len(deduped) >= max_results:
            break
    return deduped


def _fetch_evidence(
    settings: Settings,
    results: list[SearchResult],
    fetch_top_n: int,
    max_chars: int,
) -> list[EvidenceItem]:
    """Fetch text from top N search results for evidence."""
    evidence: list[EvidenceItem] = []
    for r in results[:fetch_top_n]:
        ok, err = validate_url(r.url)
        if not ok:
            logger.debug("Skipping %s: %s", r.url, err)
            evidence.append(EvidenceItem(
                title=r.title, url=r.url,
                snippet=r.snippet, text_excerpt="",
            ))
            continue

        result = fetch_text(settings, r.url)
        text = result.text if result.ok else ""
        # Truncate to max_chars
        if len(text) > max_chars:
            text = text[:max_chars] + "..."

        evidence.append(EvidenceItem(
            title=r.title, url=r.url,
            snippet=r.snippet, text_excerpt=text,
        ))
    return evidence


def _build_evidence_pack(evidence: list[EvidenceItem]) -> str:
    """Build a formatted evidence pack for Codex."""
    lines = ["## 证据包", ""]
    for i, e in enumerate(evidence, 1):
        lines.append(f"### 来源 {i}: {e.title}")
        lines.append(f"URL: {e.url}")
        if e.snippet:
            lines.append(f"摘要: {e.snippet}")
        if e.text_excerpt:
            lines.append(f"内容摘录:\n{e.text_excerpt}")
        lines.append("")
    return "\n".join(lines)


def _build_research_prompt(question: str, evidence_pack: str) -> str:
    """Build a research prompt for Codex synthesis."""
    return (
        f"## 研究问题\n\n{question}\n\n"
        f"{evidence_pack}\n\n"
        f"## 任务\n\n"
        f"请基于以上证据，用中文给出结构化的研究报告：\n"
        f"1. 📋 概述（1-2 段）\n"
        f"2. 🔍 关键发现（3-5 个要点）\n"
        f"3. 📊 详细分析\n"
        f"4. ⚠️ 注意事项和局限性\n"
        f"5. 🔗 参考来源\n"
        f"6. 💡 建议的下一步\n"
    )


def research_collect(settings: Settings, question: str) -> ToolResult:
    """Run research: search + fetch + build evidence pack."""
    question = question.strip()
    if not question:
        return ToolResult(ok=False, text="⚠️ 用法: /research <问题>")

    # Step 1: Search
    results, err = search_web(settings, question, settings.research_max_sources * 2)
    if err:
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {err}")
    if not results:
        return ToolResult(ok=False, text="⚠️ 无搜索结果")

    # Step 2: Dedupe domains
    deduped = _dedupe_domains(results, settings.research_max_sources)

    # Step 3: Fetch evidence
    evidence = _fetch_evidence(
        settings, deduped,
        settings.research_fetch_top_n,
        settings.research_max_chars_per_source,
    )

    # Step 4: Build evidence pack
    evidence_pack = _build_evidence_pack(evidence)
    prompt = _build_research_prompt(question, evidence_pack)

    return ToolResult(ok=True, text=truncate(prompt))


def project_research_collect(
    settings: Settings,
    operator_id: str,
    question: str,
    project_id: str = "",
) -> ToolResult:
    """Run research with project context."""
    from personal_tools.store import PersonalToolsStore

    question = question.strip()
    if not question:
        return ToolResult(ok=False, text="⚠️ 用法: /project_research [项目ID] <问题>")

    store = PersonalToolsStore(settings)

    # Get project
    proj = None
    if project_id.strip():
        try:
            pid = int(project_id.strip())
            proj = store.get_project_profile(operator_id, pid)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {project_id}")
    else:
        proj = store.get_active_or_first_project(operator_id)

    # Build search query with project context
    search_query = question
    if proj:
        context_parts = [proj.name, proj.type, proj.description]
        if proj.keywords:
            context_parts.extend(proj.keywords)
        context = " ".join(p for p in context_parts if p)
        search_query = f"{context} {question}"

    # Step 1: Search
    results, err = search_web(settings, search_query, settings.research_max_sources * 2)
    if err:
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {err}")
    if not results:
        return ToolResult(ok=False, text="⚠️ 无搜索结果")

    # Step 2: Dedupe domains
    deduped = _dedupe_domains(results, settings.research_max_sources)

    # Step 3: Fetch evidence
    evidence = _fetch_evidence(
        settings, deduped,
        settings.research_fetch_top_n,
        settings.research_max_chars_per_source,
    )

    # Step 4: Build evidence pack with project context
    evidence_pack = _build_evidence_pack(evidence)

    project_context = ""
    if proj:
        project_context = (
            f"## 项目上下文\n\n"
            f"- 项目名称: {proj.name}\n"
            f"- 项目类型: {proj.type}\n"
            f"- 描述: {proj.description}\n"
            f"- GitHub: {proj.github_repo}\n"
            f"- 关键词: {', '.join(proj.keywords)}\n\n"
        )

    prompt = (
        f"{project_context}"
        f"## 研究问题\n\n{question}\n\n"
        f"{evidence_pack}\n\n"
        f"## 任务\n\n"
        f"请基于以上证据和项目上下文，用中文给出结构化的研究报告：\n"
        f"1. 📋 概述（1-2 段）\n"
        f"2. 🔍 关键发现（3-5 个要点）\n"
        f"3. 📊 与项目的关联性分析\n"
        f"4. ⚠️ 注意事项和局限性\n"
        f"5. 🔗 参考来源\n"
        f"6. 💡 对项目的建议\n"
    )

    return ToolResult(ok=True, text=truncate(prompt))


# --- Adapters for personal_tools/registry.py ---

async def research_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return research_collect(settings, arg)


async def project_research_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    # Parse optional project_id from arg
    parts = arg.strip().split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        return project_research_collect(settings, operator_id, parts[1], parts[0])
    return project_research_collect(settings, operator_id, arg)
