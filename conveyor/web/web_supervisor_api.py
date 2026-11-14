"""web_supervisor_api.py — Authenticated HTTP handlers for `/api/supervisor/*`."""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from conveyor.agents import supervisor_service as svc
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_merge as merge_mod
from conveyor.agents import supervisor_validation as validation_mod
from conveyor.core.redaction import redact_text, truncate

logger = logging.getLogger("conveyor.web_supervisor_api")


def _resolve_settings(handler: Any) -> Any:
    settings = getattr(getattr(handler, "server", None), "control", None)
    settings = getattr(settings, "settings", None)
    if settings is None:
        from conveyor.core.config import load_runtime_settings
        try:
            settings = load_runtime_settings()
        except Exception:
            return None
    return settings


def handle_get(
    handler: Any,
    path: str,
    parts: list[str],
    query: dict[str, list[str]],
) -> bool:
    """Dispatch `GET /api/supervisor/*`. Assumes caller already verified bearer auth."""
    if not (path == "/api/supervisor" or path.startswith("/api/supervisor/")):
        return False

    settings = _resolve_settings(handler)
    try:
        svc.require_enabled(settings)

        if path == "/api/supervisor/capabilities":
            handler._json(HTTPStatus.OK, svc.get_capabilities(settings))
            return True

        if path == "/api/supervisor/repositories":
            repos = svc.list_repositories(settings)
            handler._json(HTTPStatus.OK, {"repositories": repos})
            return True

        if path == "/api/supervisor/tasks":
            repo_id = (query.get("repo_id") or [""])[0].strip() or None
            worktree_id = (query.get("worktree_id") or [""])[0].strip() or None
            state = (query.get("state") or [""])[0].strip() or None
            tasks = svc.list_scoped_tasks(
                settings,
                repo_id=repo_id,
                worktree_id=worktree_id,
                state=state,
            )
            handler._json(HTTPStatus.OK, {"tasks": tasks, "count": len(tasks)})
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "repositories"] and parts[4] == "validation":
            detail = validation_mod.get_repo_validation(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "worktrees"] and parts[4] == "merge-preview":
            detail = merge_mod.build_merge_preview(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if len(parts) == 4 and parts[:3] == ["api", "supervisor", "tasks"]:
            detail = svc.get_scoped_task(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "tasks"] and parts[4] == "diff":
            detail = svc.get_task_diff(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if path == "/api/supervisor/goals":
            goals = goals_mod.list_goals(settings)
            handler._json(HTTPStatus.OK, {"goals": goals, "count": len(goals)})
            return True

        if len(parts) == 4 and parts[:3] == ["api", "supervisor", "goals"]:
            detail = goals_mod.get_goal(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "goals"] and parts[4] == "diff":
            detail = goals_mod.get_goal_diff(settings, parts[3])
            handler._json(HTTPStatus.OK, detail)
            return True

        if path == "/api/supervisor/deployments":
            task_id = (query.get("task_id") or [""])[0].strip() or None
            deployments = svc.list_scoped_deployments(settings, task_id=task_id)
            handler._json(HTTPStatus.OK, {"deployments": deployments, "count": len(deployments)})
            return True

        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    except svc.SupervisorServiceError as exc:
        handler._json(exc.status, exc.to_payload())
        return True
    except Exception as exc:
        logger.exception("Unhandled error in GET %s", path)
        handler._json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": truncate(redact_text(f"internal error: {type(exc).__name__}"), 200)},
        )
        return True


def _header_value(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _host_authority(host: str) -> tuple[str, int | None] | None:
    """``(hostname, port)`` from a Host header. IPv6 must be bracketed. None if malformed."""
    raw = host.strip()
    if not raw or any(ch in raw for ch in "\r\n\x00 \t"):
        return None
    try:
        if raw.startswith("["):
            end = raw.find("]")
            if end <= 1:
                return None
            name = raw[1:end]
            rest = raw[end + 1:]
            if not name:
                return None
            if rest == "":
                return name.casefold(), None
            if not rest.startswith(":"):
                return None
            port = int(rest[1:])
        elif raw.count(":") > 1:
            return None
        elif ":" in raw:
            name, port_text = raw.rsplit(":", 1)
            if not name:
                return None
            port = int(port_text)
        else:
            return raw.casefold(), None
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return name.casefold(), port


def _origin_authority(origin: str) -> tuple[str, int] | None:
    """``(hostname, port)`` from an Origin. Missing ports use 80 or 443. None if malformed."""
    try:
        parsed = urlparse(origin.strip())
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not 1 <= port <= 65535:
        return None
    return hostname.casefold(), port


def project_create_block_reason(headers: Any) -> str | None:
    """Block a cross-site create. None means this POST may create a project.

    Requires JSON and ``X-Requested-With: XMLHttpRequest``. ``Sec-Fetch-Site``
    of ``cross-site`` or ``same-site`` is rejected. A present Origin must match
    the Host authority, including port and IPv6. ``X-Forwarded-Host`` is ignored.
    Origin may be absent for a non-browser client that still sends the two
    required headers. Malformed values return a reason instead of raising.
    """
    try:
        content_type = _header_value(headers, "Content-Type").lower()
        if "application/json" not in content_type:
            return "content_type"
        if _header_value(headers, "X-Requested-With") != "XMLHttpRequest":
            return "x_requested_with"
        fetch_site = _header_value(headers, "Sec-Fetch-Site").lower()
        if fetch_site in {"cross-site", "same-site"}:
            return "sec_fetch_site"
        origin = _header_value(headers, "Origin")
        if not origin:
            return None
        host_auth = _host_authority(_header_value(headers, "Host"))
        origin_auth = _origin_authority(origin)
        if host_auth is None or origin_auth is None:
            return "origin"
        host_name, host_port = host_auth
        origin_name, origin_port = origin_auth
        if host_name != origin_name:
            return "origin"
        if host_port is None:
            if origin_port not in {80, 443}:
                return "origin"
        elif host_port != origin_port:
            return "origin"
        return None
    except Exception:
        return "origin"


def handle_post(
    handler: Any,
    path: str,
    parts: list[str],
    body: dict[str, Any],
) -> bool:
    """Dispatch `POST /api/supervisor/*`. Assumes caller verified auth and parsed JSON body."""
    if not (path == "/api/supervisor" or path.startswith("/api/supervisor/")):
        return False

    settings = _resolve_settings(handler)
    try:
        svc.require_enabled(settings)

        if path == "/api/supervisor/projects":
            blocked = project_create_block_reason(getattr(handler, "headers", None))
            if blocked is not None:
                handler._json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "请求未通过站点校验", "code": "origin_or_request_denied"},
                )
                return True
            created_project = svc.create_managed_project(settings, name=body.get("name"))
            handler._json(HTTPStatus.CREATED, created_project)
            return True

        if path == "/api/supervisor/repositories":
            repo, created = svc.register_repository(
                settings,
                path=body.get("path"),
                name=body.get("name"),
            )
            handler._json(HTTPStatus.CREATED if created else HTTPStatus.OK, repo)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "repositories"] and parts[4] == "worktrees":
            wt = svc.create_managed_worktree(
                settings,
                parts[3],
                branch=body.get("branch"),
                base_ref=body.get("base_ref"),
            )
            handler._json(HTTPStatus.CREATED, wt)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "repositories"] and parts[4] == "validation":
            detail = validation_mod.set_repo_validation(settings, parts[3], body.get("selected"))
            handler._json(HTTPStatus.OK, detail)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "worktrees"] and parts[4] == "merge":
            result = merge_mod.merge_worktree(settings, parts[3], body)
            handler._json(HTTPStatus.OK, result)
            return True

        if path == "/api/supervisor/tasks":
            task, created = svc.create_scoped_task(settings, body)
            handler._json(HTTPStatus.CREATED if created else HTTPStatus.OK, task)
            return True

        if path == "/api/supervisor/goals":
            goal = goals_mod.create_goal(handler.server, settings, body)
            handler._json(HTTPStatus.CREATED, goal)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "goals"]:
            goal_id = parts[3]
            action = parts[4]
            if action == "stop":
                stopped = goals_mod.stop_goal(handler.server, settings, goal_id)
                handler._json(HTTPStatus.OK, stopped)
                return True
            if action == "reply":
                replied = goals_mod.reply_goal(handler.server, settings, goal_id, body)
                handler._json(HTTPStatus.OK, replied)
                return True
            if action == "extend":
                extended = goals_mod.extend_goal(handler.server, settings, goal_id, body)
                handler._json(HTTPStatus.OK, extended)
                return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "tasks"]:
            task_id = parts[3]
            action = parts[4]
            if action == "dispatch":
                queued = svc.dispatch_scoped_task(handler.server, settings, task_id, body)
                handler._json(HTTPStatus.ACCEPTED, queued)
                return True
            if action == "cancel":
                cancelled = svc.cancel_scoped_task(
                    handler.server,
                    settings,
                    task_id,
                    reason=body.get("reason"),
                )
                handler._json(HTTPStatus.OK, cancelled)
                return True
            if action == "transition":
                raise svc.SupervisorServiceError(
                    HTTPStatus.CONFLICT,
                    "这个操作不能在页面上直接做，请用「开始执行」或「取消」",
                    code="transition_disabled",
                )

        if path == "/api/supervisor/deployments":
            run = svc.start_scoped_deployment(settings, body)
            handler._json(HTTPStatus.ACCEPTED, run)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "deployments"] and parts[4] == "stage":
            raise svc.SupervisorServiceError(
                HTTPStatus.CONFLICT,
                "远程执行节点的阶段不能在页面上改",
                code="stage_mutation_disabled",
            )

        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    except svc.SupervisorServiceError as exc:
        handler._json(exc.status, exc.to_payload())
        return True
    except Exception as exc:
        logger.exception("Unhandled error in POST %s", path)
        handler._json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": truncate(redact_text(f"internal error: {type(exc).__name__}"), 200)},
        )
        return True
