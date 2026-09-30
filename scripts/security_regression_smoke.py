#!/usr/bin/env python3
import asyncio
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results

# Import test functions from existing scripts
from scripts.redaction_policy_smoke import test_redaction_policy
from scripts.security_audit_smoke import test_permissions
from scripts.quota_smoke import test_rate_limiting, test_queue_limit, test_worktree_size_limit
from scripts.apply_policy_smoke import test_apply_policy, test_apply_policy_hardening


def test_exception_traceback_redaction() -> CheckResult:
    from redaction import SecretRedactingFilter
    
    # Create a log record with exception info carrying a secret key
    logger = logging.getLogger("test_redact_exc")
    record = None
    try:
        raise ValueError("failed with key: sk-abcdefghijklmnopqrstuvwx")
    except ValueError:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        record = logger.makeRecord(
            name="test_logger",
            level=logging.ERROR,
            fn="foo.py",
            lno=10,
            msg="An error occurred with bot12345:ABCdefghijkLMnoPQRstuvwx",
            args=(),
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        
    filt = SecretRedactingFilter()
    filt.filter(record)
    
    # Assert record.exc_info is cleared
    ok_exc_info_cleared = (record.exc_info is None)
    
    # Format the record
    formatter = logging.Formatter("%(message)s\n%(exc_text)s")
    formatted = formatter.format(record)
    
    # Check that secrets are redacted from both record msg and exception text
    ok_msg = "bot12345:" not in record.msg or "[REDACTED]" in record.msg
    ok_exc = "sk-" not in formatted or "[REDACTED]" in formatted
    ok_raw_secret_absent = "sk-abcdefghijklmnopqrstuvwx" not in formatted
    
    ok = ok_msg and ok_exc and ok_exc_info_cleared and ok_raw_secret_absent
    
    return CheckResult(
        "exception_traceback_redaction",
        ok,
        f"msg_redacted={ok_msg} exc_redacted={ok_exc} exc_info_cleared={ok_exc_info_cleared} raw_absent={ok_raw_secret_absent} formatted={formatted.strip()}"
    )


def test_non_str_arg_and_bare_token_redaction() -> CheckResult:
    import re
    from redaction import SecretRedactingFilter

    logger = logging.getLogger("test_redact_non_str")

    class FakeUrl:
        def __init__(self, url: str) -> None:
            self._url = url

        def __str__(self) -> str:
            return self._url

    bare_token = "123456789:AAHfakefakefakefakefakefakefakefake1"
    url_obj = FakeUrl(f"https://api.telegram.org/bot{bare_token}/getMe")
    try:
        import httpx
        url_obj = httpx.URL(f"https://api.telegram.org/bot{bare_token}/getMe")
    except ImportError:
        pass

    record = logger.makeRecord(
        name="test_logger",
        level=logging.INFO,
        fn="foo.py",
        lno=10,
        msg="Requesting %s with bare token %s",
        args=(url_obj, bare_token),
        exc_info=None,
    )

    filt = SecretRedactingFilter()
    filt.filter(record)

    formatter = logging.Formatter("%(message)s")
    formatted = formatter.format(record)

    ok_token_absent = bare_token not in formatted
    ok_redacted_present = "[REDACTED]" in formatted

    no_bare_match = re.search(r"\d{6,}:[A-Za-z0-9_-]{20,}", formatted) is None
    no_bot_match = re.search(r"bot\d+:[A-Za-z0-9_-]{20,}", formatted) is None

    ok = ok_token_absent and ok_redacted_present and no_bare_match and no_bot_match

    return CheckResult(
        "non_str_arg_and_bare_token_redaction",
        ok,
        f"token_absent={ok_token_absent} redacted_present={ok_redacted_present} no_bare={no_bare_match} no_bot={no_bot_match} formatted={formatted}",
    )


def test_desktop_observe_validation() -> CheckResult:
    # Test path traversal and symlink rejection inside validate_observe_result
    from desktop_observe_requests import validate_observe_result
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmp:
        screenshot_dir = Path(tmp) / "screenshots"
        screenshot_dir.mkdir()
        
        valid_result = {
            "screenshot_id": "shot-1",
            "path": str(screenshot_dir / "shot.png"),
            "metadata_path": str(screenshot_dir / "shot.json"),
            "sha256": "a" * 64,
            "width": 10,
            "height": 10,
            "bytes": 100,
            "node_id": "macbook-payton",
        }
        
        # Valid path should pass
        res_valid = validate_observe_result(valid_result, screenshot_dir)
        ok_valid = res_valid is not None
        
        # Traversal should fail
        traversal_result = {**valid_result, "path": "/etc/passwd"}
        res_traversal = validate_observe_result(traversal_result, screenshot_dir)
        ok_traversal = res_traversal is None
        
        # Symlink should fail
        link_path = screenshot_dir / "link.png"
        try:
            link_path.symlink_to(screenshot_dir / "shot.png")
            symlink_result = {**valid_result, "path": str(link_path)}
            res_symlink = validate_observe_result(symlink_result, screenshot_dir)
            ok_symlink = res_symlink is None
        except OSError:
            ok_symlink = True # skip if symlink creation is not permitted by OS
            
        return CheckResult(
            "desktop_observe_validation_constraints",
            ok_valid and ok_traversal and ok_symlink,
            f"valid={ok_valid} traversal_blocked={ok_traversal} symlink_blocked={ok_symlink}"
        )


def main() -> int:
    # 1. Run redaction tests
    results = test_redaction_policy()
    
    # 2. Run traceback log redaction check
    results.append(test_exception_traceback_redaction())
    results.append(test_non_str_arg_and_bare_token_redaction())
    
    # 3. Run desktop observe validation check
    results.append(test_desktop_observe_validation())
    
    # 4. Run permissions audit tests
    results.extend(test_permissions())
    
    # 5. Run quota tests
    results.append(test_rate_limiting())
    results.append(test_queue_limit())
    results.append(test_worktree_size_limit())
    
    # 6. Run apply policy tests
    results.extend(test_apply_policy())
    results.extend(asyncio.run(test_apply_policy_hardening()))
    
    print_results(results)
    ok = all(r.ok for r in results)
    print("============================================================")
    print("Security regression smoke ok" if ok else "Security regression smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
