"""web_supervisor_api.py — Authenticated HTTP handlers for `/api/supervisor/*`."""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

import supervisor_service as svc
import supervisor_goals as goals_mod
import supervisor_validation as validation_mod
from redaction import redact_text, truncate

logger = logging.getLogger("conveyor.web_supervisor_api")


def _resolve_settings(handler: Any) -> Any:
    settings = getattr(getattr(handler, "server", None), "control", None)
    settings = getattr(settings, "settings", None)
    if settings is None:
        from config import load_runtime_settings
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

        if len(parts) == 4 and parts[:3] == ["api", "supervisor", "tasks"]:
            detail = svc.get_scoped_task(settings, parts[3])
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
                    "direct browser task transitions are disabled; use /dispatch or /cancel",
                    code="transition_disabled",
                )

        if path == "/api/supervisor/deployments":
            run = svc.start_scoped_deployment(settings, body)
            handler._json(HTTPStatus.ACCEPTED, run)
            return True

        if len(parts) == 5 and parts[:3] == ["api", "supervisor", "deployments"] and parts[4] == "stage":
            raise svc.SupervisorServiceError(
                HTTPStatus.CONFLICT,
                "browser deployment stage mutation is disabled",
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
