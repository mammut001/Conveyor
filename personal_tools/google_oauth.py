"""google_oauth.py — Google OAuth broker for Conveyor personal tools.

Handles credential loading, auth flow, refresh, and revocation for
Google Calendar and Contacts APIs. Tokens are stored at
codex_memory_root/secrets/google_token.json with chmod 600.

Gmail remains App Password backend (P3.3); OAuth only for Calendar/Contacts.

Dependencies: google-auth, google-auth-oauthlib, google-api-python-client.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# Default scopes: calendar read/write + contacts read
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts.readonly",
)

# Lazy imports for optional dependencies
_google_auth = None
_google_auth_oauthlib = None
_googleapiclient = None


def _import_deps():
    """Lazy import google auth libraries. Returns error string or None."""
    global _google_auth, _google_auth_oauthlib, _googleapiclient
    if _google_auth is not None:
        return None
    try:
        import google.auth
        import google_auth_oauthlib.flow
        import googleapiclient.discovery
        _google_auth = google.auth
        _google_auth_oauthlib = google_auth_oauthlib
        _googleapiclient = googleapiclient
        return None
    except ImportError as exc:
        return f"缺少 Google 依赖: {exc}. 请运行: pip install google-auth google-auth-oauthlib google-api-python-client"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _token_path(settings: Settings) -> Path:
    """Return the token file path (default: codex_memory_root/secrets/google_token.json)."""
    if settings.google_token_path:
        return Path(settings.google_token_path).expanduser().resolve()
    return settings.codex_memory_root / "secrets" / "google_token.json"


def _client_secret_path(settings: Settings) -> Path | None:
    """Return the client secret file path, or None if not configured."""
    if settings.google_client_secret_path:
        return Path(settings.google_client_secret_path).expanduser().resolve()
    return None


def _parse_scopes(settings: Settings) -> list[str]:
    """Parse scopes from settings or return defaults."""
    if settings.google_oauth_scopes:
        return [s.strip() for s in settings.google_oauth_scopes.split(",") if s.strip()]
    return list(DEFAULT_SCOPES)


def _check_config(settings: Settings) -> str | None:
    """Return error message if Google OAuth config is missing."""
    err = _import_deps()
    if err:
        return err
    secret_path = _client_secret_path(settings)
    if not secret_path:
        return "GOOGLE_CLIENT_SECRET_PATH 未设置（需要 Google Cloud Console 下载的 client_secret JSON）"
    if not secret_path.exists():
        return f"client_secret 文件不存在: {secret_path.name}"
    return None


def _save_token(token_data: dict, path: Path) -> None:
    """Save token JSON with chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600


def _load_token_json(path: Path) -> dict | None:
    """Load token JSON from file, or None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def google_status(settings: Settings) -> ToolResult:
    """Report Google OAuth status without exposing tokens."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    tok_path = _token_path(settings)
    tok_data = _load_token_json(tok_path)

    if tok_data is None:
        return ToolResult(ok=True, text=(
            "ℹ️ Google OAuth 未授权\n"
            f"token 路径: {tok_path.name}\n"
            "使用 /auth_google 开始授权流程"
        ))

    # Check if token has expiry
    expiry = tok_data.get("expiry", "")
    scopes = tok_data.get("scopes", [])

    lines = [
        "✅ Google OAuth 已授权",
        f"token 路径: {tok_path.name}",
        f"scopes: {', '.join(scopes) if scopes else '(未知)'}",
    ]
    if expiry:
        lines.append(f"过期时间: {expiry}")

    # Try to refresh to verify validity
    creds = load_credentials(settings)
    if creds is None:
        lines.append("⚠️ token 已过期或无效，需要重新授权: /auth_google")
    elif hasattr(creds, "valid") and creds.valid:
        lines.append("token 状态: 有效")
    else:
        lines.append("token 状态: 需刷新")

    return ToolResult(ok=True, text="\n".join(lines))


def load_credentials(settings: Settings):
    """Load and optionally refresh Google OAuth credentials.

    Returns google.oauth2.credentials.Credentials or None if unavailable.
    """
    err = _import_deps()
    if err:
        logger.warning("Google deps missing: %s", err)
        return None

    tok_path = _token_path(settings)
    tok_data = _load_token_json(tok_path)
    if tok_data is None:
        return None

    try:
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            logger.warning("google.oauth2.credentials not available (missing cryptography?)")
            return None

        creds = Credentials(
            token=tok_data.get("token"),
            refresh_token=tok_data.get("refresh_token"),
            token_uri=tok_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=tok_data.get("client_id"),
            client_secret=tok_data.get("client_secret"),
            scopes=tok_data.get("scopes"),
        )
        # Refresh if expired
        if creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                # Save refreshed token
                _save_token({
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": list(creds.scopes or []),
                    "expiry": creds.expiry.isoformat() if creds.expiry else "",
                }, tok_path)
            except Exception as exc:
                logger.warning("Token refresh failed: %s", exc)
                return None
        return creds
    except Exception as exc:
        logger.warning("Failed to load credentials: %s", exc)
        return None


def start_auth_flow(settings: Settings) -> ToolResult:
    """Start the OAuth authorization flow.

    Returns instructions for the user to complete auth in their browser.
    In a headless VPS environment, the user must run the URL manually.
    """
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    secret_path = _client_secret_path(settings)
    scopes = _parse_scopes(settings)
    port = settings.google_oauth_redirect_port

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secret_path), scopes=scopes,
        )

        # Try local server flow (works on desktop)
        try:
            creds = flow.run_local_server(port=port, open_browser=False)
        except Exception:
            # Fallback: manual URL for headless VPS
            auth_url, _ = flow.authorization_url(prompt="consent")
            return ToolResult(ok=True, text=(
                "🔐 Google OAuth 授权流程\n\n"
                "请在浏览器中打开以下链接完成授权:\n"
                f"{auth_url}\n\n"
                "授权完成后，将获得的授权码通过以下命令发送:\n"
                f"/auth_google <授权码>\n\n"
                f"scopes: {', '.join(scopes)}\n"
                f"redirect port: {port}"
            ))

        # Save token
        tok_path = _token_path(settings)
        _save_token({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
            "expiry": creds.expiry.isoformat() if creds.expiry else "",
        }, tok_path)

        return ToolResult(ok=True, text=(
            "✅ Google OAuth 授权成功！\n"
            f"token 已保存到: {tok_path.name}\n"
            f"scopes: {', '.join(scopes)}"
        ))

    except FileNotFoundError:
        return ToolResult(ok=False, text=f"⚠️ client_secret 文件不存在: {secret_path.name}")
    except json.JSONDecodeError:
        return ToolResult(ok=False, text="⚠️ client_secret 文件格式无效（需要 JSON）")
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 授权失败: {redact_text(str(exc))}")


def start_auth_flow_with_code(settings: Settings, code: str) -> ToolResult:
    """Complete OAuth flow with an authorization code (for headless VPS)."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    secret_path = _client_secret_path(settings)
    scopes = _parse_scopes(settings)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secret_path), scopes=scopes,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        tok_path = _token_path(settings)
        _save_token({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or []),
            "expiry": creds.expiry.isoformat() if creds.expiry else "",
        }, tok_path)

        return ToolResult(ok=True, text=(
            "✅ Google OAuth 授权成功！\n"
            f"token 已保存到: {tok_path.name}\n"
            f"scopes: {', '.join(scopes)}"
        ))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 授权失败: {redact_text(str(exc))}")


def revoke_credentials(settings: Settings) -> ToolResult:
    """Revoke and delete stored Google OAuth credentials."""
    tok_path = _token_path(settings)
    tok_data = _load_token_json(tok_path)

    if tok_data is None:
        return ToolResult(ok=True, text="ℹ️ 没有已存储的 Google OAuth token")

    # Try to revoke via Google API
    try:
        import urllib.request
        token = tok_data.get("token")
        if token:
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/revoke",
                data=f"token={token}".encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best-effort revoke

    # Delete local token file
    try:
        tok_path.unlink()
        return ToolResult(ok=True, text="✅ Google OAuth token 已撤销并删除")
    except FileNotFoundError:
        return ToolResult(ok=True, text="ℹ️ token 文件已不存在")
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 删除 token 文件失败: {redact_text(str(exc))}")


def build_google_service(settings: Settings, api_name: str, api_version: str):
    """Build a Google API service client.

    Returns (service, error_string). If error_string is not None, service is None.
    """
    err = _import_deps()
    if err:
        return None, err

    creds = load_credentials(settings)
    if creds is None:
        return None, "Google OAuth 未授权，请先运行 /auth_google"

    try:
        service = _googleapiclient.discovery.build(api_name, api_version, credentials=creds)
        return service, None
    except Exception as exc:
        return None, f"构建 Google API 客户端失败: {redact_text(str(exc))}"


# ---------------------------------------------------------------------------
# Adapters for personal_tools/registry.py
# ---------------------------------------------------------------------------

async def google_status_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return google_status(settings)


async def google_auth_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    """Start auth flow, or complete with code if arg provided."""
    code = arg.strip()
    if code:
        return start_auth_flow_with_code(settings, code)
    return start_auth_flow(settings)


async def google_revoke_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return revoke_credentials(settings)
