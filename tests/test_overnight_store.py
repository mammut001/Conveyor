import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from overnight_store import OvernightStore


class OvernightStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'nightly.db'
        self.store = OvernightStore(self.path)
        self.run = self.store.create('Check Conveyor chat', max_tasks=2)

    def test_dedup_claim_restart_and_cancel(self):
        task = self.store.enqueue(self.run, 'discover', 'chat', {'url':'preview'})
        self.assertEqual(task, self.store.enqueue(self.run, 'discover', 'chat', {}))
        worker = self.store.claim(self.run, 'discover')
        self.assertIsNone(OvernightStore(self.path).claim(self.run, 'discover'))
        self.store.cancel(self.run)
        self.assertFalse(self.store.finish(task, worker['lease'], {}))
        self.assertIsNone(self.store.claim(self.run, 'discover'))

    def test_expired_worker_cannot_finish_reclaimed_task(self):
        task = self.store.enqueue(self.run, 'repair', 'issue-1', {})
        worker = self.store.claim(self.run, 'repair', lease_seconds=1)
        with patch('overnight_store.time.time', return_value=worker['lease_until']+1):
            successor = OvernightStore(self.path).claim(self.run, 'repair')
            self.assertIsNotNone(successor)
            self.assertFalse(self.store.finish(task, worker['lease'], {}))
            self.assertTrue(self.store.finish(task, successor['lease'], {'commit':'verified'}))

    def test_task_budget_and_deadline(self):
        self.store.enqueue(self.run, 'discover', 'a', {})
        self.store.enqueue(self.run, 'discover', 'b', {})
        with self.assertRaises(ValueError):
            self.store.enqueue(self.run, 'discover', 'c', {})
        with patch('overnight_store.time.time', return_value=10**12):
            self.assertIsNone(self.store.claim(self.run, 'discover'))
