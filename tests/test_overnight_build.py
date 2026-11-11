import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from conveyor.automation.overnight_build import build_frontend


class BuildTests(unittest.TestCase):
    def test_strict_worker_umask_still_produces_readable_public_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);web=root/'web';web.mkdir()
            (web/'package-lock.json').write_text('{}')
            async def fake_command(argv,**kw):
                if argv[:2]==['npm','run']:
                    assets=web/'dist/assets';assets.mkdir(parents=True)
                    (web/'dist/index.html').write_text('<html>new UI</html>')
                    (assets/'app.js').write_text('new UI')
                return ''
            previous=os.umask(0o077)
            try:
                with patch('conveyor.automation.overnight_build.command',side_effect=fake_command):
                    build_frontend(root)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE((web/'dist').stat().st_mode),0o755)
            self.assertEqual(stat.S_IMODE((web/'dist/assets/app.js').stat().st_mode),0o644)
            self.assertEqual(stat.S_IMODE((root/'preview-build.json').stat().st_mode),0o644)
