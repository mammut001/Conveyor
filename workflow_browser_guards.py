"""Fail-closed browser guards kept separate from the real-browser adapter.

The init script runs in every page/frame before site JavaScript. Chromium CDP
provides a second DOM view that includes closed shadow-root descendants.
No DOM text, field values, URLs or credentials are returned or logged.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EXTRA_INIT_SCRIPT = r"""
(() => {
  // Use the same sticky denial marker inspected by the existing init script.
  const deny = (reason) => {
    if (!window.__conveyorUnsafeDOMReason)
      window.__conveyorUnsafeDOMReason = reason;
    throw new Error(reason);
  };
  const workerDenied = function() { return deny('worker_denied'); };
  for (const name of ['Worker', 'SharedWorker']) {
    Object.defineProperty(window, name, {
      configurable: false, writable: false, value: workerDenied
    });
  }

  const documentDenied = function() { return deny('document_write_denied'); };
  for (const name of ['open', 'write', 'writeln']) {
    Object.defineProperty(Document.prototype, name, {
      configurable: false, writable: false, value: documentDenied
    });
  }

  // The Playwright context also sets service_workers='block'. Intercept
  // registration itself so even an attempted registration is journaled.
  if (typeof ServiceWorkerContainer !== 'undefined') {
    Object.defineProperty(ServiceWorkerContainer.prototype, 'register', {
      configurable: false, writable: false, value: workerDenied
    });
  }
})();
"""

_UNSAFE_INPUT_TYPES = frozenset({"password", "file", "submit", "image"})
_SENSITIVE_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code",
})
_CHILD_ARRAYS = ("children", "shadowRoots", "pseudoElements", "distributedNodes")
_CHILD_OBJECTS = ("templateContent", "contentDocument")


def _attributes(node: Mapping[str, Any]) -> dict[str, str] | None:
    """Decode CDP's flat alternating name/value attribute vector."""
    raw = node.get("attributes", [])
    if not isinstance(raw, list) or len(raw) % 2:
        return None
    if any(not isinstance(value, str) for value in raw):
        return None
    return {raw[i].lower(): raw[i + 1] for i in range(0, len(raw), 2)}


def inspect_cdp_dom(root: Any) -> str | None:
    """Pure, recursive-in-effect CDP node scanner; malformed trees fail closed.

    Traverses CDP shadowRoots (including type 'closed'), templateContent,
    contentDocument and normal children. Never reads attribute *values* except
    known policy-relevant control metadata.
    """
    if not isinstance(root, Mapping):
        return "cdp_dom_unavailable"
    pending = [root]
    while pending:
        node = pending.pop()
        if not isinstance(node, Mapping):
            return "cdp_dom_unavailable"
        attrs = _attributes(node)
        if attrs is None:
            return "cdp_dom_unavailable"
        name = node.get("localName") or node.get("nodeName") or ""
        if not isinstance(name, str):
            return "cdp_dom_unavailable"
        name = name.lower()

        if name == "template" and "shadowrootmode" in attrs:
            return "declarative_shadow_denied"
        if "contenteditable" in attrs:
            return "sensitive_or_submit_dom_denied"
        if name == "input" and attrs.get("type", "text").lower() in _UNSAFE_INPUT_TYPES:
            return "sensitive_or_submit_dom_denied"
        # In HTML the missing/invalid button type defaults to submit.
        if name == "button" and attrs.get("type", "submit").lower() not in ("button", "reset"):
            return "sensitive_or_submit_dom_denied"
        if name == "form" and "action" in attrs:
            return "sensitive_or_submit_dom_denied"
        autocomplete = attrs.get("autocomplete", "").lower().split()
        if any(part.startswith("cc-") or part in _SENSITIVE_AUTOCOMPLETE
               for part in autocomplete):
            return "sensitive_autocomplete_denied"

        for key in _CHILD_ARRAYS:
            descendants = node.get(key, [])
            if not isinstance(descendants, list):
                return "cdp_dom_unavailable"
            pending.extend(reversed(descendants))
        for key in _CHILD_OBJECTS:
            descendant = node.get(key)
            if descendant is not None:
                if not isinstance(descendant, Mapping):
                    return "cdp_dom_unavailable"
                pending.append(descendant)
    return None


def inspect_page_via_cdp(page: Any) -> str | None:
    """Pierce closed shadow roots using Chromium CDP, failing closed on errors.

    A fresh session avoids retaining DOM handles or leaking sensitive values
    through logging. Only a fixed denial code escapes this function.
    """
    session = None
    try:
        session = page.context.new_cdp_session(page)
        result = session.send("DOM.getDocument", {"depth": -1, "pierce": True})
        if not isinstance(result, Mapping):
            return "cdp_dom_unavailable"
        return inspect_cdp_dom(result.get("root"))
    except Exception:
        return "cdp_dom_unavailable"
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
