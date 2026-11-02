"""Durable nightly run queue with fenced leases and cancellation."""
from __future__ import annotations
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class OvernightStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS nightly_runs(
              id TEXT PRIMARY KEY, objective TEXT NOT NULL, state TEXT NOT NULL,
              deadline REAL NOT NULL, max_tasks INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS nightly_tasks(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES nightly_runs(id),
              role TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
              state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              lease TEXT, lease_until REAL, result TEXT,
              UNIQUE(run_id,role,fingerprint));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, objective, *, hours=6, max_tasks=30):
        if not objective.strip() or not 0 < hours <= 12 or not 1 <= max_tasks <= 100:
            raise ValueError('invalid nightly run limits')
        ident = uuid.uuid4().hex
        now = time.time()
        with self.db() as db:
            db.execute('INSERT INTO nightly_runs VALUES(?,?,?,?,?,?)',
                       (ident, objective.strip(), 'running', now+hours*3600, max_tasks, now))
        return ident

    def enqueue(self, run_id, role, fingerprint, payload):
        if role not in {'discover', 'repair', 'verify'} or not fingerprint:
            raise ValueError('invalid task')
        with self.db() as db:
            run = db.execute('SELECT * FROM nightly_runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['state'] != 'running' or run['deadline'] <= time.time():
                raise ValueError('run is not active')
            existing = db.execute('SELECT id FROM nightly_tasks WHERE run_id=? AND role=? AND fingerprint=?',
                                  (run_id, role, fingerprint)).fetchone()
            if existing:
                return existing['id']
            count = db.execute('SELECT count(*) FROM nightly_tasks WHERE run_id=?', (run_id,)).fetchone()[0]
            if count >= run['max_tasks']:
                raise ValueError('nightly task budget exhausted')
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO nightly_tasks(id,run_id,role,fingerprint,payload,state) VALUES(?,?,?,?,?,?)',
                       (ident, run_id, role, fingerprint, json.dumps(payload), 'queued'))
            return ident

    def claim(self, run_id, role, *, lease_seconds=300, max_attempts=3):
        if not 0 < lease_seconds <= 3600 or not 1 <= max_attempts <= 10:
            raise ValueError('invalid lease limits')
        now = time.time()
        with self.db() as db:
            run = db.execute('SELECT * FROM nightly_runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['state'] != 'running' or run['deadline'] <= now:
                return None
            db.execute("UPDATE nightly_tasks SET state='blocked',lease=NULL WHERE run_id=? AND state='running' AND lease_until<=? AND attempts>=?",
                       (run_id, now, max_attempts))
            row = db.execute("SELECT * FROM nightly_tasks WHERE run_id=? AND role=? AND attempts<? AND (state='queued' OR (state='running' AND lease_until<=?)) ORDER BY rowid LIMIT 1",
                             (run_id, role, max_attempts, now)).fetchone()
            if not row:
                return None
            token = secrets.token_hex(24)
            expires = min(now+lease_seconds, run['deadline'])
            db.execute("UPDATE nightly_tasks SET state='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, expires, row['id']))
            return {**dict(row), 'lease':token, 'lease_until':expires, 'payload':json.loads(row['payload'])}

    def finish(self, task_id, lease, result, *, success=True):
        now = time.time()
        with self.db() as db:
            changed = db.execute("""UPDATE nightly_tasks SET state=?,result=?,lease=NULL
              WHERE id=? AND state='running' AND lease=? AND lease_until>?
              AND run_id IN(SELECT id FROM nightly_runs WHERE state='running' AND deadline>?)""",
              ('passed' if success else 'blocked', json.dumps(result), task_id, lease, now, now)).rowcount
            return changed == 1

    def cancel(self, run_id):
        with self.db() as db:
            db.execute("UPDATE nightly_runs SET state='cancelled' WHERE id=? AND state='running'", (run_id,))
            db.execute("UPDATE nightly_tasks SET state='cancelled',lease=NULL WHERE run_id=? AND state IN('queued','running')", (run_id,))

    def renew(self, task_id, lease, *, lease_seconds=300):
        if not 0 < lease_seconds <= 3600:
            raise ValueError('invalid lease limit')
        now = time.time()
        with self.db() as db:
            return db.execute("""UPDATE nightly_tasks SET lease_until=min(?,
              (SELECT deadline FROM nightly_runs WHERE id=nightly_tasks.run_id))
              WHERE id=? AND lease=? AND state='running' AND lease_until>?
              AND run_id IN(SELECT id FROM nightly_runs WHERE state='running' AND deadline>?)""",
              (now+lease_seconds, task_id, lease, now, now)).rowcount == 1

    def status(self, run_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM nightly_runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            tasks = db.execute('SELECT id,role,state,attempts,result FROM nightly_tasks WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
            return {**dict(row), 'tasks':[dict(task) for task in tasks]}

    def advance(self, task_id, lease, result, next_tasks):
        """Persist a stage result and successor tasks atomically under the lease."""
        now = time.time()
        with self.db() as db:
            row = db.execute("""SELECT t.run_id,r.max_tasks FROM nightly_tasks t
              JOIN nightly_runs r ON r.id=t.run_id WHERE t.id=? AND t.lease=?
              AND t.state='running' AND t.lease_until>? AND r.state='running'
              AND r.deadline>?""", (task_id,lease,now,now)).fetchone()
            if not row:
                return False
            for successor in next_tasks:
                role, fingerprint, payload = successor
                if role not in {'discover','repair','verify'} or not fingerprint:
                    raise ValueError('invalid successor task')
                exists = db.execute('SELECT 1 FROM nightly_tasks WHERE run_id=? AND role=? AND fingerprint=?',
                                    (row['run_id'],role,fingerprint)).fetchone()
                if exists:
                    continue
                count = db.execute('SELECT count(*) FROM nightly_tasks WHERE run_id=?', (row['run_id'],)).fetchone()[0]
                if count >= row['max_tasks']:
                    raise ValueError('nightly task budget exhausted')
                db.execute('INSERT INTO nightly_tasks(id,run_id,role,fingerprint,payload,state) VALUES(?,?,?,?,?,?)',
                           (uuid.uuid4().hex,row['run_id'],role,fingerprint,json.dumps(payload),'queued'))
            db.execute("UPDATE nightly_tasks SET state='passed',result=?,lease=NULL WHERE id=?",
                       (json.dumps(result),task_id))
            return True
