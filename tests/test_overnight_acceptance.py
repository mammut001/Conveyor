import tempfile
import unittest
from pathlib import Path
from overnight_store import OvernightStore


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'runs.sqlite3'
        self.store=OvernightStore(self.path)
        self.run=self.store.create('Make the goal observable',hours=1,max_tasks=12)
        self.store.enqueue(self.run,'discover','initial',{'revision':'a'*40})
        self.claim=self.store.claim(self.run,'discover')
        self.document={'objective':'Make the goal observable','revision':'a'*40,'checks':[{'name':'goal check','python':'assert False'}]}

    def test_plan_survives_restart_and_rejects_changes(self):
        digest=self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],self.document)
        recovered=OvernightStore(self.path).acceptance(self.run)
        self.assertEqual(recovered,{'digest':digest,'document':self.document})
        self.assertEqual(self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],self.document),digest)
        altered={**self.document,'checks':[{'name':'weakened','python':'assert True'}]}
        with self.assertRaisesRegex(ValueError,'cannot change'):
            self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],altered)
        self.assertEqual(self.store.acceptance(self.run),recovered)

    def test_cancelled_discovery_cannot_freeze_a_plan(self):
        self.store.cancel(self.run)
        with self.assertRaisesRegex(ValueError,'lease'):
            self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],self.document)
        self.assertIsNone(self.store.acceptance(self.run))

    def test_plan_must_match_objective_and_source_revision(self):
        for changed in ({'objective':'another task'},{'revision':'b'*40}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError,'target'):
                self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],{**self.document,**changed})
        self.assertIsNone(self.store.acceptance(self.run))

    def test_stale_lease_cannot_win_after_reclaim(self):
        with self.store.db() as db:
            db.execute('UPDATE nightly_tasks SET lease_until=0 WHERE id=?',(self.claim['id'],))
        replacement=self.store.claim(self.run,'discover')
        with self.assertRaisesRegex(ValueError,'lease'):
            self.store.freeze_acceptance(self.claim['id'],self.claim['lease'],self.document)
        self.store.freeze_acceptance(replacement['id'],replacement['lease'],self.document)
