"""personal_tools/research.py — Research tool for Conveyor (P4.1 Phase C).

Hybrid WebRuntime search + fetch + Codex synthesis.
Collects evidence from web search, fetches top sources, builds an evidence pack,
then passes it to Codex for structured analysis. All READ-only, no WRITE tools.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.web_runtime import WebRuntime
from personal_tools.web_search import SearchResult


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
    runtime: WebRuntime,
    results: list[SearchResult],
    fetch_top_n: int,
    max_chars: int,
) -> list[EvidenceItem]:
    """Fetch text from top N search results through the configured runtime."""
    evidence: list[EvidenceItem] = []
    for r in results[:fetch_top_n]:
        result = runtime.fetch(r.url)
        text = result.text if result.ok else ""
        if len(text) > max_chars:
            text = text[:max_chars] + "..."

        evidence.append(EvidenceItem(
            title=r.title,
            url=r.url,
            snippet=r.snippet,
            text_excerpt=text,
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


def _search_and_fetch(
    settings: Settings,
    query: str,
) -> tuple[list[EvidenceItem], str]:
    """Collect deduplicated evidence through WebRuntime."""
    runtime = WebRuntime.from_settings(settings)
    results, err = runtime.search(query, settings.research_max_sources * 2)
    if err:
        return [], err
    if not results:
        return [], "no search results"

    deduped = _dedupe_domains(results, settings.research_max_sources)
    evidence = _fetch_evidence(
        runtime,
        deduped,
        settings.research_fetch_top_n,
        settings.research_max_chars_per_source,
    )
    return evidence, ""


def research_collect(settings: Settings, question: str) -> ToolResult:
    """Run research: search + fetch + build evidence pack.

    Returns [HYBRID_PROMPT] prefix for Codex synthesis.
    """
    question = question.strip()
    if not question:
        return ToolResult(ok=False, text="⚠️ 用法: /research <问题>")

    evidence, err = _search_and_fetch(settings, question)
    if err:
        if err == "no search results":
            return ToolResult(ok=False, text="⚠️ 无搜索结果")
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {err}")

    evidence_pack = _build_evidence_pack(evidence)
    prompt = _build_research_prompt(question, evidence_pack)
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


def project_research_collect(
    settings: Settings,
    operator_id: str,
    question: str,
    project_id: str = "",
) -> ToolResult:
    """Run research with project context.

    Returns [HYBRID_PROMPT] prefix for Codex synthesis.
    """
    from personal_tools.store import PersonalToolsStore

    question = question.strip()
    if not question:
        return ToolResult(ok=False, text="⚠️ 用法: /project_research [项目ID] <问题>")

    store = PersonalToolsStore(settings)
    proj = None
    if project_id.strip():
        try:
            pid = int(project_id.strip())
            proj = store.get_project_profile(operator_id, pid)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {project_id}")
    else:
        proj = store.get_active_or_first_project(operator_id)

    search_query = question
    if proj:
        context_parts = [proj.name, proj.type, proj.description]
        if proj.keywords:
            context_parts.extend(proj.keywords)
        context = " ".join(p for p in context_parts if p)
        search_query = f"{context} {question}"

    evidence, err = _search_and_fetch(settings, search_query)
    if err:
        if err == "no search results":
            return ToolResult(ok=False, text="⚠️ 无搜索结果")
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {err}")

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
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


def factcheck_evidence(settings: Settings, claim: str) -> tuple[str, str]:
    """Collect a web evidence pack for fact-checking ``claim``.

    Returns ``(evidence_pack, error)``. Callers fall back to letting the agent
    check on its own when ``error`` is set, so this never raises for normal
    backend/search failures.
    """
    claim = claim.strip()
    if not claim:
        return "", "empty claim"

    evidence, err = _search_and_fetch(settings, claim)
    if err:
        return "", err
    return _build_evidence_pack(evidence), ""


# --- Adapters for personal_tools/registry.py ---

async def research_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return research_collect(settings, arg)


async def project_research_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    parts = arg.strip().split(None, 1)
    if len(parts) == 2 and parts[0].isdigit():
        return project_research_collect(settings, operator_id, parts[1], parts[0])
    return project_research_collect(settings, operator_id, arg)
