from __future__ import annotations

import logging
import os
import pty
import re
import secrets
import select
import shlex
import subprocess
import threading
import time
from typing import Any

import workers

logger = logging.getLogger("conveyor.workers.login")

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
CODE_PROMPT_PUNCT_RE = re.compile(r"[:?：？]\s*$")

SESSION_TIMEOUT_SECONDS = 600.0  # 10 minutes


class LoginUnsupportedError(Exception):
    pass


class SessionNotFoundError(Exception):
    pass


class BadCodeError(Exception):
    pass


class SessionInactiveError(Exception):
    pass


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?'\")>]")
    return url or None


def detect_code_prompt(text: str) -> bool:
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        lower = trimmed.lower()
        if "code" in lower or "验证码" in lower:
            if CODE_PROMPT_PUNCT_RE.search(trimmed):
                return True
            if "paste code here" in lower or "enter code" in lower or "enter the code" in lower:
                return True
    return False


def validate_code(code: Any) -> str:
    if not isinstance(code, str):
        raise BadCodeError("code must be a string")
    if "\r" in code or "\n" in code:
        raise BadCodeError("code must be a single line")
    code_stripped = code.strip()
    if len(code_stripped) < 1 or len(code_stripped) > 200:
        raise BadCodeError("code length must be 1..200")
    if not code_stripped.isprintable():
        raise BadCodeError("code must be printable characters")
    return code_stripped


def _build_clean_env() -> dict[str, str]:
    allowed_keys = ("PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TMPDIR")
    clean_env = {k: v for k, v in os.environ.items() if k in allowed_keys}
    clean_env["TERM"] = "xterm-256color"
    return clean_env


class WorkerLoginSession:
    def __init__(
        self,
        worker_id: str,
        session_id: str,
        settings: Any,
        worker_def: dict[str, Any],
    ) -> None:
        self.worker_id = worker_id
        self.session_id = session_id
        self.settings = settings
        self.worker_def = worker_def
        self.state: str = "waiting_url"
        self.url: str | None = None
        self.message: str = "正在启动登录..."
        self.started_at: float = time.time()
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.cancelled: bool = False
        self._lock = threading.Lock()

    def is_expired(self) -> bool:
        return (time.time() - self.started_at) > SESSION_TIMEOUT_SECONDS

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session": self.session_id,
                "state": self.state,
                "url": self.url,
                "message": self.message,
            }

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self.cancelled = True
            self.state = "failed"
            self.message = "已取消"
            fd = self.master_fd
            self.master_fd = None
            proc = self.proc

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass

        return self.to_dict()


_active_sessions: dict[str, WorkerLoginSession] = {}
_session_lock = threading.Lock()


def clear_login_sessions() -> None:
    with _session_lock:
        for session in list(_active_sessions.values()):
            session.cancel()
        _active_sessions.clear()


def _reader_loop(session: WorkerLoginSession) -> None:
    accumulated = ""
    master_fd = session.master_fd
    proc = session.proc

    try:
        while True:
            if session.cancelled:
                break
            if session.is_expired():
                with session._lock:
                    session.state = "failed"
                    session.message = "登录超时"
                break

            if master_fd is None:
                break

            # Poll for data with select (short timeout)
            try:
                r, _, _ = select.select([master_fd], [], [], 0.2)
            except (OSError, ValueError):
                break

            if not r:
                if proc is not None and proc.poll() is not None:
                    break
                continue

            try:
                chunk = os.read(master_fd, 1024)
            except OSError:
                break

            if not chunk:
                break

            text = strip_ansi(chunk.decode("utf-8", errors="replace"))
            accumulated += text

            with session._lock:
                if session.url is None:
                    found_url = extract_url(accumulated)
                    if found_url:
                        session.url = found_url
                        if session.state == "waiting_url":
                            session.state = "open_url"
                            session.message = "请在浏览器中打开链接完成登录"

                if session.state in ("waiting_url", "open_url"):
                    if detect_code_prompt(accumulated):
                        session.state = "needs_code"
                        session.message = "请输入浏览器中显示的验证码"

            if proc is not None and proc.poll() is not None:
                # Read remaining buffered output if any
                try:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.05)
                        if not r:
                            break
                        chunk = os.read(master_fd, 1024)
                        if not chunk:
                            break
                        accumulated += strip_ansi(chunk.decode("utf-8", errors="replace"))
                except (OSError, ValueError):
                    pass
                break
    finally:
        with session._lock:
            fd = session.master_fd
            session.master_fd = None

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        rc = None
        if proc is not None:
            try:
                rc = proc.wait(timeout=5.0)
            except Exception:
                try:
                    proc.kill()
                    rc = proc.wait(timeout=2.0)
                except Exception:
                    pass

        # Final state transition
        with session._lock:
            if session.cancelled:
                session.state = "failed"
                session.message = "已取消"
                return
            if session.state == "failed" and session.message == "登录超时":
                return

        # Re-run auth detection with cleared cache
        try:
            workers.clear_detection_cache()
            det = workers.detect_worker(session.worker_def)
            auth_ok = det.get("auth") == "ok"
        except Exception as exc:
            logger.warning("Auth re-detection failed for %s: %s", session.worker_id, exc)
            auth_ok = False

        with session._lock:
            if session.cancelled:
                session.state = "failed"
                session.message = "已取消"
            elif rc == 0 and auth_ok:
                session.state = "done"
                session.message = "登录成功"
            else:
                session.state = "failed"
                if not auth_ok:
                    session.message = "登录未完成或验证失败"
                else:
                    session.message = "登录进程异常退出"


def start_login_session(settings: Any, worker_id: str) -> dict[str, Any]:
    worker_def = workers.get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    login_cmd_str = worker_def.get("login_command")
    if not login_cmd_str:
        raise LoginUnsupportedError("login unsupported")

    with _session_lock:
        old_session = _active_sessions.get(worker_id)
        if old_session is not None:
            old_session.cancel()
            _active_sessions.pop(worker_id, None)

        session_id = secrets.token_hex(16)
        session = WorkerLoginSession(
            worker_id=worker_id,
            session_id=session_id,
            settings=settings,
            worker_def=worker_def,
        )

        master_fd, slave_fd = pty.openpty()
        cmd = shlex.split(login_cmd_str)
        clean_env = _build_clean_env()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                env=clean_env,
            )
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        session.master_fd = master_fd
        session.proc = proc

        thread = threading.Thread(
            target=_reader_loop,
            args=(session,),
            daemon=True,
            name=f"login-{worker_id}-{session_id[:8]}",
        )
        session.thread = thread
        thread.start()

        _active_sessions[worker_id] = session
        return session.to_dict()


def get_login_session(settings: Any, worker_id: str, session_id: str) -> dict[str, Any]:
    worker_def = workers.get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    with _session_lock:
        session = _active_sessions.get(worker_id)

    if session is None or session.session_id != session_id:
        raise SessionNotFoundError("session not found")

    if session.is_expired():
        session.cancel()
        with session._lock:
            session.state = "failed"
            session.message = "登录超时"

    return session.to_dict()


def send_login_code(settings: Any, worker_id: str, session_id: str, code: str) -> dict[str, Any]:
    worker_def = workers.get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    valid_code = validate_code(code)

    with _session_lock:
        session = _active_sessions.get(worker_id)

    if session is None or session.session_id != session_id:
        raise SessionNotFoundError("session not found")

    if session.state in ("done", "failed"):
        raise SessionInactiveError("session inactive")

    with session._lock:
        fd = session.master_fd

    if fd is not None:
        try:
            os.write(fd, (valid_code + "\n").encode("utf-8"))
        except OSError as exc:
            logger.warning("Failed writing code to pty: %s", exc)

    return session.to_dict()


def cancel_login_session(settings: Any, worker_id: str, session_id: str) -> dict[str, Any]:
    worker_def = workers.get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    with _session_lock:
        session = _active_sessions.get(worker_id)

    if session is None or session.session_id != session_id:
        raise SessionNotFoundError("session not found")

    return session.cancel()
