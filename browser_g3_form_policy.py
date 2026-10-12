"""Fail-closed G3 form-scoped classification; ignore unrelated footer/nav links."""
from __future__ import annotations

import re
import unicodedata

# Normalize before classifying so visually disguised UI words cannot bypass safety.
_INVISIBLE = re.compile(r"[\u00ad\u034f\u061c\u180e\u200b-\u200f\u2060-\u206f\ufeff]")
_SPACES = re.compile(r"\s+")


def _text(value: str) -> str:
    return _SPACES.sub(" ", _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))).strip()


DANGER = re.compile(
    r"(checkout|purchase|payment|credit.card|billing|cvv|pay now|place order|shopping cart|"
    r"log[-_/ ]*in|sign[-_/ ]*in|sign[-_/ ]*up|password|"
    r"account[-_/ ]*(?:delet|clos|remov|deactiv)|"
    r"(?:delete|deletion|remove|removal|close|deactivate)[-_/ ]*(?:my[-_/ ]*)?account|"
    r"close[-_/ ]+your[-_/ ]+account|card[-_/ ]+number|security[-_/ ]+code|"
    r"cancel[-_/ ]?subscription|付款|支付|结账|登录|账户删除|删除账户)", re.I)
SENSITIVE_FIELD = re.compile(
    r"(password|passwd|secret|token|csrf|auth|session|cookie|cc[-_ ]|"
    r"credit|debit|card|cvv|cvc|iban|routing|bank|ssn|"
    r"one[-_ /]*time(?:[-_ /]*code)?|otp|2fa|mfa|email|phone|account|"
    r"security[-_ /]*code)", re.I)
DESTRUCTIVE_ACTION = re.compile(
    r"\b(?:delete|remove|deactivate|erase|unsubscribe|terminate|reset|wipe|purge)\b|"
    r"删除|注销|退订", re.I)
SENSITIVE_ACTION = re.compile(
    r"\b(?:pay|charge|buy|donate|refund|purchase|checkout|authenticate|oauth|sso)\b|"
    r"支付|付款|结账", re.I)


def _dangerous(value: str, *, field: bool = False) -> bool:
    normalized = _text(value)
    return bool(
        DANGER.search(normalized)
        or DESTRUCTIVE_ACTION.search(normalized)
        or SENSITIVE_ACTION.search(normalized)
        or (field and SENSITIVE_FIELD.search(normalized))
    )


def sensitive_form(url: str, form: dict) -> bool:
    """Classify only selected-form metadata plus document title/preceding headings.

    No arbitrary body text, footer, navigation or unrelated anchor text is used.
    Malformed metadata is refused instead of silently exempted.
    """
    if not isinstance(form, dict) or not isinstance(url, str):
        return True
    if _dangerous(url):
        return True
    for key in ("target", "heading", "page_heading", "document_title", "submit_label"):
        value = form.get(key, "")
        if not isinstance(value, str) or _dangerous(value):
            return True
    controls = form.get("controls", [])
    if not isinstance(controls, list):
        return True
    for control in controls:
        if not isinstance(control, dict):
            return True
        # c.labels, including labels explicitly linked by for/id, and accessibility hints.
        labels = control.get("labels", [])
        if not isinstance(labels, list):
            return True
        for value in [*labels, *(control.get(k, "") for k in
                                 ("aria_label", "aria_labelledby", "placeholder", "title"))]:
            if not isinstance(value, str) or _dangerous(value, field=True):
                return True
    return False
