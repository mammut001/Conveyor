import unittest
import os
import shutil
import tempfile
import subprocess
from conveyor.agents.agent_supervisor import build_reading_guide, build_reviewer_prompt

class TestReadingGuide(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.tmp_dir, "repo")
        os.makedirs(self.repo_dir)
        subprocess.run(["git", "init"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.repo_dir, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_guide_features(self):
        # Create a large file (25KB) and a small file (1KB)
        large_file = os.path.join(self.repo_dir, "large.txt")
        with open(large_file, "wb") as f:
            f.write(b"a" * 26 * 1024)
        
        small_file = os.path.join(self.repo_dir, "small.txt")
        with open(small_file, "wb") as f:
            f.write(b"b" * 1024)
            
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        
        self.assertIn("large.txt  26 KB LARGE", guide)
        self.assertIn("small.txt  1 KB", guide)
        self.assertIn("Reading rules:", guide)
        self.assertIn("Files over 20 KB (marked LARGE)", guide)

    def test_reviewer_role(self):
        guide_analyst = build_reading_guide(self.repo_dir, role="analyst")
        guide_reviewer = build_reading_guide(self.repo_dir, role="reviewer")
        
        self.assertNotIn("The full candidate diff is included below", guide_analyst)
        self.assertIn("The full candidate diff is included below", guide_reviewer)

    def test_non_git_dir(self):
        non_git = os.path.join(self.tmp_dir, "not_a_repo")
        os.makedirs(non_git)
        guide = build_reading_guide(non_git, role="analyst")
        self.assertEqual(guide, "")

    def test_cap_120(self):
        for i in range(130):
            with open(os.path.join(self.repo_dir, f"file{i}.txt"), "w") as f:
                f.write("a")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        
        self.assertIn("... and 10 more files", guide)

    def test_reviewer_prompt_guide(self):
        # Test guide in reviewer prompt
        prompt = build_reviewer_prompt(
            goal="test", worktree_path=self.repo_dir, base_sha="a"*40, current_sha="b"*40,
            fingerprint="fp", analysis_plan="plan", implementer_summary="summary",
            acceptance_criteria="criteria"
        )
        self.assertIn("Repository files (tracked, size):", prompt)
        
        # Test guide absent when CONVEYOR_READING_GUIDE=0
        os.environ["CONVEYOR_READING_GUIDE"] = "0"
        try:
            prompt_no_guide = build_reviewer_prompt(
                goal="test", worktree_path=self.repo_dir, base_sha="a"*40, current_sha="b"*40,
                fingerprint="fp", analysis_plan="plan", implementer_summary="summary",
                acceptance_criteria="criteria"
            )
            self.assertNotIn("Repository files (tracked, size):", prompt_no_guide)
        finally:
            del os.environ["CONVEYOR_READING_GUIDE"]

if __name__ == "__main__":
    unittest.main()
