"""Always-on chat window for the VPS desktop.

Posts one sentence to the local web console, which already routes a
desktop request onto computer use. The window stays on the XFCE session
so the machine that stays powered on has a chat box of its own.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import Any


def enable_system_gtk() -> None:
    """Let the app venv import Ubuntu's PyGObject.

    python3-gi lives in dist-packages, which a normal venv does not see.
    Append that directory so packages already installed in the venv stay first.
    """
    extra = "/usr/lib/python3/dist-packages"
    if os.path.isdir(extra) and extra not in sys.path:
        sys.path.append(extra)


def web_chat_url(settings: Any) -> str:
    host = str(getattr(settings, "conveyor_web_host", "") or "127.0.0.1")
    try:
        port = int(getattr(settings, "conveyor_web_port", 8787) or 8787)
    except (TypeError, ValueError):
        port = 8787
    return f"http://{host}:{port}/api/chat"


def read_chat_events(raw: str) -> tuple[str, str]:
    """Return (outcome, assistant text) from an SSE body."""
    texts: list[str] = []
    outcome = ""
    error = ""
    for frame in (raw or "").split("\n\n"):
        event = ""
        data = ""
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if not event or not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if event == "message":
            text = str(payload.get("text") or "").strip()
            if text:
                texts.append(text)
        elif event == "done":
            outcome = str(payload.get("outcome") or "")
        elif event == "error":
            error = str(payload.get("error") or "")
    if error and not texts:
        texts.append(error)
    return outcome, "\n".join(texts).strip()


def post_chat(settings: Any, text: str, *, timeout: float) -> tuple[int, str]:
    token = str(getattr(settings, "conveyor_web_token", "") or "")
    if not token:
        return 0, ""
    body = json.dumps({"message": text}).encode("utf-8")
    request = urllib.request.Request(
        web_chat_url(settings),
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, raw


def _append(buffer: Any, line: str) -> None:
    end = buffer.get_end_iter()
    buffer.insert(end, line.rstrip() + "\n")


def main() -> None:
    enable_system_gtk()
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    from config import load_runtime_settings
    from dotenv import load_dotenv

    load_dotenv("/etc/default/conveyor", override=False)
    load_dotenv("/opt/conveyor/.env", override=True)
    env_file = os.environ.get("CONVEYOR_ENV_FILE", "/opt/conveyor/.env")
    settings = load_runtime_settings(env_file)
    try:
        budget = float(getattr(settings, "conveyor_computer_max_seconds", 600) or 600) + 60.0
    except (TypeError, ValueError):
        budget = 660.0

    window = Gtk.Window(title="Conveyor")
    window.set_default_size(440, 640)
    window.set_position(Gtk.WindowPosition.CENTER)

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    root.set_margin_top(12)
    root.set_margin_bottom(12)
    root.set_margin_start(12)
    root.set_margin_end(12)
    window.add(root)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(8)
    view.set_right_margin(8)
    view.set_top_margin(8)
    view.set_bottom_margin(8)
    scroll.add(view)
    root.pack_start(scroll, True, True, 0)

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    entry = Gtk.Entry()
    entry.set_placeholder_text("说一句，让这台电脑去做")
    entry.set_hexpand(True)
    send = Gtk.Button(label="发送")
    row.pack_start(entry, True, True, 0)
    row.pack_start(send, False, False, 0)
    root.pack_start(row, False, False, 0)

    status = Gtk.Label(label="", xalign=0)
    root.pack_start(status, False, False, 0)
    buffer = view.get_buffer()

    if not str(getattr(settings, "conveyor_web_token", "") or ""):
        _append(buffer, "网页令牌没有配置，这句话发不出去。")

    def finish(outcome: str, reply: str) -> bool:
        send.set_sensitive(True)
        entry.set_sensitive(True)
        status.set_text("")
        if reply:
            _append(buffer, reply)
        elif outcome:
            _append(buffer, outcome)
        else:
            _append(buffer, "没有收到回复。")
        return False

    def fail(note: str) -> bool:
        send.set_sensitive(True)
        entry.set_sensitive(True)
        status.set_text("")
        _append(buffer, note)
        return False

    def worker(text: str) -> None:
        try:
            code, raw = post_chat(settings, text, timeout=budget)
        except Exception:
            GLib.idle_add(fail, "发送失败。")
            return
        if code == 0:
            GLib.idle_add(fail, "网页令牌没有配置，这句话发不出去。")
            return
        if code != 200:
            GLib.idle_add(fail, f"网页返回 {code}。")
            return
        outcome, reply = read_chat_events(raw)
        GLib.idle_add(finish, outcome, reply)

    def on_send(_widget: Any = None) -> None:
        text = entry.get_text().strip()
        if not text or not send.get_sensitive():
            return
        entry.set_text("")
        send.set_sensitive(False)
        entry.set_sensitive(False)
        status.set_text("正在发送…")
        _append(buffer, f"你: {text}")
        threading.Thread(target=worker, args=(text,), daemon=True).start()

    send.connect("clicked", on_send)
    entry.connect("activate", on_send)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
