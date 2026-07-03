#!/usr/bin/env python3
import sys
import tempfile
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results
from scripts.security_audit import check_file_private, check_dir_private

def test_permissions() -> list[CheckResult]:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        
        # Test file with 0644 (should fail)
        f_bad = tmp_dir / "f_bad.env"
        f_bad.write_text("secret")
        f_bad.chmod(0o644)
        res1 = check_file_private(f_bad)
        results.append(CheckResult("mode 0644 secret file fails", not res1.ok, f"ok={res1.ok} detail={res1.detail}"))
        
        # Test file with 0600 (should pass)
        f_good = tmp_dir / "f_good.env"
        f_good.write_text("secret")
        f_good.chmod(0o600)
        res2 = check_file_private(f_good)
        results.append(CheckResult("mode 0600 passes", res2.ok, f"ok={res2.ok} detail={res2.detail}"))
        
        # Test dir with 0755 (should fail)
        d_bad = tmp_dir / "d_bad"
        d_bad.mkdir()
        d_bad.chmod(0o755)
        res3 = check_dir_private(d_bad)
        results.append(CheckResult("0755 memory dir fails", not res3.ok, f"ok={res3.ok} detail={res3.detail}"))
        
        # Test dir with 0700 (should pass)
        d_good = tmp_dir / "d_good"
        d_good.mkdir()
        d_good.chmod(0o700)
        res4 = check_dir_private(d_good)
        results.append(CheckResult("0700 passes", res4.ok, f"ok={res4.ok} detail={res4.detail}"))
        
    return results

def main() -> int:
    results = test_permissions()
    print_results(results)
    ok = all(r.ok for r in results)
    print("security audit permissions smoke ok" if ok else "security audit permissions smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
