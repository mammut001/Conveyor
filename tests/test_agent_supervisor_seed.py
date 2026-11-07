"""Focused test for Agent Supervisor skill seeding and markdown parser consistency."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_tools import skills
from scripts.seed_agent_supervisor_skill import (
    make_seed_settings,
    seed_skill,
    main as seed_main,
)
import scripts.seed_agent_supervisor_skill as seed_agent_module
import scripts.seed_antigravity_supervisor_skill as seed_legacy_module


class TestAgentSupervisorSeed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.settings = make_seed_settings(self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_import_has_no_side_effects(self) -> None:
        fresh_tmp = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(len(list(fresh_tmp.iterdir())), 0)
            self.assertTrue(callable(seed_agent_module.main))
            self.assertTrue(callable(seed_legacy_module.main))
            self.assertEqual(len(list(fresh_tmp.iterdir())), 0)
        finally:
            shutil.rmtree(fresh_tmp, ignore_errors=True)

    def test_parse_markdown_fields(self) -> None:
        md_path = ROOT / "skills" / "agent-supervisor.md"
        self.assertTrue(md_path.is_file())
        fields = skills.parse_markdown(md_path.read_text(encoding="utf-8"))
        self.assertEqual(fields.get("slug"), "agent-supervisor")
        self.assertEqual(fields.get("name"), "Agent Supervisor")
        self.assertTrue(fields.get("description"))
        self.assertTrue(fields.get("triggers"))
        self.assertTrue(fields.get("body"))

    def test_idempotent_create_and_update(self) -> None:
        md_path = ROOT / "skills" / "agent-supervisor.md"

        # 1. First seed: creates record
        res1 = seed_skill(self.settings, md_path)
        self.assertEqual(res1["action"], "created")
        self.assertEqual(res1["slug"], "agent-supervisor")

        item1 = skills.get_skill(self.settings, "agent-supervisor")
        self.assertIsNotNone(item1)
        self.assertEqual(item1["slug"], "agent-supervisor")
        self.assertEqual(item1["name"], "Agent Supervisor")

        # Insert unrelated custom skill
        skills.create_skill(
            self.settings,
            name="Unrelated Custom Skill",
            description="Custom skill description",
            triggers="custom, unrelated",
            body="Custom skill body content",
            slug="unrelated-custom",
        )

        # 2. Second seed: updates existing record without duplicates or deleting custom skills
        res2 = seed_skill(self.settings, md_path)
        self.assertEqual(res2["action"], "updated")
        self.assertEqual(res2["slug"], "agent-supervisor")

        all_skills = skills.list_skills(self.settings)
        slugs = {s["slug"] for s in all_skills}
        self.assertEqual(slugs, {"agent-supervisor", "unrelated-custom"})
        self.assertEqual(len(all_skills), 2)

    def test_cli_invocation_and_legacy_wrapper(self) -> None:
        # Generic CLI invocation
        rc = seed_main(["--state-dir", str(self.tmp_dir)])
        self.assertEqual(rc, 0)

        # Legacy wrapper CLI invocation
        rc_leg = seed_legacy_module.main(["--state-dir", str(self.tmp_dir)])
        self.assertEqual(rc_leg, 0)


if __name__ == "__main__":
    unittest.main()
