from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from personal_tools.web_search import _search_tinyfish, search_web


class TinyFishSearchTests(unittest.TestCase):
    def _settings(self, **overrides):
        values = {
            "web_search_api_key": "tinyfish-test-key",
            "web_search_endpoint": None,
            "web_search_backend": "tinyfish",
            "web_search_max_results": 8,
            "web_user_agent": "ConveyorTest/1.0",
            "web_fetch_timeout_seconds": 10,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @patch("personal_tools.web_search.validate_url", return_value=(True, ""))
    @patch("personal_tools.web_search._fetch_json")
    def test_tinyfish_normalizes_structured_results(self, fetch_json, _validate):
        fetch_json.return_value = (
            200,
            {
                "results": [
                    {
                        "position": 2,
                        "site_name": "example.com",
                        "title": "Fresh result",
                        "snippet": "Current information",
                        "url": "https://example.com/fresh",
                    }
                ],
                "total_results": 1,
                "page": 0,
            },
            "",
        )

        results, err = _search_tinyfish(self._settings(), "fresh query", 5)

        self.assertEqual(err, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].rank, 2)
        self.assertEqual(results[0].source, "tinyfish")
        self.assertEqual(results[0].url, "https://example.com/fresh")

        called_url = fetch_json.call_args.args[0]
        called_headers = fetch_json.call_args.kwargs["headers"]
        self.assertIn("query=fresh+query", called_url)
        self.assertEqual(called_headers["X-API-Key"], "tinyfish-test-key")

    @patch("personal_tools.web_search.validate_url", return_value=(True, ""))
    @patch("personal_tools.web_search._fetch_json")
    def test_search_web_dispatches_to_tinyfish(self, fetch_json, _validate):
        fetch_json.return_value = (200, {"results": []}, "")
        results, err = search_web(self._settings(), "anything", 3)
        self.assertEqual(results, [])
        self.assertEqual(err, "")

    def test_tinyfish_requires_key(self):
        results, err = _search_tinyfish(
            self._settings(web_search_api_key=None),
            "query",
            3,
        )
        self.assertEqual(results, [])
        self.assertIn("API key", err)


if __name__ == "__main__":
    unittest.main()
