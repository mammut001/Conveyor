"""Authenticated browser admin API and G2 confirmation-inbox integration."""
from __future__ import annotations
import asyncio
import http.client
import json
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from channel.types import InboundMessage
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.runner import _request_confirmation
from personal_tools.browser_read import _connect, _db, _operator_key
from personal_tools.browser_approval_scope import status
from web_chat import decide_tool_approval
from web_console import WebConsoleHandler, WebConsoleServer
from workflow_browser_egress import EgressAudit
from workflow_browser_external import UntrustedPageText

TOKEN = "test-browser-control-auth-abcdefghijklmnop"
URL = "https://approved.example.org/start"
SEARCH = "https://approved.example.org/search"
FLAGS = {"CONVEYOR_REAL_BROWSER":"1",
         "CONVEYOR_BROWSER_EXTERNAL_READONLY":"1",
         "CONVEYOR_BROWSER_EXTERNAL_INTERACT":"1"}


class FakePort:
    supports_inline_buttons = False
    supports_attachments = False
    def __init__(self): self.messages = []
    async def reply(self, _msg, body):
        self.messages.append(body); return None
    async def send_new(self, _msg, body):
        self.messages.append(body); return None


class FakeBrowser:
    def __init__(self, *, ledger, task, query_paths, **_kw):
        self.site = ledger.require(task)
        self.query_paths = query_paths
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def browse(self, url, actions):
        assert url == URL
        assert (SEARCH, "q") in self.query_paths
        assert {"type":"search_get","path":"/search","field":"q",
                "query":"public"} in actions
        return UntrustedPageText(self.site.origin, "Untrusted results")


class FakeControl:
    def __init__(self, settings): self.settings = settings
    def list_approvals(self): return []


class BrowserManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.settings = SimpleNamespace(
            codex_memory_root=Path(cls.temp.name)/"memory",
            approval_inbox_enabled=True, approval_relay_enabled=False,
            routines_enabled=False, mcp_enabled=False, skills_enabled=False,
            long_term_memory_enabled=False, chat_tools_network_allow=(),
        )
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever,daemon=True)
        cls.thread.start()
        cls.server = WebConsoleServer(("127.0.0.1",0),WebConsoleHandler,
            control=FakeControl(cls.settings),loop=cls.loop,token=TOKEN)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=3)
        cls.server_thread.join(timeout=3)
        cls.temp.cleanup()

    def setUp(self):
        self.flags=patch.dict(os.environ, FLAGS)
        self.flags.start()
        self.addCleanup(self.flags.stop)
        from browser_approvals_admin import set_kill
        set_kill(self.settings,False)
        with _connect(self.settings) as conn:
            conn.execute("DELETE FROM browser_agent_grants")
            conn.execute("""CREATE TABLE IF NOT EXISTS browser_g2_query_grants
                (operator_key TEXT,url TEXT,field TEXT,expires_at REAL,
                 PRIMARY KEY(operator_key,url,field))""")
            conn.execute("DELETE FROM browser_g2_query_grants")
            conn.execute("""CREATE TABLE IF NOT EXISTS browser_g2_action_grants
                (operator_key TEXT,url TEXT,action_types TEXT,expires_at REAL,
                 PRIMARY KEY(operator_key,url))""")
            conn.execute("DELETE FROM browser_g2_action_grants")
            conn.commit()

    def request(self, method, path, body=None, authorized=True):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=4)
        headers={"Content-Type":"application/json"}
        if authorized: headers["Authorization"]="Bearer "+TOKEN
        conn.request(method,path,body=json.dumps(body).encode() if body is not None else None,
                     headers=headers)
        response=conn.getresponse()
        payload=json.loads(response.read() or b"{}")
        conn.close()
        return response.status,payload

    def test_auth_listing_expiry_revoke_egress_and_kill(self):
        from personal_tools.browser_approval_scope import grant_confirmed
        arg = [{"type":"search_get","path":"/search","field":"q","query":"public"}]
        grant_confirmed(self.settings,"operator-x",URL,arg)
        unauth,_=self.request("GET","/api/browser/approvals",authorized=False)
        self.assertEqual(unauth,401)
        status_code,result=self.request("GET","/api/browser/approvals")
        self.assertEqual(status_code,200)
        rows=result["approvals"]
        self.assertTrue(any(x["kind"]=="query" and x["query_field"]=="q" and
                            x["path"]=="/search" for x in rows))
        self.assertTrue(any(x["kind"]=="actions" and
                            "search_get" in x["action_types"] for x in rows))
        self.assertNotIn("operator-x",json.dumps(rows))
        self.assertNotIn("public",json.dumps(rows))
        EgressAudit(_db(self.settings)).record("task-one","approved.example.org",
            "GET","allowed","approved_readonly")
        status_code, result = self.request("GET","/api/browser/egress?limit=5")
        self.assertEqual(status_code,200)
        self.assertIn("approved_readonly",str(result["requests"]))
        self.assertNotIn("path",json.dumps(result))
        self.assertEqual(self.request("GET","/api/browser/egress?limit=500")[0],400)
        self.assertEqual(self.request("DELETE","/api/browser/approvals/"+rows[0]["id"],
                         authorized=False)[0],401)
        query_row=next(x for x in rows if x["kind"]=="query")
        self.assertEqual(self.request("DELETE",
            "/api/browser/approvals/"+query_row["id"])[0],200)
        self.assertFalse(any(x["kind"]=="query" for x in
            self.request("GET","/api/browser/approvals")[1]["approvals"]))
        self.assertEqual(self.request("POST","/api/browser/kill",{"enabled":True},
                                      authorized=False)[0],401)
        self.assertEqual(self.request("POST","/api/browser/kill",{"enabled":"yes"})[0],400)
        self.assertTrue(self.request("POST","/api/browser/kill",{"enabled":True})[1]["kill_switch"])
        self.assertTrue(self.request("GET","/api/browser/approvals")[1]["kill_switch"])
        self.assertFalse(self.request("POST","/api/browser/kill",{"enabled":False})[1]["kill_switch"])

    def test_expired_entries_do_not_appear(self):
        from personal_tools.browser_approval_scope import grant_confirmed
        grant_confirmed(self.settings,"operator-x",URL,[])
        with _connect(self.settings) as conn:
            conn.execute("UPDATE browser_agent_grants SET expires_at=0")
            conn.commit()
        self.assertEqual(self.request("GET","/api/browser/approvals")[1]["approvals"],[])


class G2PendingConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_all_pending()
        self.addCleanup(clear_all_pending)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings=SimpleNamespace(
            codex_memory_root=Path(self.temp.name),
            approval_inbox_enabled=True,approval_relay_enabled=False,
            routines_enabled=False, long_term_memory_enabled=False,
            mcp_enabled=False,skills_enabled=False,
        )
        self.msg=InboundMessage(channel="web",operator_id="operator",chat_id="session",
            message_id="message",text="GET search",chat_type="p2p")
        self.port=FakePort()
        self.arg=json.dumps({"url":URL,"actions":[
            {"type":"search_get","path":"/search","field":"q","query":"public"}]})

    async def test_confirmation_card_shows_scope_and_persists_query(self):
        with patch.dict(os.environ,FLAGS), patch(
                "personal_tools.browser_interact.G2Browser",FakeBrowser):
            await _request_confirmation(self.msg,self.port,self.settings,
                                        "browser.browse_site",self.arg)
            pending=get_pending_for_context("operator","session","web")
            self.assertIsNotNone(pending)
            import approval_inbox
            items=approval_inbox.list_items(self.settings,FakeControl(self.settings))
            scope=next(x for x in items if x["id"]==pending.token)["summary"]
            self.assertIn("/search",scope)
            self.assertIn("field=q",scope)
            decision=await decide_tool_approval(pending,True,self.settings)
            self.assertEqual(decision["status"],"accepted")
            self.assertIn("UNTRUSTED_WEBPAGE_BEGIN",decision["result"])
            self.assertFalse(status(self.settings,"operator",URL,
                [{"type":"search_get","path":"/search","field":"q","query":"public"}])["query_missing"])
            self.assertIn("follow_link",status(self.settings,"operator",URL,
                [{"type":"follow_link","text":"Next"}])["new_actions"])
