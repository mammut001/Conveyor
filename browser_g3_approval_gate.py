"""G3 Web approval binds a fresh operator decision to the displayed preview."""
from __future__ import annotations
import hmac
import json
from workflow_browser_g3_store import review

def matches(settings, arg, expected_hash):
    if not isinstance(expected_hash,str) or len(expected_hash)!=64:
        return False
    try:
        token_and_hash=json.loads(arg)
        if not isinstance(token_and_hash,dict) or set(token_and_hash)!={"token","hash"}:
            return False
        entry=review(settings,token_and_hash["token"])
        return (entry is not None and
                hmac.compare_digest(expected_hash,token_and_hash["hash"]) and
                hmac.compare_digest(expected_hash,entry["hash"]))
    except (TypeError,ValueError,KeyError):
        return False
