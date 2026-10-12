"""G3 safety boundary for static, unauthenticated, user-reviewed forms.

The page never receives a browser submit event: after revalidation, the
reviewed form data is encoded by trusted code for one pinned TLS request.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from urllib.parse import urlencode

from workflow_browser_g2 import G2Browser, enabled as g2_enabled
from workflow_browser_external_identity import ExternalPolicyDenied, canonical_url
from personal_tools.browser_read import _requested_url
from workflow_browser_g3_store import digest

DANGER = re.compile(
    r"(checkout|purchase|payment|credit.card|billing|cvv|pay now|place order|shopping cart|"
    r"log[-_/ ]?in|sign[-_/ ]?in|sign[-_/ ]?up|password|"
    r"account[-_/ ]*(?:delet|clos|remov|deactiv)|"
    r"(?:delete|deletion|remove|removal|close|deactivate)[-_/ ]*(?:my[-_/ ]*)?account|"
    r"cancel[-_/ ]?subscription|"
    r"付款|支付|结账|登录|账户删除|删除账户)",re.I)
SENSITIVE = re.compile(
    r"(password|passwd|secret|token|csrf|auth|session|cookie|"
    r"cc-|credit|debit|card|cvv|cvc|iban|routing|bank|ssn|"
    r"one.time|otp|2fa|mfa|email|phone|account)",re.I)
FORM_JS = r"""(forms, args) => {
 const [index, changed] = args;
 if (!Number.isInteger(index) || index<0 || index>=forms.length) return {error:'g3_form_missing'};
 if (document.querySelector('iframe,frame,object,embed,[form],fieldset,template[shadowrootmode]'))
   return {error:'g3_complex_dom_denied'};
 const f=forms[index], attrs=[...f.attributes].map(a=>[a.name,a.value]);
 if (f.getAttribute('onsubmit') || f.querySelector('[formaction],[formmethod],[formenctype],[formnovalidate]'))
   return {error:'g3_submit_override_denied'};
 const allowed=['text','search','checkbox','hidden','submit','button'];
 const controls=[...f.elements];
 if (controls.length>30 || !controls.length) return {error:'g3_control_budget_denied'};
 const found=new Set(), out=[], pairs=[];
 for(const c of controls) {
   const tag=c.tagName.toLowerCase();
   const type=tag==='input'?c.type.toLowerCase():(tag==='button'?'button':tag);
   const name=c.getAttribute('name')||'';
   const ac=(c.getAttribute('autocomplete')||'').toLowerCase();
   if (!['input','textarea','select','button'].includes(tag) ||
       (tag==='input' && !allowed.includes(type)) ||
       (tag==='select' && c.multiple) || c.isContentEditable ||
       ac.includes('password') || ac.startsWith('cc-') || ac.includes('one-time-code') ||
       c.getAttribute('form') || c.getAttribute('dirname'))
       return {error:'g3_sensitive_field_denied'};
   if ((type==='submit'||tag==='button') && name)
       return {error:'g3_named_submit_denied'};
   if (name && (!/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(name) ||
       /password|passwd|secret|token|csrf|auth|session|cookie|cc-|credit|debit|card|cvv|cvc|iban|routing|bank|ssn|one.time|otp|2fa|mfa|email|phone|account/i.test(name)))
       return {error:'g3_sensitive_field_denied'};
   if (name && found.has(name)) return {error:'g3_duplicate_field_denied'};
   if(name) found.add(name);
   const editable=type!=='hidden' && type!=='submit' && tag!=='button' && !c.disabled;
   if(editable) {
     const rect=c.getBoundingClientRect();
     let opacity=1, clipped=false;
     for(let el=c;el&&el.nodeType===1;el=el.parentElement) {
       const st=getComputedStyle(el);
       opacity*=Number(st.opacity);
       if(st.display==='none'||st.visibility!=='visible'||st.clipPath!=='none')
         clipped=true;
       if(el!==c && (['hidden','clip','scroll','auto'].includes(st.overflowX) ||
                     ['hidden','clip','scroll','auto'].includes(st.overflowY))) {
         const pr=el.getBoundingClientRect();
         if(rect.left<pr.left||rect.right>pr.right||rect.top<pr.top||rect.bottom>pr.bottom)
           clipped=true;
       }
     }
     const points=[[.5,.5],[.2,.2],[.8,.2],[.2,.8],[.8,.8]];
     const hits=points.every(([u,v])=>{
       const x=rect.left+rect.width*u,y=rect.top+rect.height*v;
       if(x<0||y<0||x>=innerWidth||y>=innerHeight) return false;
       const hit=document.elementFromPoint(x,y);
       return hit===c||c.contains(hit);
     });
     if(clipped||opacity<.1||rect.width<4||rect.height<4||!hits)
       return {error:'g3_invisible_control_denied'};
   }
   if(Object.prototype.hasOwnProperty.call(changed,name)) {
     if(!editable || !name || !(type==='text'||type==='search'||type==='textarea'||type==='select'||type==='checkbox'))
       return {error:'g3_uneditable_field_denied'};
     const val=changed[name];
     if(type==='checkbox') {
       if(typeof val!=='boolean') return {error:'g3_invalid_field_value'};
       c.checked=val;
     } else if(typeof val==='string' && val.length<=120 && !/[\r\n\0]/.test(val)) {
       if(tag==='select' && ![...c.options].some(o=>o.value===val && !o.disabled))
         return {error:'g3_invalid_select_value'};
       c.value=val;
       if(c.value!==val) return {error:'g3_field_readback_denied'};
     } else return {error:'g3_invalid_field_value'};
   }
   let val=type==='checkbox'?(c.checked?c.value:null):c.value;
   if(typeof val==='string' && val.length>256) return {error:'g3_field_too_long'};
   out.push({tag,type,name,value:val,disabled:!!c.disabled});
   if(name && !c.disabled && type!=='submit' && tag!=='button' &&
      !(type==='checkbox' && !c.checked)) pairs.push([name,val]);
 }
 for(const key of Object.keys(changed)) if(!found.has(key)) return {error:'g3_unknown_field'};
 const method=f.method.toUpperCase(), target=f.action;
 if(!['GET','POST'].includes(method) || !/^application\/x-www-form-urlencoded/i.test(f.enctype) ||
    (f.getAttribute('accept-charset')||'').toLowerCase().replace(/[-_]/g,'')!==''
    && (f.getAttribute('accept-charset')||'').toLowerCase().replace(/[-_]/g,'')!=='utf8')
   return {error:'g3_form_encoding_denied'};
 return {method,target,controls:out,fields:pairs,attrs};
}"""

def enabled():
    return (g2_enabled() and os.getenv("CONVEYOR_BROWSER_EXTERNAL_SUBMIT")=="1")

def validate_request(arg):
    if not isinstance(arg,str) or len(arg)>4000:
        raise ExternalPolicyDenied("g3_schema_denied")
    try:
        payload=json.loads(arg)
    except (ValueError,TypeError) as exc:
        raise ExternalPolicyDenied("g3_schema_denied") from exc
    if not isinstance(payload,dict) or set(payload)!={"url","form_ref","fields"}:
        raise ExternalPolicyDenied("g3_schema_denied")
    url=payload["url"]; _requested_url(url)
    idx=payload["form_ref"]; fields=payload["fields"]
    if type(idx) is not int or not 0<=idx<20 or not isinstance(fields,dict) or len(fields)>20:
        raise ExternalPolicyDenied("g3_schema_denied")
    for key,val in fields.items():
        if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}",key):
            raise ExternalPolicyDenied("g3_schema_denied")
        if not (type(val) is bool or (type(val) is str and len(val)<=120
                and not re.search(r"[\r\n\x00]",val))):
            raise ExternalPolicyDenied("g3_schema_denied")
        if SENSITIVE.search(key):
            raise ExternalPolicyDenied("g3_sensitive_field_denied")
    if DANGER.search(url):
        raise ExternalPolicyDenied("g3_sensitive_page_denied")
    return url,idx,fields

class G3Browser(G2Browser):
    def __init__(self, **kwargs):
        if not enabled():
            raise ExternalPolicyDenied("g3_disabled")
        super().__init__(**kwargs)

    def _ensure_live(self):
        super()._ensure_live()
        if not enabled():
            raise ExternalPolicyDenied("g3_disabled")

    async def preview(self,url,index,fields):
        if self._active_url is not None:
            raise ExternalPolicyDenied("session_already_used")
        await self._visit(url)
        await self._guard()
        body=await self._page.locator("body").inner_text(timeout=2000)
        if DANGER.search(url+" "+body[:8192]):
            raise ExternalPolicyDenied("g3_sensitive_page_denied")
        result=await self._page.locator("form").evaluate_all(FORM_JS,[index,fields])
        if not isinstance(result,dict) or "error" in result:
            raise ExternalPolicyDenied(result.get("error","g3_snapshot_denied")
                                       if isinstance(result,dict) else "g3_snapshot_denied")
        target=result["target"]; method=result["method"]
        # Never allow query, fragment, other scheme/origin or non-canonical target.
        host,port,path,origin=_requested_url(target)
        if DANGER.search(target) or origin!=self.site.origin:
            raise ExternalPolicyDenied("g3_target_denied")
        # The caller checks the exact action URL against the independent
        # operator G1 grant immediately after the page preview is captured.
        if method not in ("GET","POST"):
            raise ExternalPolicyDenied("g3_method_denied")
        if sum(len(str(k))+len(str(v)) for k,v in result["fields"])>4096:
            raise ExternalPolicyDenied("g3_payload_too_large")
        await self._guard()
        # The screenshot is privately held for an authenticated human, not LLM text.
        screen=await self._page.screenshot(type="png", animations="disabled")
        if len(screen)>400_000:
            raise ExternalPolicyDenied("g3_screenshot_too_large")
        await self._guard()
        preview={"url":url,"form_ref":index,"method":method,"target":target,
                 "fields":result["fields"],"controls":result["controls"],
                 "form_attrs":result["attrs"],"screenshot_sha256":hashlib.sha256(screen).hexdigest()}
        return preview,screen
