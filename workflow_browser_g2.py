"""G2: bounded typed read-only interaction on G1-approved HTTPS pages.

No model selectors, scripts, arbitrary URLs, DOM link clicks or form submission.
JavaScript remains disabled. Every navigation uses G1's pinned GET transport.
"""
from __future__ import annotations
import os
import re
import time
from urllib.parse import urlsplit, parse_qsl, urlencode
from workflow_browser_external import ExternalReadOnlyBrowser, UntrustedPageText
from workflow_browser_external_identity import ExternalPolicyDenied, canonical_url

MAX_ACTIONS = 8
FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
QUERY_RE = re.compile(r"[A-Za-z0-9 ._-]{1,80}\Z")


def enabled():
    return all(os.getenv(k) == "1" for k in (
        "CONVEYOR_REAL_BROWSER", "CONVEYOR_BROWSER_EXTERNAL_READONLY",
        "CONVEYOR_BROWSER_EXTERNAL_INTERACT"))


def validate_actions(actions):
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ExternalPolicyDenied("g2_actions_denied")
    specs = {
        "follow_link": ({"type", "text"}, {"type", "index"}),
        "paginate": ({"type"},),
        "expand": ({"type", "index"},),
        "scroll": ({"type", "pixels"},),
        "search_get": ({"type", "query", "path", "field"},
                       {"type", "query", "index", "path", "field"}),
    }
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in specs:
            raise ExternalPolicyDenied("g2_action_denied")
        if set(action) not in specs[action["type"]]:
            raise ExternalPolicyDenied("g2_schema_denied")
        if "index" in action and (type(action["index"]) is not int
                                  or not 0 <= action["index"] < 100):
            raise ExternalPolicyDenied("g2_schema_denied")
        if "text" in action and (not isinstance(action["text"], str)
                                 or not 1 <= len(action["text"]) <= 100):
            raise ExternalPolicyDenied("g2_schema_denied")
        if "pixels" in action and (type(action["pixels"]) is not int
                                   or not 1 <= action["pixels"] <= 1500):
            raise ExternalPolicyDenied("g2_schema_denied")
        if "path" in action:
            from workflow_browser_external_identity import PATH_RE
            if (not isinstance(action["path"], str) or
                    not PATH_RE.fullmatch(action["path"]) or
                    "//" in action["path"] or ".." in action["path"].split("/") or
                    not isinstance(action.get("field"), str) or
                    not FIELD_RE.fullmatch(action["field"])):
                raise ExternalPolicyDenied("g2_search_scope_denied")
        if "query" in action and (not isinstance(action["query"], str)
                                  or not QUERY_RE.fullmatch(action["query"])):
            raise ExternalPolicyDenied("g2_query_denied")
    return tuple(dict(a) for a in actions)


def parse_query_url(url):
    """A single canonical GET parameter only, with separately approved field."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ExternalPolicyDenied("g2_query_denied")
    p = urlsplit(url)
    if not p.query:
        return None
    base = url.split("?", 1)[0]
    canonical_url(base)
    try:
        pairs = parse_qsl(p.query, strict_parsing=True, keep_blank_values=True)
    except ValueError as exc:
        raise ExternalPolicyDenied("g2_query_denied") from exc
    if len(pairs) != 1 or p.fragment:
        raise ExternalPolicyDenied("g2_query_denied")
    field, value = pairs[0]
    if not FIELD_RE.fullmatch(field) or not QUERY_RE.fullmatch(value) or (
        urlencode(pairs) != p.query):
        raise ExternalPolicyDenied("g2_query_denied")
    return base, field, value


# All JavaScript below is static, provided by trusted code, never by a webpage
# or model. It reads only link metadata, not form field values or credentials.
LINKS_JS = r"""(nodes) => nodes.map((a,i) => {
 const r=a.getBoundingClientRect();
 let clipped=false, opacity=1;
 let left=0, top=0, right=innerWidth, bottom=innerHeight;
 for(let p=a; p && p.nodeType===1; p=p.parentElement) {
   const s=getComputedStyle(p);
   opacity*=Number(s.opacity);
   if(s.display==='none' || s.visibility!=='visible' ||
      s.clipPath!=='none' || s.clip!=='auto' || s.transformStyle==='preserve-3d') clipped=true;
   if(['hidden','clip','scroll','auto'].includes(s.overflowX) ||
      ['hidden','clip','scroll','auto'].includes(s.overflowY)) {
     const b=p.getBoundingClientRect();
     left=Math.max(left,b.left); top=Math.max(top,b.top);
     right=Math.min(right,b.right); bottom=Math.min(bottom,b.bottom);
   }
 }
 const x0=Math.max(r.left,left), y0=Math.max(r.top,top);
 const x1=Math.min(r.right,right), y1=Math.min(r.bottom,bottom);
 const goodSize=r.width>=4 && r.height>=4 && x1-x0>=4 && y1-y0>=4;
 const points=[[.5,.5],[.2,.2],[.8,.2],[.2,.8],[.8,.8]];
 const hits=goodSize && points.every(([u,v])=>{
   const hit=document.elementFromPoint(x0+(x1-x0)*u,y0+(y1-y0)*v);
   return hit && hit.closest('a')===a;
 });
 return {index:i,text:(a.innerText||'').trim().slice(0,100),
   href:a.href,rel:a.rel,ok:!clipped && opacity>=.1 && goodSize && hits};
}).filter(a=>a.ok && a.text)"""
FORMS_JS = r"""(nodes) => nodes.map((f,index)=>{
 const controls=[...f.querySelectorAll('input,textarea,select,button')];
 const matches=controls.filter(c=>c.tagName==='INPUT' && c.type==='search');
 const safe=f.method.toLowerCase()==='get' && matches.length===1 &&
 controls.every(c=>c===matches[0] || (c.tagName==='BUTTON' && c.type==='submit')) &&
 !f.querySelector('[formaction]') && matches[0].name &&
 !matches[0].disabled && matches[0].getBoundingClientRect().width>0;
 return {index,safe:!!safe,action:f.action,field:matches[0]?.name||''};
})"""


class G2Browser(ExternalReadOnlyBrowser):
    def __init__(self, *, query_paths=frozenset(), grant_check=None,
                 query_check=None, path_check=None, **kwargs):
        if not enabled():
            raise ExternalPolicyDenied("g2_disabled")
        super().__init__(**kwargs)
        self.query_paths = frozenset(query_paths)
        self._grant_check = grant_check
        self._query_check = query_check
        self._path_check = path_check
        self._requests_limit = MAX_ACTIONS + 2
        self._started = None

    def _ensure_live(self):
        super()._ensure_live()
        if not enabled():
            raise ExternalPolicyDenied("g2_disabled")
        if self._grant_check is not None:
            self._grant_check()
        if self._started and time.monotonic() - self._started > 25:
            raise ExternalPolicyDenied("g2_time_budget_denied")

    def _verify_url(self, url, method):
        parsed = parse_query_url(url)
        if parsed is None:
            super()._verify_url(url, method)
            if self._path_check is not None and not self._path_check(url):
                raise ExternalPolicyDenied("g2_path_approval_revoked")
            return
        base, field, _ = parsed
        if method != "GET":
            raise ExternalPolicyDenied("unsafe_http_method")
        self.site.check_url(base, method)
        if ((base, field) not in self.query_paths or
                (self._query_check is not None and
                 not self._query_check(base, field))):
            raise ExternalPolicyDenied("g2_query_approval_missing")

    def _request_path(self, url):
        parsed = parse_query_url(url)
        if parsed is None:
            return super()._request_path(url)
        return canonical_url(parsed[0])[2] + "?" + urlsplit(url).query

    async def _guard(self):
        self._ensure_live()
        if self._page is None or len(self._page.frames) != 1:
            raise ExternalPolicyDenied("g2_frame_denied")
        if self._page.url != self._active_url:
            raise ExternalPolicyDenied("g2_navigation_race_denied")
        if await self._page.locator(
            "iframe,frame,object,embed,input[type=password],input[type=file],"
            "[contenteditable],input[autocomplete*=password],"
            "input[autocomplete^=cc-],template[shadowrootmode]"
        ).count():
            raise ExternalPolicyDenied("g2_sensitive_dom_denied")
        # CDP traversal pierces closed shadow roots. Treat any such root or
        # embedded browsing context as unsupported rather than a safe target.
        session = None
        try:
            session = await self._context.new_cdp_session(self._page)
            tree = await session.send("DOM.getDocument", {"depth": -1, "pierce": True})
            nodes = [tree["root"]]
            while nodes:
                node = nodes.pop()
                # Native <input>/<details> contain browser-owned user-agent
                # shadow trees. Reject *author* roots (open or closed), not UA.
                shadows = node.get("shadowRoots", [])
                if node.get("contentDocument") or any(
                        root.get("shadowRootType") != "user-agent"
                        for root in shadows):
                    raise ExternalPolicyDenied("g2_hidden_realm_denied")
                attrs = node.get("attributes", [])
                if len(attrs) % 2:
                    raise ExternalPolicyDenied("g2_cdp_denied")
                values = dict(zip(attrs[::2], attrs[1::2]))
                name = (node.get("localName") or "").lower()
                if name in ("iframe", "frame", "object", "embed") or (
                    name == "input" and values.get("type", "").lower() in ("password", "file")
                ) or "contenteditable" in values or "shadowrootmode" in values:
                    raise ExternalPolicyDenied("g2_sensitive_dom_denied")
                nodes.extend(node.get("children", []))
        except ExternalPolicyDenied:
            raise
        except Exception as exc:
            raise ExternalPolicyDenied("g2_cdp_denied") from exc
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception as exc:
                    raise ExternalPolicyDenied("g2_cdp_denied") from exc
        self._ensure_live()

    async def _links(self):
        await self._guard()
        links = await self._page.locator("a[href]").evaluate_all(LINKS_JS)
        await self._guard()
        if not isinstance(links, list) or len(links) > 200:
            raise ExternalPolicyDenied("g2_links_denied")
        return links

    async def _visit(self, url):
        self._ensure_live()
        self._verify_url(url, "GET")
        self._active_url = url
        response = await self._page.goto(url, wait_until="domcontentloaded", timeout=10000)
        self._ensure_live()
        if response is None or response.status != 200 or self._page.url != url:
            raise ExternalPolicyDenied("g2_navigation_denied")
        await self._guard()

    async def browse(self, url, actions):
        if self._page is None or self._active_url is not None:
            raise ExternalPolicyDenied("session_already_used")
        self._started = time.monotonic()
        try:
            self._audit("OTHER", "allowed", "g2_step_initial")
            await self._visit(url)
            for a in actions:
                kind = a["type"]
                self._audit("OTHER", "allowed", "g2_step_" + kind)
                if kind in ("follow_link", "paginate"):
                    before = await self._links()
                    if kind == "paginate":
                        selected = [x for x in before if "next" in x["rel"].lower().split()
                                    or x["text"].lower() in ("next", "next page", "older", "下一页")]
                    elif "text" in a:
                        selected = [x for x in before if x["text"] == a["text"]]
                    else:
                        selected = [x for x in before if x["index"] == a["index"]]
                    if len(selected) != 1:
                        raise ExternalPolicyDenied("g2_target_denied")
                    if await self._links() != before:
                        raise ExternalPolicyDenied("g2_dom_race_denied")
                    # Never click: a mutated onclick/overlay cannot change destination.
                    self._verify_url(selected[0]["href"], "GET")
                    await self._visit(selected[0]["href"])
                elif kind == "scroll":
                    await self._guard()
                    await self._page.mouse.wheel(0, a["pixels"])
                    await self._guard()
                elif kind == "expand":
                    await self._guard()
                    nodes = self._page.locator("details")
                    if a["index"] >= await nodes.count():
                        raise ExternalPolicyDenied("g2_target_denied")
                    summary = nodes.nth(a["index"]).locator("summary")
                    if await summary.count() != 1 or not await summary.is_visible():
                        raise ExternalPolicyDenied("g2_target_denied")
                    await summary.click(timeout=1500)
                    await self._guard()
                elif kind == "search_get":
                    await self._guard()
                    forms = await self._page.locator("form").evaluate_all(FORMS_JS)
                    i = a.get("index", 0)
                    if i >= len(forms) or not forms[i]["safe"]:
                        raise ExternalPolicyDenied("g2_form_denied")
                    if forms != await self._page.locator("form").evaluate_all(FORMS_JS):
                        raise ExternalPolicyDenied("g2_dom_race_denied")
                    form = forms[i]
                    canonical_url(form["action"])
                    if (form["action"] != self.site.origin + a["path"] or
                            form["field"] != a["field"]):
                        raise ExternalPolicyDenied("g2_form_scope_mismatch")
                    if not FIELD_RE.fullmatch(form["field"]):
                        raise ExternalPolicyDenied("g2_form_denied")
                    dest = form["action"] + "?" + urlencode([(form["field"], a["query"])])
                    self._verify_url(dest, "GET")
                    await self._visit(dest)
            await self._guard()
            body = await self._page.locator("body").inner_text(timeout=2000)
            listing = "\n".join(f"[{x['index']}] {x['text']}" for x in
                                (await self._links())[:30])
            return UntrustedPageText(self.site.origin,
                body[:50000] + "\nVisible links (indices):\n" + listing)
        except ExternalPolicyDenied as exc:
            self._poison(str(exc))
            self._audit("OTHER", "denied", str(exc))
            raise
        except Exception as exc:
            self._poison("g2_interaction_denied")
            self._audit("OTHER", "denied", "g2_interaction_denied")
            raise ExternalPolicyDenied("g2_interaction_denied") from exc
