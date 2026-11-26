"""github_workspace.py — GitHub CLI (`gh`) repository discovery and conversation workspace binding.

Allows a new or active conversation (Session or Agent) to bind to either:
  1. A local workspace folder on the host (`source="folder"`), or
  2. A GitHub repository from the operator's `gh`-connected account (`source="github"`).

When a GitHub repository (`owner/repo`) is selected:
  - If a local checkout already exists on the host (in the configured workspace,
    sibling folders, `~/Documents/GitHub`, or `<codex_task_root>/repos`), it is
    reused immediately.
  - Otherwise, it is cloned via `gh repo clone` (or `git clone`) into
    `<codex_task_root>/repos/<owner>/<repo>` and bound to the conversation.
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from redaction import redact_text, truncate

logger = logging.getLogger("conveyor.github_workspace")

_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_GITHUB_URL_RE = re.compile(
    r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

# Short in-memory cache so rapid UI opens don't shell out to `gh` repeatedly.
_GH_STATUS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_GH_REPOS_CACHE: dict[str, Any] = {"ts": 0.0, "limit": 0, "data": None}
_CACHE_TTL_SECONDS = 20.0


def invalidate_gh_cache() -> None:
    _GH_STATUS_CACHE["ts"] = 0.0
    _GH_STATUS_CACHE["data"] = None
    _GH_REPOS_CACHE["ts"] = 0.0
    _GH_REPOS_CACHE["data"] = None


def is_valid_repo_full_name(value: str) -> bool:
    if not value or ".." in value:
        return False
    return bool(_REPO_FULL_NAME_RE.match(value.strip()))


def workspace_status_for_path(workspace_path: str | Path | None) -> str:
    """Return '' | 'ok' | 'not_git' | 'missing' for a workspace path."""
    raw = str(workspace_path or "").strip()
    if not raw:
        return ""
    try:
        folder = Path(raw).expanduser()
        if not folder.exists() or not folder.is_dir():
            return "missing"
        if (folder / ".git").exists():
            return "ok"
        return "not_git"
    except OSError:
        return "missing"


def detect_git_remote_repo(folder: Path) -> str:
    """Inspect `.git/config` (or git dir) for a GitHub `origin` remote `owner/repo`."""
    try:
        git_entry = folder / ".git"
        git_dir = git_entry
        if git_entry.is_file():
            content = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                rel = content[len("gitdir:"):].strip()
                git_dir = (folder / rel).resolve()
                commondir_file = git_dir / "commondir"
                if commondir_file.is_file():
                    common_rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
                    git_dir = (git_dir / common_rel).resolve()
        config_path = git_dir / "config"
        if not config_path.is_file():
            return ""
        parser = configparser.ConfigParser(strict=False)
        parser.read(config_path, encoding="utf-8")
        for section in ('remote "origin"', "remote 'origin'"):
            if parser.has_section(section) and parser.has_option(section, "url"):
                url = parser.get(section, "url").strip()
                m = _GITHUB_URL_RE.search(url)
                if m:
                    return m.group(1)
        for section in parser.sections():
            if section.startswith("remote ") and parser.has_option(section, "url"):
                url = parser.get(section, "url").strip()
                m = _GITHUB_URL_RE.search(url)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def _candidate_scan_roots(settings: Any) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path | None) -> None:
        if p is None:
            return
        try:
            resolved = p.expanduser().resolve()
        except OSError:
            return
        key = str(resolved)
        if key not in seen and resolved.is_dir():
            seen.add(key)
            roots.append(resolved)

    ws_root = getattr(settings, "codex_workspace_root", None)
    if ws_root:
        ws_path = Path(ws_root)
        _add(ws_path)
        _add(ws_path.parent)
    task_root = getattr(settings, "codex_task_root", None)
    if task_root:
        _add(Path(task_root) / "repos")
    _add(Path.home() / "Documents" / "GitHub")
    _add(Path.home() / "GitHub")
    _add(Path.home() / "projects")
    _add(Path("/srv"))
    return roots


def discover_local_git_repos(settings: Any, *, max_results: int = 80) -> list[dict[str, Any]]:
    """Find local git repositories on the host for quick folder/GitHub binding."""
    found: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def _inspect_dir(folder: Path) -> None:
        if len(found) >= max_results:
            return
        try:
            if not folder.is_dir() or folder.name.startswith("."):
                return
            resolved = folder.resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen_paths:
            return
        if not (resolved / ".git").exists():
            return
        seen_paths.add(key)
        gh_repo = detect_git_remote_repo(resolved)
        found.append({
            "name": resolved.name,
            "path": key,
            "github_repo": gh_repo,
            "workspace_status": "ok",
        })

    ws_root = getattr(settings, "codex_workspace_root", None)
    if ws_root:
        _inspect_dir(Path(ws_root))

    for root in _candidate_scan_roots(settings):
        _inspect_dir(root)
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children[:200]:
            _inspect_dir(child)
            # Managed `<task_root>/repos/<owner>/<repo>` is 2 levels deep
            if root.name == "repos" and child.is_dir() and not (child / ".git").exists():
                try:
                    for sub in sorted(child.iterdir(), key=lambda p: p.name.lower())[:50]:
                        _inspect_dir(sub)
                except OSError:
                    continue
    return found


def gh_auth_status(*, use_cache: bool = True) -> dict[str, Any]:
    """Check `gh` CLI installation and authentication status."""
    now = time.monotonic()
    if use_cache and _GH_STATUS_CACHE["data"] is not None and (now - _GH_STATUS_CACHE["ts"]) < _CACHE_TTL_SECONDS:
        return dict(_GH_STATUS_CACHE["data"])

    gh_bin = shutil.which("gh")
    if not gh_bin:
        result = {
            "installed": False,
            "connected": False,
            "account": "",
            "name": "",
            "error": "GitHub CLI (gh) 未安装。请先安装 gh 并执行 gh auth login。",
        }
        _GH_STATUS_CACHE.update(ts=now, data=result)
        return dict(result)

    try:
        proc = subprocess.run(
            [gh_bin, "api", "user"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            user_data = json.loads(proc.stdout)
            account = str(user_data.get("login") or "").strip()
            name = str(user_data.get("name") or "").strip()
            result = {
                "installed": True,
                "connected": bool(account),
                "account": account,
                "name": name,
                "error": "",
            }
            _GH_STATUS_CACHE.update(ts=now, data=result)
            return dict(result)
    except Exception:
        pass

    # Fallback: parse `gh auth status`
    try:
        proc = subprocess.run(
            [gh_bin, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        if proc.returncode == 0:
            m = re.search(r"account\s+([A-Za-z0-9_.-]+)", combined)
            account = m.group(1) if m else "connected"
            result = {
                "installed": True,
                "connected": True,
                "account": account,
                "name": "",
                "error": "",
            }
            _GH_STATUS_CACHE.update(ts=now, data=result)
            return dict(result)
        err_text = truncate(redact_text(combined.splitlines()[0] if combined else "gh not authenticated"), 200)
    except Exception as exc:
        err_text = truncate(redact_text(str(exc)), 200)

    result = {
        "installed": True,
        "connected": False,
        "account": "",
        "name": "",
        "error": err_text or "gh 未登录，请在终端运行 gh auth login",
    }
    _GH_STATUS_CACHE.update(ts=now, data=result)
    return dict(result)


def gh_auth_token() -> str | None:
    """Retrieve the active GitHub token from `gh auth token` (never exposed to clients)."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return None
    try:
        proc = subprocess.run(
            [gh_bin, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        token = (proc.stdout or "").strip()
        if proc.returncode == 0 and token:
            return token
    except Exception:
        pass
    return None


def list_gh_repos(
    settings: Any,
    *,
    limit: int = 100,
    query: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    """List repositories from `gh repo list` (with local checkout matching)."""
    capped_limit = min(max(1, int(limit or 100)), 200)
    now = time.monotonic()
    local_repos = discover_local_git_repos(settings, max_results=120)
    local_by_full_name: dict[str, str] = {}
    local_by_name: dict[str, str] = {}
    for item in local_repos:
        gh_slug = str(item.get("github_repo") or "").strip().lower()
        if gh_slug and gh_slug not in local_by_full_name:
            local_by_full_name[gh_slug] = item["path"]
        name_slug = str(item.get("name") or "").strip().lower()
        if name_slug and name_slug not in local_by_name:
            local_by_name[name_slug] = item["path"]

    raw_repos: list[dict[str, Any]] | None = None
    status = gh_auth_status(use_cache=not refresh)

    if (
        not refresh
        and _GH_REPOS_CACHE["data"] is not None
        and _GH_REPOS_CACHE["limit"] >= capped_limit
        and (now - _GH_REPOS_CACHE["ts"]) < _CACHE_TTL_SECONDS
    ):
        raw_repos = list(_GH_REPOS_CACHE["data"])

    gh_bin = shutil.which("gh")
    if raw_repos is None and gh_bin:
        try:
            proc = subprocess.run(
                [
                    gh_bin,
                    "repo",
                    "list",
                    "--limit",
                    str(capped_limit),
                    "--json",
                    "nameWithOwner,name,description,isPrivate,updatedAt,defaultBranchRef,url",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, list):
                    raw_repos = parsed
                    _GH_REPOS_CACHE.update(ts=now, limit=capped_limit, data=raw_repos)
                    if not status.get("connected") and raw_repos:
                        first_owner = str(raw_repos[0].get("nameWithOwner") or "").split("/")[0]
                        status["connected"] = True
                        status["account"] = status.get("account") or first_owner
                        status["error"] = ""
            elif proc.stderr:
                status["error"] = truncate(redact_text(proc.stderr.strip()), 240)
        except Exception as exc:
            status["error"] = truncate(redact_text(str(exc)), 240)

    # Fallback to GITHUB_TOKEN REST API if `gh` returned nothing
    if raw_repos is None and getattr(settings, "github_token", None):
        try:
            from personal_tools.github_tools import _github_request
            ok, data = _github_request(
                settings,
                "GET",
                "/user/repos",
                params={"sort": "updated", "per_page": min(capped_limit, 100)},
            )
            if ok and isinstance(data, list):
                raw_repos = [
                    {
                        "nameWithOwner": item.get("full_name") or "",
                        "name": item.get("name") or "",
                        "description": item.get("description") or "",
                        "isPrivate": bool(item.get("private")),
                        "updatedAt": item.get("updated_at") or "",
                        "defaultBranchRef": {"name": item.get("default_branch") or "main"},
                        "url": item.get("html_url") or "",
                    }
                    for item in data
                    if isinstance(item, dict) and item.get("full_name")
                ]
                status["connected"] = True
                if not status.get("account") and raw_repos:
                    status["account"] = str(raw_repos[0]["nameWithOwner"]).split("/")[0]
                status["error"] = ""
        except Exception:
            pass

    repos: list[dict[str, Any]] = []
    q = (query or "").strip().lower()
    for item in (raw_repos or []):
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("nameWithOwner") or "").strip()
        if not full_name:
            continue
        name = str(item.get("name") or full_name.split("/")[-1]).strip()
        desc = str(item.get("description") or "").strip()
        branch_ref = item.get("defaultBranchRef")
        default_branch = (
            str(branch_ref.get("name") or "main")
            if isinstance(branch_ref, dict)
            else "main"
        )
        local_path = (
            local_by_full_name.get(full_name.lower())
            or local_by_name.get(name.lower())
            or ""
        )
        # Also check managed clone directory
        if not local_path and getattr(settings, "codex_task_root", None):
            managed = Path(settings.codex_task_root) / "repos" / full_name
            if (managed / ".git").exists():
                local_path = str(managed.resolve())

        entry = {
            "full_name": full_name,
            "name": name,
            "description": desc,
            "private": bool(item.get("isPrivate")),
            "default_branch": default_branch,
            "updated_at": str(item.get("updatedAt") or ""),
            "url": str(item.get("url") or f"https://github.com/{full_name}"),
            "local_path": local_path,
            "cloned": bool(local_path),
        }
        if q and q not in full_name.lower() and q not in name.lower() and q not in desc.lower():
            continue
        repos.append(entry)

    auth_info = {
        "installed": bool(status.get("installed")),
        "connected": bool(status.get("connected")),
        "authenticated": bool(status.get("connected")),
        "account": str(status.get("account") or ""),
        "name": str(status.get("name") or ""),
        "error": str(status.get("error") or ""),
    }
    return {
        "ok": True,
        **auth_info,
        "auth": auth_info,
        "repos": repos,
        "count": len(repos),
    }


def validate_local_folder(path_str: str) -> dict[str, Any]:
    """Validate a local workspace folder path and inspect its git status."""
    raw = str(path_str or "").strip()
    if not raw or len(raw) > 512 or "\n" in raw or "\x00" in raw:
        raise ValueError("请输入有效的本地文件夹路径")
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise ValueError("工作文件夹路径必须是绝对路径（例如 /Users/... 或 /srv/...）")
    try:
        resolved = folder.resolve()
    except OSError as exc:
        raise ValueError(f"无法解析路径: {exc}") from exc
    if not resolved.exists():
        raise ValueError(f"文件夹不存在: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"路径不是文件夹: {resolved}")

    status = workspace_status_for_path(resolved)
    github_repo = detect_git_remote_repo(resolved) if status == "ok" else ""
    return {
        "workspace_path": str(resolved),
        "workspace_status": status,
        "github_repo": github_repo,
        "name": resolved.name,
    }


def ensure_github_repo_workspace(
    settings: Any,
    full_name: str,
    *,
    preferred_path: str = "",
) -> dict[str, Any]:
    """Ensure a local git checkout exists for `owner/repo` and return its workspace info."""
    repo_slug = str(full_name or "").strip()
    if not is_valid_repo_full_name(repo_slug):
        raise ValueError("无效的 GitHub 仓库名称（格式应为 owner/repo）")

    # 1. Check explicit preferred_path if provided and already a git checkout
    if preferred_path:
        pref = Path(preferred_path).expanduser()
        if pref.is_dir() and (pref / ".git").exists():
            resolved = pref.resolve()
            return {
                "full_name": repo_slug,
                "github_repo": repo_slug,
                "workspace_path": str(resolved),
                "workspace_status": "ok",
                "cloned_now": False,
                "name": resolved.name,
            }

    # 2. Check existing local repositories matching `owner/repo`
    owner, repo_name = repo_slug.split("/", 1)
    for item in discover_local_git_repos(settings, max_results=150):
        item_gh = str(item.get("github_repo") or "").strip()
        if item_gh.lower() == repo_slug.lower():
            return {
                "full_name": repo_slug,
                "github_repo": repo_slug,
                "workspace_path": item["path"],
                "workspace_status": "ok",
                "cloned_now": False,
                "name": repo_name,
            }

    # Also check by exact directory name if its remote matches or has no conflicting remote
    for item in discover_local_git_repos(settings, max_results=150):
        if str(item.get("name") or "").lower() == repo_name.lower():
            item_gh = str(item.get("github_repo") or "").strip()
            if not item_gh or item_gh.lower() == repo_slug.lower():
                return {
                    "full_name": repo_slug,
                    "github_repo": repo_slug,
                    "workspace_path": item["path"],
                    "workspace_status": "ok",
                    "cloned_now": False,
                    "name": repo_name,
                }

    # 3. Check managed clone directory `<codex_task_root>/repos/<owner>/<repo>`
    task_root = Path(getattr(settings, "codex_task_root", None) or (Path.home() / ".conveyor" / "tasks"))
    target_dir = (task_root / "repos" / owner / repo_name).resolve()
    if (target_dir / ".git").exists():
        return {
            "full_name": repo_slug,
            "github_repo": repo_slug,
            "workspace_path": str(target_dir),
            "workspace_status": "ok",
            "cloned_now": False,
            "name": repo_name,
        }

    # 4. Clone the repository into `target_dir` using `gh repo clone` or `git clone`
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    gh_bin = shutil.which("gh")
    clone_cmd = (
        [gh_bin, "repo", "clone", repo_slug, str(target_dir)]
        if gh_bin
        else ["git", "clone", f"https://github.com/{repo_slug}.git", str(target_dir)]
    )
    try:
        proc = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Clone 仓库 {repo_slug} 超时，请检查网络连接") from exc
    except OSError as exc:
        raise ValueError(f"无法执行 git/gh clone: {redact_text(str(exc))}") from exc

    if proc.returncode != 0 or not (target_dir / ".git").exists():
        err_msg = truncate(redact_text((proc.stderr or proc.stdout or "clone failed").strip()), 300)
        raise ValueError(f"Clone 仓库 {repo_slug} 失败: {err_msg}")

    invalidate_gh_cache()
    return {
        "full_name": repo_slug,
        "github_repo": repo_slug,
        "workspace_path": str(target_dir),
        "workspace_status": "ok",
        "cloned_now": True,
        "name": repo_name,
    }


def binding_for_chat(settings: Any, channel: str, chat_id: str) -> dict[str, str]:
    """Return `{'workspace_path': ..., 'github_repo': ..., 'workspace_source': ...}` for a chat."""
    result = {"workspace_path": "", "github_repo": "", "workspace_source": ""}
    if not settings or not channel or not chat_id:
        return result
    try:
        from transcript_store import get_transcript_store
        session = get_transcript_store(settings).session_for_chat(channel, chat_id)
        if session:
            ws = str(session.get("workspace_path") or "").strip()
            gh = str(session.get("github_repo") or "").strip()
            src = str(session.get("workspace_source") or "").strip()
            if ws or gh:
                return {
                    "workspace_path": ws,
                    "github_repo": gh,
                    "workspace_source": src or ("github" if gh else "folder"),
                }
    except Exception:
        pass

    try:
        import agents
        agent = agents.agent_for_chat(settings, channel, chat_id)
        if agent:
            ws = str(agent.get("workspace_path") or "").strip()
            gh = str(agent.get("github_repo") or "").strip()
            src = str(agent.get("workspace_source") or "").strip()
            if ws and not gh:
                gh = detect_git_remote_repo(Path(ws))
            if ws or gh:
                return {
                    "workspace_path": ws,
                    "github_repo": gh,
                    "workspace_source": src or ("github" if gh else ("folder" if ws else "")),
                }
    except Exception:
        pass

    return result


def workspace_prompt_block(settings: Any, channel: str, chat_id: str) -> str:
    """Prompt context block describing the bound workspace/GitHub repo for this conversation."""
    info = binding_for_chat(settings, channel, chat_id)
    ws = info.get("workspace_path") or ""
    gh = info.get("github_repo") or ""
    if not ws and not gh:
        return ""
    parts: list[str] = []
    if gh:
        parts.append(f"GitHub repository: {gh}")
    if ws:
        parts.append(f"Local workspace path: {ws}")
    joined = " | ".join(parts)
    return (
        f'<workspace-binding source="{info.get("workspace_source") or "bound"}">\n'
        f"This conversation is bound to: {joined}\n"
        f"</workspace-binding>\n\n"
    )
