"""Keep service launchers stable and implementation modules out of the root."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS_AND_CONFIG = {
    'bot.py', 'feishu_bot.py', 'web_console.py', 'web_console_takeover.py',
    'desktop_agent.py', 'desktop_agent_server.py', 'desktop_chat_window.py',
    'agent_desktops.py', 'handoff_sidecar.py', 'runner.py',
    'config.py', 'redaction.py', 'logging_setup.py',
}

class RepositoryLayoutTests(unittest.TestCase):
    def test_root_contains_only_established_launchers_and_config(self):
        self.assertEqual({p.name for p in ROOT.glob('*.py')}, LAUNCHERS_AND_CONFIG)

    def test_runtime_imports_do_not_load_removed_root_modules(self):
        moved = {p.stem for p in (ROOT / 'conveyor').glob('*/*.py') if p.stem != '__init__'}
        violations = []
        for directory in ('conveyor', 'agent_coordination', 'channel', 'handlers', 'runner', 'personal_tools', 'nodes'):
            for path in (ROOT / directory).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                    if isinstance(node, ast.Import):
                        names = [alias.name.split('.')[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        names = [(node.module or '').split('.')[0]]
                    else:
                        continue
                    violations.extend(f'{path.relative_to(ROOT)}:{node.lineno}: {name}' for name in names if name in moved)
        self.assertEqual(violations, [])
