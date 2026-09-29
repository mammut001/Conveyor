"""setup_wizard/ui.py — small interactive terminal UI (stdlib only).

On a real terminal: arrow-key menus, hidden secret input, spinners and
colors. Anywhere else (pipes, tests, NO_COLOR): numbered menus, plain
line input, no escape codes — so the wizard is scriptable and testable.
"""
from __future__ import annotations

import getpass
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence, TextIO


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    hint: str = ""


def display_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """Left-align ``text`` to ``width`` terminal columns (CJK counts as 2)."""
    return text + " " * max(0, width - display_width(text))


def mask(secret: str | None) -> str:
    if not secret:
        return "(未设置)"
    tail = secret[-4:] if len(secret) > 8 else ""
    return f"••••{tail}"


class Abort(Exception):
    """The operator cancelled (Ctrl-C / Ctrl-D / Esc)."""


class UI:
    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        *,
        interactive: bool | None = None,
    ) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        if interactive is None:
            interactive = bool(
                hasattr(self.stdin, "isatty") and self.stdin.isatty()
                and hasattr(self.stdout, "isatty") and self.stdout.isatty()
            )
        self.interactive = interactive
        self.color = interactive and not os.environ.get("NO_COLOR")

    # ---- output ----------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._c("1", text)

    def dim(self, text: str) -> str:
        return self._c("2", text)

    def print(self, text: str = "") -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def title(self, text: str) -> None:
        bar = "─" * max(8, min(60, display_width(text) + 4))
        self.print()
        self.print(self._c("1;36", f"┌{bar}"))
        self.print(self._c("1;36", "│ ") + self.bold(text))
        self.print(self._c("1;36", f"└{bar}"))

    def section(self, text: str) -> None:
        self.print()
        self.print(self._c("1;34", f"▸ {text}"))

    def info(self, text: str) -> None:
        self.print(f"  {text}")

    def hint(self, text: str) -> None:
        for line in text.splitlines():
            self.print("  " + self.dim(line))

    def ok(self, text: str) -> None:
        self.print(f"  {self._c('32', '✔')} {text}")

    def warn(self, text: str) -> None:
        self.print(f"  {self._c('33', '!')} {text}")

    def fail(self, text: str) -> None:
        self.print(f"  {self._c('31', '✘')} {text}")

    # ---- input -----------------------------------------------------------

    def _readline(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise Abort()
        return line.rstrip("\n")

    def ask(
        self,
        label: str,
        default: str | None = None,
        *,
        required: bool = False,
        validate: Callable[[str], str | None] | None = None,
    ) -> str:
        """Text input. ``validate`` returns an error message or None."""
        suffix = f" {self.dim('[' + default + ']')}" if default else ""
        while True:
            try:
                value = self._readline(f"  {label}{suffix}: ").strip()
            except KeyboardInterrupt as exc:
                raise Abort() from exc
            value = value or (default or "")
            if not value and required:
                self.fail("这一项必填。")
                continue
            if value and validate:
                error = validate(value)
                if error:
                    self.fail(error)
                    continue
            return value

    def secret(self, label: str, existing: str | None = None, *, required: bool = True) -> str:
        """Hidden input; Enter keeps the existing value."""
        if existing:
            self.hint(f"当前：{mask(existing)}（直接回车保留）")
        while True:
            prompt = f"  {label}: "
            try:
                if self.interactive:
                    value = getpass.getpass(prompt, stream=self.stdout).strip()
                else:
                    value = self._readline(prompt).strip()
            except (KeyboardInterrupt, EOFError) as exc:
                raise Abort() from exc
            if value:
                self.hint(f"已输入 {mask(value)}")
                return value
            if existing:
                return existing
            if not required:
                return ""
            self.fail("这一项必填。")

    def confirm(self, label: str, default: bool = True) -> bool:
        choices = "Y/n" if default else "y/N"
        while True:
            try:
                value = self._readline(f"  {label} {self.dim('[' + choices + ']')}: ").strip().lower()
            except KeyboardInterrupt as exc:
                raise Abort() from exc
            if not value:
                return default
            if value in ("y", "yes", "是", "好"):
                return True
            if value in ("n", "no", "否", "不"):
                return False
            self.fail("请输入 y 或 n。")

    def select(self, title: str, options: Sequence[Option], default: int = 0) -> str:
        """Pick one option: arrow keys on a TTY, numbers elsewhere."""
        if not options:
            raise ValueError("no options")
        default = max(0, min(default, len(options) - 1))
        if self.interactive and _raw_capable(self.stdin):
            try:
                return options[self._select_raw(title, options, default)].value
            except _RawUnavailable:
                pass
        self.print(f"  {self.bold(title)}")
        for i, opt in enumerate(options, 1):
            mark = "›" if i - 1 == default else " "
            hint = f"  {self.dim(opt.hint)}" if opt.hint else ""
            self.print(f"  {mark} {i}. {opt.label}{hint}")
        while True:
            raw = self.ask("输入编号", str(default + 1))
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1].value
            matches = [o for o in options if o.value == raw]
            if matches:
                return matches[0].value
            self.fail(f"请输入 1-{len(options)}。")

    def _select_raw(self, title: str, options: Sequence[Option], index: int) -> int:
        import termios
        import tty

        fd = self.stdin.fileno()
        try:
            saved = termios.tcgetattr(fd)
        except termios.error as exc:
            raise _RawUnavailable() from exc
        out = self.stdout
        out.write(f"  {self.bold(title)}  {self.dim('↑↓ 选择 · 回车确认 · 数字直选')}\n")

        def draw(first: bool) -> None:
            if not first:
                out.write(f"\033[{len(options)}A")
            for i, opt in enumerate(options):
                hint = f"  {self.dim(opt.hint)}" if opt.hint else ""
                if i == index:
                    line = self._c("1;36", f"  ❯ {opt.label}") + hint
                else:
                    line = f"    {opt.label}{hint}"
                out.write("\033[2K" + line + "\n")
            out.flush()

        draw(True)
        try:
            tty.setcbreak(fd)
            out.write("\033[?25l")
            while True:
                ch = os.read(fd, 1)
                if ch in (b"\r", b"\n"):
                    break
                if ch == b"\x03":
                    raise Abort()
                if ch == b"\x1b":
                    import select as _select

                    # A lone Esc cancels; arrow keys arrive as Esc [ A/B.
                    if not _select.select([fd], [], [], 0.05)[0]:
                        raise Abort()
                    seq = os.read(fd, 2)
                    if seq in (b"[A", b"OA"):
                        index = (index - 1) % len(options)
                    elif seq in (b"[B", b"OB"):
                        index = (index + 1) % len(options)
                elif ch in (b"k",):
                    index = (index - 1) % len(options)
                elif ch in (b"j",):
                    index = (index + 1) % len(options)
                elif ch.isdigit() and 1 <= int(ch) <= len(options):
                    index = int(ch) - 1
                    draw(False)
                    break
                draw(False)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            out.write("\033[?25h")
            out.flush()
        return index

    # ---- progress --------------------------------------------------------

    @contextmanager
    def spinner(self, text: str) -> Iterator["_Step"]:
        """Run a check with a spinner; call ``step.done(ok, detail)``."""
        step = _Step()
        stop = threading.Event()
        thread = None
        if self.interactive:
            def spin() -> None:
                frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                i = 0
                while not stop.is_set():
                    self.stdout.write(f"\r  {self._c('36', frames[i % len(frames)])} {text}…")
                    self.stdout.flush()
                    i += 1
                    time.sleep(0.08)
            thread = threading.Thread(target=spin, daemon=True)
            thread.start()
        else:
            self.print(f"  … {text}")
        try:
            yield step
        finally:
            stop.set()
            if thread is not None:
                thread.join()
                self.stdout.write("\r\033[2K")
            detail = f" — {step.detail}" if step.detail else ""
            if step.ok is None:
                self.warn(f"{text}（中断）")
            elif step.ok:
                self.ok(f"{text}{detail}")
            else:
                self.fail(f"{text}{detail}")


class _Step:
    def __init__(self) -> None:
        self.ok: bool | None = None
        self.detail = ""

    def done(self, ok: bool, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail


class _RawUnavailable(Exception):
    pass


def _raw_capable(stream: TextIO) -> bool:
    if os.name != "posix":
        return False
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True
