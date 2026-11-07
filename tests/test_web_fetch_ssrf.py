"""web.fetch must not reach private addresses: IPv4 inside IPv6, and DNS rebinding."""
from __future__ import annotations

import dataclasses
import socket
import unittest
from unittest.mock import patch

from personal_tools import web_fetch
from personal_tools.web_fetch import validate_and_pin, validate_url

_DNS = {"example.com": "93.184.215.14", "v6.example": "2606:2800:21f:cb07:6820:80da:af6b:8b2c"}


def _fake_getaddrinfo(host, *_a, **_k):
    ip = _DNS.get(host, host)
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, 0))]


class WebFetchSsrfTest(unittest.TestCase):
    def test_ipv4_carried_in_ipv6_is_refused(self) -> None:
        with patch("socket.getaddrinfo", _fake_getaddrinfo):
            for url in ("http://[::ffff:127.0.0.1]:8787/", "http://[::ffff:169.254.169.254]/",
                        "http://[::ffff:a9fe:a9fe]/", "http://[::127.0.0.1]/",
                        "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[::1]/"):
                with self.subTest(url=url):
                    self.assertFalse(validate_url(url)[0])
            self.assertTrue(validate_url("https://example.com/")[0])
            self.assertTrue(validate_url("https://v6.example/")[0])

    def test_name_is_pinned_to_the_checked_address_for_every_request(self) -> None:
        with patch("socket.getaddrinfo", _fake_getaddrinfo):
            ok, _err, pin = validate_and_pin("https://example.com/page")
            self.assertTrue(ok)
            self.assertEqual(pin, ["--resolve", "example.com:443:93.184.215.14"])
            self.assertEqual(validate_and_pin("http://v6.example:8080/")[2],
                             ["--resolve", "v6.example:8080:[2606:2800:21f:cb07:6820:80da:af6b:8b2c]"])

            from config import load_settings
            settings = dataclasses.replace(load_settings(".env.test.example"), web_fetch_enabled=True)
            calls = []

            def fake_run(argv):
                calls.append(argv)
                return 0, "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nhi", ""

            with patch.object(web_fetch, "_run_curl", side_effect=fake_run):
                web_fetch.fetch_text(settings, "https://example.com/page")
        self.assertEqual(len(calls), 2)  # HEAD and GET
        for argv in calls:
            self.assertIn("--resolve", argv)
            self.assertEqual(argv[argv.index("--resolve") + 1], "example.com:443:93.184.215.14")


if __name__ == "__main__":
    unittest.main()
