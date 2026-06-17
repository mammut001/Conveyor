"""personal_tools/github_tools.py — GitHub read-first project tools for Conveyor (P3.6).

Provides GitHub REST API client for:
  - Issues (list, read)
  - PRs (list, read)
  - CI status (check runs, workflows)
  - Create issue (WRITE_SAFE)
  - Comment on issue/PR (WRITE, requires confirmation)

All outputs pass redact_text + truncate. No raw token exposed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# GitHub API time format
GITHUB_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _check_config(settings: Settings) -> str | None:
    """Check if GitHub config is present. Returns error message or None."""
    if not settings.github_token:
        return "⚠️ GITHUB_TOKEN 未配置"
    if not settings.github_default_repo:
        return "⚠️ GITHUB_DEFAULT_REPO 未配置（如 mammut001/Conveyor）"
    return None


def _make_headers(settings: Settings) -> dict[str, str]:
    """Build GitHub API request headers."""
    return {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Conveyor-Bot",
    }


def _github_request(
    settings: Settings,
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    """Make a GitHub API request. Returns (ok, data_or_error)."""
    import urllib.request
    import urllib.parse
    import urllib.error

    url = f"{settings.github_api_base.rstrip('/')}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = _make_headers(settings)
    data = json.dumps(json_body).encode("utf-8") if json_body else None

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return True, {}
            return True, json.loads(body)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        # Redact token from error
        error_msg = redact_text(f"GitHub API {exc.code}: {error_body[:200]}")
        return False, error_msg
    except Exception as exc:
        return False, redact_text(f"GitHub request failed: {type(exc).__name__}")


def _parse_repo(repo: str) -> tuple[str, str]:
    """Parse owner/repo string. Returns (owner, repo)."""
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo format: {repo} (expected owner/repo)")
    return parts[0], parts[1]


def _format_issue(issue: dict) -> str:
    """Format issue for display."""
    number = issue.get("number", "?")
    title = issue.get("title", "(no title)")
    state = issue.get("state", "unknown")
    user = issue.get("user", {}).get("login", "?")
    created = issue.get("created_at", "")[:10]
    comments = issue.get("comments", 0)

    labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
    label_str = f" [{labels}]" if labels else ""

    return f"#{number} {state} {title}{label_str} (@{user}, {created}, {comments}💬)"


def _format_pr(pr: dict) -> str:
    """Format PR for display."""
    number = pr.get("number", "?")
    title = pr.get("title", "(no title)")
    state = pr.get("state", "unknown")
    user = pr.get("user", {}).get("login", "?")
    created = pr.get("created_at", "")[:10]
    comments = pr.get("comments", 0)
    merged = pr.get("merged", False)

    status = "merged" if merged else state
    draft = " (draft)" if pr.get("draft") else ""

    return f"#{number} {status}{draft} {title} (@{user}, {created}, {comments}💬)"


def _format_check_run(check: dict) -> str:
    """Format check run for display."""
    name = check.get("name", "?")
    status = check.get("status", "unknown")
    conclusion = check.get("conclusion")
    started = check.get("started_at", "")[:16]

    if status == "completed":
        icon = "✅" if conclusion == "success" else "❌" if conclusion == "failure" else "⚠️"
        return f"  {icon} {name}: {conclusion} ({started})"
    return f"  ⏳ {name}: {status} ({started})"


# --- Public functions ---

def github_status(settings: Settings) -> ToolResult:
    """Report GitHub connection status."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    # Test connection by fetching user info
    ok, data = _github_request(settings, "GET", "/user")
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ GitHub 连接失败: {data}")

    login = data.get("login", "?")
    name = data.get("name", "")
    repo = settings.github_default_repo

    return ToolResult(ok=True, text=(
        f"GitHub 连接正常\n"
        f"用户: {login} ({name})\n"
        f"默认仓库: {repo}"
    ))


def github_issues(
    settings: Settings,
    query_or_state: str = "open",
    limit: int = 20,
) -> ToolResult:
    """List issues. Supports state filter or search query."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    owner, repo = _parse_repo(settings.github_default_repo)

    # Determine if it's a state filter or search query
    state = query_or_state.strip().lower()
    if state in ("open", "closed", "all"):
        endpoint = f"/repos/{owner}/{repo}/issues"
        params = {"state": state, "per_page": min(limit, 100), "sort": "updated"}
        ok, data = _github_request(settings, "GET", endpoint, params=params)
    else:
        # Search query
        endpoint = "/search/issues"
        q = f"repo:{owner}/{repo} is:issue {query_or_state}"
        params = {"q": q, "per_page": min(limit, 100)}
        ok, data = _github_request(settings, "GET", endpoint, params=params)
        if ok:
            data = data.get("items", [])

    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 获取 issues 失败: {data}")

    if not data:
        return ToolResult(ok=True, text=f"无 {state} issues")

    lines = [f"Issues ({state}, {len(data)} 条):"]
    for issue in data[:limit]:
        # Skip pull requests (GitHub returns them in /issues endpoint)
        if "pull_request" in issue:
            continue
        lines.append(_format_issue(issue))

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def github_issue(settings: Settings, number: str) -> ToolResult:
    """Get a specific issue by number."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    try:
        issue_num = int(number)
    except ValueError:
        return ToolResult(ok=False, text=f"⚠️ 无效的 issue 编号: {number}")

    owner, repo = _parse_repo(settings.github_default_repo)
    endpoint = f"/repos/{owner}/{repo}/issues/{issue_num}"

    ok, data = _github_request(settings, "GET", endpoint)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 获取 issue #{issue_num} 失败: {data}")

    title = data.get("title", "(no title)")
    state = data.get("state", "unknown")
    user = data.get("user", {}).get("login", "?")
    body = data.get("body", "")[:500] if data.get("body") else "(无描述)"
    created = data.get("created_at", "")[:16]
    updated = data.get("updated_at", "")[:16]
    comments = data.get("comments", 0)
    labels = ", ".join(l.get("name", "") for l in data.get("labels", []))

    lines = [
        f"Issue #{issue_num}: {title}",
        f"状态: {state} | 作者: @{user}",
        f"创建: {created} | 更新: {updated} | 评论: {comments}",
    ]
    if labels:
        lines.append(f"标签: {labels}")
    lines.append("")
    lines.append(body)

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def github_prs(
    settings: Settings,
    state: str = "open",
    limit: int = 20,
) -> ToolResult:
    """List pull requests."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    owner, repo = _parse_repo(settings.github_default_repo)
    endpoint = f"/repos/{owner}/{repo}/pulls"
    params = {"state": state, "per_page": min(limit, 100), "sort": "updated"}

    ok, data = _github_request(settings, "GET", endpoint, params=params)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 获取 PRs 失败: {data}")

    if not data:
        return ToolResult(ok=True, text=f"无 {state} PRs")

    lines = [f"Pull Requests ({state}, {len(data)} 条):"]
    for pr in data[:limit]:
        lines.append(_format_pr(pr))

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def github_pr(settings: Settings, number: str) -> ToolResult:
    """Get a specific PR by number."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    try:
        pr_num = int(number)
    except ValueError:
        return ToolResult(ok=False, text=f"⚠️ 无效的 PR 编号: {number}")

    owner, repo = _parse_repo(settings.github_default_repo)
    endpoint = f"/repos/{owner}/{repo}/pulls/{pr_num}"

    ok, data = _github_request(settings, "GET", endpoint)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 获取 PR #{pr_num} 失败: {data}")

    title = data.get("title", "(no title)")
    state = data.get("state", "unknown")
    merged = data.get("merged", False)
    user = data.get("user", {}).get("login", "?")
    body = data.get("body", "")[:500] if data.get("body") else "(无描述)"
    created = data.get("created_at", "")[:16]
    updated = data.get("updated_at", "")[:16]
    comments = data.get("comments", 0)
    review_comments = data.get("review_comments", 0)
    draft = data.get("draft", False)
    head = data.get("head", {}).get("ref", "?")
    base = data.get("base", {}).get("ref", "?")

    status = "merged" if merged else state
    draft_str = " (draft)" if draft else ""

    lines = [
        f"PR #{pr_num}: {title}{draft_str}",
        f"状态: {status} | 作者: @{user}",
        f"分支: {head} → {base}",
        f"创建: {created} | 更新: {updated}",
        f"评论: {comments} | Review: {review_comments}",
    ]
    lines.append("")
    lines.append(body)

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def github_ci(
    settings: Settings,
    ref_or_empty: str = "",
) -> ToolResult:
    """Get CI status for a ref (branch/commit SHA). Defaults to default branch."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    owner, repo = _parse_repo(settings.github_default_repo)

    # Get default branch if no ref specified
    if not ref_or_empty.strip():
        repo_ok, repo_data = _github_request(settings, "GET", f"/repos/{owner}/{repo}")
        if not repo_ok:
            return ToolResult(ok=False, text=f"⚠️ 获取仓库信息失败: {repo_data}")
        ref = repo_data.get("default_branch", "main")
    else:
        ref = ref_or_empty.strip()

    # Get check runs for the ref
    endpoint = f"/repos/{owner}/{repo}/commits/{ref}/check-runs"
    ok, data = _github_request(settings, "GET", endpoint)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 获取 CI 状态失败: {data}")

    check_runs = data.get("check_runs", [])
    if not check_runs:
        return ToolResult(ok=True, text=f"分支 {ref}: 无 CI 检查")

    total = len(check_runs)
    success = sum(1 for c in check_runs if c.get("conclusion") == "success")
    failure = sum(1 for c in check_runs if c.get("conclusion") == "failure")
    pending = sum(1 for c in check_runs if c.get("status") != "completed")

    lines = [
        f"CI 状态 ({ref}):",
        f"总计: {total} | 成功: {success} | 失败: {failure} | 进行中: {pending}",
        "",
    ]

    # Show check runs
    for check in check_runs[:10]:
        lines.append(_format_check_run(check))

    if total > 10:
        lines.append(f"  ... 还有 {total - 10} 个检查")

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def github_create_issue(
    settings: Settings,
    title: str,
    body: str = "",
) -> ToolResult:
    """Create a new issue. WRITE_SAFE with audit."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    if not title.strip():
        return ToolResult(ok=False, text="⚠️ Issue 标题不能为空")

    owner, repo = _parse_repo(settings.github_default_repo)
    endpoint = f"/repos/{owner}/{repo}/issues"

    json_body = {"title": title.strip()}
    if body.strip():
        json_body["body"] = body.strip()

    ok, data = _github_request(settings, "POST", endpoint, json_body=json_body)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 创建 issue 失败: {data}")

    number = data.get("number", "?")
    url = data.get("html_url", "")

    return ToolResult(ok=True, text=(
        f"✅ Issue #{number} 已创建\n"
        f"标题: {title}\n"
        f"链接: {url}"
    ))


def github_comment(
    settings: Settings,
    number: str,
    body: str,
) -> ToolResult:
    """Comment on an issue or PR. WRITE, requires confirmation."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=err)

    try:
        issue_num = int(number)
    except ValueError:
        return ToolResult(ok=False, text=f"⚠️ 无效的 issue/PR 编号: {number}")

    if not body.strip():
        return ToolResult(ok=False, text="⚠️ 评论内容不能为空")

    owner, repo = _parse_repo(settings.github_default_repo)
    endpoint = f"/repos/{owner}/{repo}/issues/{issue_num}/comments"

    json_body = {"body": body.strip()}

    ok, data = _github_request(settings, "POST", endpoint, json_body=json_body)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ 添加评论失败: {data}")

    url = data.get("html_url", "")

    return ToolResult(ok=True, text=(
        f"✅ 评论已添加到 #{issue_num}\n"
        f"链接: {url}"
    ))


def github_summary(settings: Settings) -> dict[str, Any]:
    """Get summary for Daily Briefing. Returns dict with open_issues, open_prs, ci_status."""
    result = {
        "open_issues": None,
        "open_prs": None,
        "ci_status": None,
    }

    err = _check_config(settings)
    if err:
        return result

    owner, repo = _parse_repo(settings.github_default_repo)

    # Get open issues count
    ok, data = _github_request(
        settings, "GET",
        f"/repos/{owner}/{repo}/issues",
        params={"state": "open", "per_page": 1}
    )
    if ok:
        # Count issues (not PRs)
        issues = [i for i in data if "pull_request" not in i] if isinstance(data, list) else []
        result["open_issues"] = len(issues)

    # Get open PRs count
    ok, data = _github_request(
        settings, "GET",
        f"/repos/{owner}/{repo}/pulls",
        params={"state": "open", "per_page": 1}
    )
    if ok and isinstance(data, list):
        result["open_prs"] = len(data)

    # Get CI status for default branch
    repo_ok, repo_data = _github_request(settings, "GET", f"/repos/{owner}/{repo}")
    if repo_ok:
        default_branch = repo_data.get("default_branch", "main")
        ci_ok, ci_data = _github_request(
            settings, "GET",
            f"/repos/{owner}/{repo}/commits/{default_branch}/check-runs"
        )
        if ci_ok:
            checks = ci_data.get("check_runs", [])
            if checks:
                success = sum(1 for c in checks if c.get("conclusion") == "success")
                failure = sum(1 for c in checks if c.get("conclusion") == "failure")
                total = len(checks)
                if failure > 0:
                    result["ci_status"] = f"❌ {failure}/{total} 失败"
                elif success == total:
                    result["ci_status"] = f"✅ {total}/{total} 通过"
                else:
                    result["ci_status"] = f"⏳ {total} 个检查中"
            else:
                result["ci_status"] = "无 CI"

    return result


# --- Adapters for personal_tools/registry.py ---

async def github_status_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return github_status(settings)


async def github_issues_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    query = arg.strip() if arg.strip() else "open"
    return github_issues(settings, query)


async def github_issue_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    if not arg.strip():
        return ToolResult(ok=False, text="⚠️ 用法: github.issue <number>")
    return github_issue(settings, arg.strip())


async def github_prs_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    state = arg.strip() if arg.strip() else "open"
    return github_prs(settings, state)


async def github_pr_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    if not arg.strip():
        return ToolResult(ok=False, text="⚠️ 用法: github.pr <number>")
    return github_pr(settings, arg.strip())


async def github_ci_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return github_ci(settings, arg.strip())


async def github_create_issue_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    # Parse "title | body" format
    parts = arg.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return github_create_issue(settings, title, body)


async def github_comment_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    # Parse "number | body" format
    parts = arg.split("|", 1)
    if len(parts) < 2:
        return ToolResult(ok=False, text="⚠️ 用法: github.comment <number> | <body>")
    number = parts[0].strip()
    body = parts[1].strip()
    return github_comment(settings, number, body)
