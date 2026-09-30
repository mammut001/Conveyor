from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from logging_setup import configure_logging, install_redacting_excepthook
from redaction import SecretRedactingFilter

TOKEN_RE_1 = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
TOKEN_RE_2 = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


class StringListHandler(logging.Handler):
    """A logging handler that stores formatted messages as strings."""

    def __init__(self) -> None:
        super().__init__()
        self.formatted_records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.formatted_records.append(self.format(record))
        except Exception:
            self.handleError(record)


class TestLogRedaction(unittest.TestCase):
    def setUp(self) -> None:
        self.orig_sys_excepthook = sys.excepthook
        self.orig_threading_excepthook = threading.excepthook
        self.root_logger = logging.getLogger()
        self.orig_handlers = list(self.root_logger.handlers)
        self.orig_filters = list(self.root_logger.filters)
        self.orig_level = self.root_logger.level
        self.orig_noisy_levels = {
            name: logging.getLogger(name).level
            for name in ("httpx", "httpcore", "urllib3")
        }

        # Clear root handlers and filters for test isolation
        self.root_logger.handlers = []
        self.root_logger.filters = []

        self.handler = StringListHandler()
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s\n%(exc_text)s"))
        self.root_logger.addHandler(self.handler)

        configure_logging(level=logging.INFO)

        self.logger = logging.getLogger("test_redaction_logger")

    def tearDown(self) -> None:
        sys.excepthook = self.orig_sys_excepthook
        threading.excepthook = self.orig_threading_excepthook
        self.root_logger.handlers = self.orig_handlers
        self.root_logger.filters = self.orig_filters
        self.root_logger.setLevel(self.orig_level)
        for name, lvl in self.orig_noisy_levels.items():
            logging.getLogger(name).setLevel(lvl)

    def assertNoTokens(self, text: str) -> None:
        self.assertIsNone(TOKEN_RE_1.search(text), f"Found token matching {TOKEN_RE_1.pattern} in: {text}")
        self.assertIsNone(TOKEN_RE_2.search(text), f"Found token matching {TOKEN_RE_2.pattern} in: {text}")

    def test_httpx_url_arg(self) -> None:
        try:
            import httpx
        except ImportError:
            self.skipTest("httpx is not installed")

        fake_token = "123456789:AAHfakefakefakefakefakefakefakefake1"
        url = httpx.URL(f"https://api.telegram.org/bot{fake_token}/getMe")
        self.logger.info("Requesting Telegram URL: %s", url)

        output = "\n".join(self.handler.formatted_records)
        self.assertNotIn(fake_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertNoTokens(output)

    def test_bare_token_in_message(self) -> None:
        fake_bare_token = "987654321:BBHfakefakefakefakefakefakefakefake2"
        self.logger.info("Direct bare token %s in log message", fake_bare_token)
        self.logger.info("Inline bare token 987654321:BBHfakefakefakefakefakefakefakefake2 present")

        output = "\n".join(self.handler.formatted_records)
        self.assertNotIn(fake_bare_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertNoTokens(output)

    def test_dict_args_format(self) -> None:
        fake_token = "555666777:CCHfakefakefakefakefakefakefakefake3"
        self.logger.info(
            "Payload info: user=%(user)s token=%(token)s action=%(action)s",
            {"user": "alice", "token": fake_token, "action": "poll"},
        )

        output = "\n".join(self.handler.formatted_records)
        self.assertNotIn(fake_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertIn("user=alice", output)
        self.assertNoTokens(output)

    def test_logger_exception_with_token(self) -> None:
        fake_token = "444333222:DDHfakefakefakefakefakefakefakefake4"
        try:
            raise ValueError(f"Connection failed for https://api.telegram.org/bot{fake_token}/sendMessage")
        except ValueError:
            self.logger.exception("Polling error encountered")

        output = "\n".join(self.handler.formatted_records)
        self.assertNotIn(fake_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertNoTokens(output)

    def test_excepthook_output(self) -> None:
        fake_token = "888777666:EEHfakefakefakefakefakefakefakefake5"
        stderr_capture = io.StringIO()

        with patch("sys.stderr", stderr_capture):
            try:
                raise RuntimeError(f"Uncaught crash with token: {fake_token}")
            except RuntimeError:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                sys.excepthook(exc_type, exc_value, exc_traceback)

        output = stderr_capture.getvalue()
        self.assertNotIn(fake_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertNoTokens(output)

    def test_threading_excepthook_output(self) -> None:
        fake_token = "777888999:FFHfakefakefakefakefakefakefakefake6"
        stderr_capture = io.StringIO()

        with patch("sys.stderr", stderr_capture):
            try:
                raise RuntimeError(f"Thread crash with bare token {fake_token}")
            except RuntimeError:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                args = threading.ExceptHookArgs((exc_type, exc_value, exc_traceback, threading.current_thread()))
                threading.excepthook(args)

        output = stderr_capture.getvalue()
        self.assertNotIn(fake_token, output)
        self.assertIn("[REDACTED]", output)
        self.assertNoTokens(output)

    def test_idempotency_of_configure_logging(self) -> None:
        configure_logging(level=logging.INFO)
        configure_logging(level=logging.INFO)

        root_filters = [f for f in self.root_logger.filters if isinstance(f, SecretRedactingFilter)]
        self.assertEqual(len(root_filters), 1, "Root logger must have exactly one SecretRedactingFilter")

        for idx, handler in enumerate(self.root_logger.handlers):
            handler_filters = [f for f in handler.filters if isinstance(f, SecretRedactingFilter)]
            self.assertEqual(
                len(handler_filters),
                1,
                f"Handler {idx} must have exactly one SecretRedactingFilter",
            )

        for name in ("httpx", "httpcore", "urllib3"):
            lvl = logging.getLogger(name).getEffectiveLevel()
            self.assertGreaterEqual(lvl, logging.WARNING, f"Logger {name} level {lvl} should be >= WARNING")

    def test_desktop_agent_stdlib_imports(self) -> None:
        code = (
            "import sys\n"
            "import logging_setup\n"
            "for mod in ('telegram', 'httpx', 'config', 'lark_oapi'):\n"
            "    if mod in sys.modules:\n"
            "        sys.exit(f'Unexpected imported module: {mod}')\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Import check failed: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
