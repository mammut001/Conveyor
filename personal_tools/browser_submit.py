"""The only model-visible G3 entry point is PREPARE, never SUBMIT."""
from __future__ import annotations
import asyncio
import json
import secrets
import sqlite3
import time

from handlers.tools.confirm import create_pending
from personal_tools.base import ToolResult
from personal_tools.browser_read import (
    _db, _operator_key, _requested_url, approval_required as path_approval_required,
)
from workflow_browser_external_approvals import ApprovalLedger
from workflow_browser_egress import EgressAudit
from workflow_browser_external_identity import ExternalPolicyDenied, canonical_url
from workflow_browser_g3 import G3Browser, enabled, validate_request
from workflow_browser_g3_store import save, claim, review, advance, digest
from workflow_browser_g3_transport import send_once

TOOL_NAME="browser.prepare_submit"
CONFIRMED_NAME="browser.g3_dispatch"  # NEVER register or expose to models

def _ensure():
    if not enabled():
        raise ExternalPolicyDenied("g3_disabled")

def _path_live(settings,operator_id,url):
    if path_approval_required(settings,url,operator_id):
        raise ExternalPolicyDenied("g3_path_approval_missing")

def _context(settings,operator_id,source,target=None):
    _ensure()
    _path_live(settings,operator_id,source)
    if target:
        _path_live(settings,operator_id,target)
    host,port,path,origin=_requested_url(source)
    paths=[path]
    if target:
        h,p,t,o=_requested_url(target)
        if o!=origin:
            raise ExternalPolicyDenied("g3_origin_denied")
        paths.append(t)
    ledger=ApprovalLedger(_db(settings))
    task="g3_"+secrets.token_hex(16)
    ledger.approve(task=task,host=host,port=port,
                   expires_at=time.time()+120,
                   approver="operator_"+_operator_key(operator_id)[:24],
                   safe_paths=tuple(dict.fromkeys(paths)),methods=("GET",))
    browser=G3Browser(task=task,ledger=ledger,audit=EgressAudit(ledger.path),
                      grant_check=lambda: (_path_live(settings,operator_id,source),
                                           _path_live(settings,operator_id,target) if target else None))
    return browser,ledger,task

async def prepare(settings,arg,*,operator_id,channel="web",chat_id="",**_kwargs):
    try:
        _ensure()
        if not operator_id or not channel or not chat_id:
            raise ExternalPolicyDenied("g3_operator_context_missing")
        url,index,fields=validate_request(arg)
        b,l,t=_context(settings,operator_id,url)
        try:
            async with b:
                preview,screen=await b.preview(url,index,fields)
        finally:
            l.revoke(t)
        # Target requires its own prior, unexpired, exact-path G1 grant.
        _path_live(settings,operator_id,preview["target"])
        token,hashval=save(settings,operator_id=operator_id,url=url,
                           form_ref=index,fields=fields,preview=preview,screenshot=screen)
        if channel == "telegram":
            from browser_g3_telegram import send_preview
            if not await asyncio.to_thread(send_preview, settings, chat_id,
                    json.dumps({"token":token,"hash":hashval})):
                advance(settings,token,"pending","denied")
                raise ExternalPolicyDenied("g3_telegram_preview_unavailable")
        pending=create_pending(CONFIRMED_NAME,
                 json.dumps({"token":token,"hash":hashval},sort_keys=True),
                 operator_id=operator_id,chat_id=chat_id,channel=channel)
        # The durable token, not the page, determines executable state.
        try:
            import approval_relay
            visible=(f"G3 FORM SUBMIT — HUMAN REVIEW REQUIRED; "
                     f"{preview['method']} {preview['target']}; "
                     f"fields={json.dumps(preview['fields'],ensure_ascii=True)}; "
                     f"preview_sha256={hashval}; expires in 5 minutes; "
                     "view screenshot in authenticated Approval Inbox")
            published=approval_relay.publish(settings,pending,summary=visible,
                                             danger="write",source="chat")
            if getattr(settings,"approval_relay_enabled",False) and not published:
                from handlers.tools.confirm import pop_pending
                pop_pending(pending.token)
                advance(settings,token,"pending","denied")
                raise ExternalPolicyDenied("g3_relay_unavailable")
        except ExternalPolicyDenied:
            raise
        except Exception as exc:
            raise ExternalPolicyDenied("g3_relay_unavailable") from exc
        # Local web console inbox works with relay off. Telegram requires a
        # working relay for outbound approval notice and remote decisions.
        if channel!="web" and not getattr(settings,"approval_relay_enabled",False):
            from handlers.tools.confirm import pop_pending
            pop_pending(pending.token)
            advance(settings,token,"pending","denied")
            raise ExternalPolicyDenied("g3_relay_required")
        return ToolResult(True, f"awaiting human G3 preview approval: {hashval}; "
                          f"expires in 5 minutes; no submit dispatched")
    except ExternalPolicyDenied as exc:
        return ToolResult(False, f"browser denied: {exc}")
    except Exception:
        return ToolResult(False, "browser denied: g3_prepare_unavailable")

async def approve_and_submit(settings,arg,*,operator_id):
    """Only handlers.tools.runner.execute_confirmed calls this from human approval."""
    try:
        _ensure()
        payload=json.loads(arg)
        if not isinstance(payload,dict) or set(payload)!={"token","hash"}:
            raise ExternalPolicyDenied("g3_confirmation_denied")
        token=payload["token"]; expected_hash=payload["hash"]
        if not isinstance(expected_hash,str) or len(expected_hash)!=64:
            raise ExternalPolicyDenied("g3_confirmation_denied")
        row=review(settings,token)
        if row is None or row["hash"]!=expected_hash:
            raise ExternalPolicyDenied("g3_approval_expired_or_used")
        url,index,fields,expected=claim(settings,token,operator_id,expected_hash)
        dispatched=False
        try:
            target=expected["target"]
            b,l,t=_context(settings,operator_id,url,target)
            try:
                async with b:
                    fresh,_=await b.preview(url,index,fields)
                    if digest(fresh)!=expected_hash:
                        raise ExternalPolicyDenied("g3_preview_mismatch")
                    # Ensure original grants and kill switch still live.
                    b._ensure_live()
                    _path_live(settings,operator_id,target)
                    _,_,path,_=_requested_url(target)
                    # Durable write is committed before network can begin.
                    advance(settings,token,"verifying","dispatched")
                    dispatched=True
                    b._audit(expected["method"],"allowed","g3_human_approved_dispatched")
                    status=await asyncio.to_thread(send_once,b.site.host,b.site.port,
                        b._pinned_ip,path,expected["method"],expected["fields"])
            finally:
                l.revoke(t)
        except BaseException:
            advance(settings,token,"dispatched" if dispatched else "verifying",
                    "needs_reconciliation" if dispatched else "denied")
            raise
        advance(settings,token,"dispatched","done")
        return f"G3 submit completed (HTTP {status}); one request only; preview {expected_hash}"
    except ExternalPolicyDenied as exc:
        return f"browser denied: {exc}"
    except Exception:
        from workflow_browser_g3_store import state
        try:
            current = state(settings, token)
        except Exception:
            current = "unavailable"
        # Never claim an uncertain remote commit if nothing was dispatched.
        if current in ("dispatched", "needs_reconciliation"):
            return "browser submit uncertain: needs_reconciliation; do not retry"
        return "browser denied: g3_verification_unavailable"
