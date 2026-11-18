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

    def test_no_outline_header_without_large_python_files(self):
        with open(os.path.join(self.repo_dir, "small.py"), "w") as f:
            f.write("def tiny():\n    return 1\n")
        with open(os.path.join(self.repo_dir, "big.txt"), "wb") as f:
            f.write(b"x" * 30 * 1024)
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        guide = build_reading_guide(self.repo_dir, role="analyst")
        self.assertIn("big.txt", guide)
        self.assertNotIn("Outline", guide)

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

    def test_python_outline(self):
        # Create a large Python file
        large_py = os.path.join(self.repo_dir, "large.py")
        with open(large_py, "w") as f:
            f.write("a" * 21 * 1024 + "\n") # Line 1
            f.write("class MyClass:\n")     # Line 2
            f.write("    def method1(self):\n") # Line 3
            f.write("        pass\n")           # Line 4
            f.write("    def method2(self):\n") # Line 5
            f.write("        pass\n")           # Line 6
            f.write("    def method3(self):\n") # Line 7
            f.write("        pass\n")           # Line 8
            f.write("\n")                       # Line 9
            f.write("def my_func():\n")         # Line 10
            f.write("    pass\n")               # Line 11
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        
        self.assertIn("large.py outline:", guide)
        self.assertIn("L2 class MyClass", guide)
        self.assertIn("L3 method1", guide)
        self.assertIn("L5 method2", guide)
        self.assertIn("L7 method3", guide)
        self.assertIn("L10 def my_func", guide)
        
        # New reading rule
        self.assertIn("- Use the outline line numbers with read offset/limit instead of paging through a file.", guide)

    def test_non_python_large_file(self):
        large_txt = os.path.join(self.repo_dir, "large.txt")
        with open(large_txt, "wb") as f:
            f.write(b"a" * 21 * 1024)
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        self.assertIn("large.txt  21 KB LARGE", guide)
        self.assertNotIn("large.txt outline:", guide)

    def test_syntax_error_python(self):
        large_py = os.path.join(self.repo_dir, "broken.py")
        with open(large_py, "w") as f:
            f.write("a" * 21 * 1024 + "\n")
            f.write("class MyClass:") # Incomplete
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        self.assertIn("broken.py  21 KB LARGE", guide)
        # Guide should still be returned, no outline for this file
        self.assertNotIn("broken.py outline:", guide)
        self.assertIn("Reading rules:", guide)

    def test_per_file_cap(self):
        large_py = os.path.join(self.repo_dir, "large.py")
        with open(large_py, "w") as f:
            f.write("a" * 21 * 1024 + "\n")
            # Create more than 250 entries
            for i in range(300):
                f.write(f"def func{i}(): pass\n")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True)
        
        guide = build_reading_guide(self.repo_dir, role="analyst")
        self.assertIn("large.py outline:", guide)
        self.assertIn("… (outline truncated; grep for more)", guide)

if __name__ == "__main__":
    unittest.main()
