import io
import os
import unittest
from contextlib import redirect_stderr
from http import HTTPStatus
from unittest import mock

# The server exits at import when the desktop node is enabled without a token
# (CI's all-feature-flags job); give it one for the import.
with mock.patch.dict(os.environ, {"CONVEYOR_DESKTOP_AGENT_TOKEN": "test-token"}):
    from conveyor.entrypoints.desktop_agent_server import DesktopAgentHTTPHandler


def _logged(command: str, path: str, code) -> str:
    h = DesktopAgentHTTPHandler.__new__(DesktopAgentHTTPHandler)
    h.command = command
    h.path = path
    h.requestline = f"{command} {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.client_address = ("127.0.0.1", 5555)
    buf = io.StringIO()
    with redirect_stderr(buf):
        h.log_request(code)
    return buf.getvalue()


class DesktopAccessLogTests(unittest.TestCase):
    def test_successful_polls_are_silent(self):
        self.assertEqual(_logged("GET", "/desktop/computer/task/pending?node_id=vps-desktop", HTTPStatus.OK), "")
        self.assertEqual(_logged("POST", "/desktop/heartbeat", 200), "")

    def test_failures_and_other_requests_still_logged(self):
        for command, path, code in [
            ("GET", "/desktop/computer/task/pending?node_id=x", HTTPStatus.UNAUTHORIZED),
            ("POST", "/desktop/heartbeat", 500),
            ("POST", "/desktop/register", 200),
            ("GET", "/desktop/computer/task/pending/extra", 200),
        ]:
            self.assertIn(path, _logged(command, path, code), (command, path, code))


if __name__ == "__main__":
    unittest.main()
