import logging
import unittest
from http import HTTPStatus

from conveyor.entrypoints.web_console import WebConsoleHandler


def _handler(command: str, path: str) -> WebConsoleHandler:
    h = WebConsoleHandler.__new__(WebConsoleHandler)
    h.command = command
    h.path = path
    h.requestline = f"{command} {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.client_address = ("127.0.0.1", 5555)
    return h


class AccessLogTests(unittest.TestCase):
    def _info_lines(self, command, path, code):
        with self.assertLogs("conveyor.web", level="DEBUG") as cm:
            logging.getLogger("conveyor.web").debug("marker")
            _handler(command, path).log_request(code, 10)
        return [r for r in cm.records if r.levelno >= logging.INFO]

    def test_successful_api_poll_is_not_info(self):
        for code in (HTTPStatus.OK, 200, 304):
            self.assertEqual(self._info_lines("GET", "/api/jobs?limit=20", code), [])

    def test_successful_api_poll_still_at_debug(self):
        with self.assertLogs("conveyor.web", level="DEBUG") as cm:
            _handler("GET", "/api/sessions").log_request(200, 10)
        self.assertTrue(any("/api/sessions" in r.getMessage() for r in cm.records))

    def test_errors_writes_and_pages_stay_info(self):
        cases = [
            ("GET", "/api/jobs", HTTPStatus.INTERNAL_SERVER_ERROR),
            ("GET", "/api/jobs", 401),
            ("POST", "/api/reminders", 200),
            ("DELETE", "/api/reminders/1", 200),
            ("GET", "/", 200),
        ]
        for command, path, code in cases:
            lines = self._info_lines(command, path, code)
            self.assertEqual(len(lines), 1, (command, path, code))
            self.assertIn(path, lines[0].getMessage())


if __name__ == "__main__":
    unittest.main()
