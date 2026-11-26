import json
import tempfile
import unittest
from pathlib import Path

from supervisor_validation import infer_validation


class ValidationDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_package_script_uses_declared_package_manager(self):
        (self.root / "package.json").write_text(json.dumps({
            "packageManager": "pnpm@9.0.0", "scripts": {"test": "vitest run"},
        }))
        commands, source = infer_validation(str(self.root))
        self.assertEqual(commands, [["pnpm", "run", "test"]])
        self.assertEqual(source, "package.json scripts.test")

    def test_placeholder_or_unknown_projects_do_not_get_a_fake_test(self):
        self.assertIsNone(infer_validation(str(self.root)))
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "echo no test specified && exit 1"}}))
        self.assertIsNone(infer_validation(str(self.root)))

    def test_python_test_frameworks_are_detected_conservatively(self):
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_case.py").write_text("def test_case(): pass\n")
        self.assertIsNone(infer_validation(str(self.root)))
        (self.root / "pytest.ini").write_text("[pytest]\n")
        self.assertEqual(infer_validation(str(self.root))[0][0][1:], ["-m", "pytest"])
        (self.root / "pytest.ini").unlink()
        (tests / "test_case.py").write_text("import unittest\n")
        self.assertEqual(infer_validation(str(self.root))[0][0][1:], ["-m", "unittest", "discover", "-s", "tests"])


if __name__ == "__main__":
    unittest.main()
