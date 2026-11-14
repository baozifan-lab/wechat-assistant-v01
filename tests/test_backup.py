from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from zhishi.core.backup import backup_database, inspect_database
from zhishi.runtime import build_pilot


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'pilot.sqlite3'
        self.pilot = build_pilot(self.source, root=self.root, enable_wechat=False)
        self.destination = self.root / 'backups' / 'snapshot.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_includes_committed_wal_and_preserves_unknown_sending_states(self):
        self.pilot.chat('1 小时后提醒我备份验证', 'backup-request-001')
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("INSERT INTO ai_dispatch_notices(code,revision,role,kind,text,state) VALUES ('test',1,'manager','test','private','sending')")
            writer.commit()
            self.assertTrue(Path(str(self.source) + '-wal').exists())
            # An in-flight mutation must not become part of the committed snapshot.
            writer.execute("UPDATE tasks SET status='done'")
            result = backup_database(self.source, self.destination)
            self.assertTrue({'tasks', 'ai_dispatch_notices', 'invoice_demo_runs', 'invoice_demo_notices'} <= set(result['tables']))
            self.assertEqual(len(result['sha256']), 64)
            writer.rollback()
        with closing(sqlite3.connect(self.destination)) as restored:
            self.assertEqual(restored.execute('SELECT status FROM tasks').fetchone()[0], 'open')
            self.assertEqual(restored.execute('SELECT state FROM ai_dispatch_notices').fetchone()[0], 'sending')
        # Inspecting the backup must not run application startup/recovery.
        self.assertTrue(inspect_database(self.destination)['ok'])
        with closing(sqlite3.connect(self.destination)) as restored:
            self.assertEqual(restored.execute('SELECT state FROM ai_dispatch_notices').fetchone()[0], 'sending')

    def test_existing_target_and_source_are_never_overwritten(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'keep-this-file')
        for target in (self.source, self.destination):
            with self.assertRaises(FileExistsError):
                backup_database(self.source, target)
        self.assertEqual(self.destination.read_bytes(), b'keep-this-file')
        self.assertTrue(inspect_database(self.source)['ok'])

    def test_missing_source_is_not_created_and_failed_backup_is_not_published(self):
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):
            backup_database(missing, self.destination)
        self.assertFalse(missing.exists())
        with patch('zhishi.core.backup.inspect_database', side_effect=ValueError('failed validation')):
            with self.assertRaises(ValueError):
                backup_database(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_timeout_cleans_incomplete_snapshot(self):
        with patch('zhishi.core.backup.time.monotonic', side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                backup_database(self.source, self.destination, timeout=30)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_sqlite_sidecar_cannot_be_used_as_destination(self):
        with self.assertRaises(ValueError):
            backup_database(self.source, Path(str(self.source) + '-wal'))


if __name__ == '__main__':
    unittest.main()
