"""G2 typed browser policy and approval isolation tests (no live networking)."""
import json
import os
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers.chat_tools import build_tool_schemas
from personal_tools.browser_interact import (
    approval_required, _approved_paths, _query_paths, approve_query_path
)
from personal_tools.browser_read import _connect, _operator_key
from workflow_browser_external_identity import ExternalPolicyDenied
from workflow_browser_g2 import enabled, parse_query_url, validate_actions

URL = "https://public.example.org/approved"
FLAGS = {"CONVEYOR_REAL_BROWSER": "1",
         "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
         "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1"}


class G2PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(
            codex_memory_root=Path(self.tmp.name),
            approval_inbox_enabled=True, mcp_enabled=False, routines_enabled=False,
            skills_enabled=False, long_term_memory_enabled=False,
            chat_tools_network_allow=())
        self.arg = json.dumps({"url":URL,
            "actions":[{"type":"scroll","pixels":100}]})

    def test_three_flags_are_required_for_tool_exposure(self):
        for missing in FLAGS:
            flags = dict(FLAGS)
            flags.pop(missing)
            with patch.dict(os.environ, flags, clear=True):
                self.assertFalse(enabled())
                self.assertNotIn("browser__browse_site",
                    [s["function"]["name"] for s in build_tool_schemas(self.settings)])
        with patch.dict(os.environ, FLAGS, clear=True):
            self.assertTrue(enabled())
            self.assertIn("browser__browse_site",
                [s["function"]["name"] for s in build_tool_schemas(self.settings)])

    def test_schema_rejects_click_selector_js_and_unbounded_plan(self):
        unsafe = ({"type":"click","selector":"#purchase"},
                  {"type":"follow_link","selector":"a"},
                  {"type":"follow_link","text":"Next","url":"https://evil.test/"},
                  {"type":"search_get","query":"password=a&token=b"},
                  {"type":"expand","index":True},
                  {"type":"scroll","pixels":20000})
        for action in unsafe:
            with self.subTest(action=action), self.assertRaises(ExternalPolicyDenied):
                validate_actions([action])
        with self.assertRaises(ExternalPolicyDenied):
            validate_actions([{"type":"paginate"}]*9)
        self.assertEqual(len(validate_actions([{"type":"paginate"}])),1)
        self.assertEqual(parse_query_url(URL+"?q=public+records"),
                         (URL,"q","public records"))
        for bad in (URL+"?q=hi&x=1", URL+"?q=a%0ab",
                    URL+"?q=a%2Fb", URL+"?q=hi#fragment"):
            with self.subTest(bad=bad), self.assertRaises(ExternalPolicyDenied):
                parse_query_url(bad)

    def test_query_requires_distinct_operator_explicit_grant(self):
        with patch.dict(os.environ, FLAGS, clear=True):
            self.assertTrue(approval_required(self.settings,self.arg,"operator"))
            with self.assertRaises(ExternalPolicyDenied):
                approve_query_path(self.settings,operator_id="operator",url=URL,
                    field="q",expires_at=time.time()+300)
            with closing(_connect(self.settings)) as conn:
                conn.execute("INSERT INTO browser_agent_grants VALUES(?,?,?,?)",
                    (_operator_key("operator"),URL,time.time()+300,time.time()))
                conn.commit()
            self.assertFalse(approval_required(self.settings,self.arg,"operator"))
            self.assertTrue(approval_required(self.settings,self.arg,"other"))
            self.assertEqual(_approved_paths(self.settings,"operator",URL),
                             ("/approved",))
            approve_query_path(self.settings,operator_id="operator",url=URL,
                field="q",expires_at=time.time()+300)
            self.assertEqual(_query_paths(self.settings,"operator",
                "https://public.example.org"),frozenset({(URL,"q")}))
            self.assertEqual(_query_paths(self.settings,"other",
                "https://public.example.org"),frozenset())


if __name__ == "__main__":
    unittest.main()
