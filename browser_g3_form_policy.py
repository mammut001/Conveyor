"""Fail-closed G3 form-scoped classification; unrelated page/footer links are not evidence."""
from __future__ import annotations
import re

DANGER = re.compile(
    r"(checkout|purchase|payment|credit.card|billing|cvv|pay now|place order|shopping cart|"
    r"log[-_/ ]?in|sign[-_/ ]?in|sign[-_/ ]?up|password|"
    r"account[-_/ ]*(?:delet|clos|remov|deactiv)|"
    r"(?:delete|deletion|remove|removal|close|deactivate)[-_/ ]*(?:my[-_/ ]*)?account|"
    r"cancel[-_/ ]?subscription|"
    r"付款|支付|结账|登录|账户删除|删除账户)", re.I)
DESTRUCTIVE_ACTION = re.compile(
    r"\b(?:delete|remove|deactivate|erase|unsubscribe|terminate|reset account)\b|"
    r"删除|注销|退订", re.I)


def sensitive_form(url: str, form: dict) -> bool:
    """Examine the selected form and its URL, not arbitrary page-wide body text."""
    if DANGER.search(url):
        return True
    for key in ("target", "heading", "submit_label"):
        value = form.get(key, "")
        if not isinstance(value, str):
            return True
        if DANGER.search(value) or (key != "target" and DESTRUCTIVE_ACTION.search(value)):
            return True
    return False
