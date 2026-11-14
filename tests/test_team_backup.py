from pathlib import Path

from zhishi.team.application import TeamApplication
from zhishi.team.backup import create_snapshot, restore_snapshot, verify_snapshot
from zhishi.team.errors import TeamError
from tests.team_support import TeamCase


class TeamBackupTests(TeamCase):
    def test_full_snapshot_restores_files_but_does_not_replay_notifications_or_sessions(self):
        self.bind()
        task = self.action('bob', self.task(), 'accept')
        task, artifact = self.upload('bob', task)
        with self.app.store.connect(write=True) as db:
            self.s.notify(db, self.ids['bob'], 'snapshot notice', task)
        bundle = self.root / 'backup'
        create_snapshot(self.db, self.s.artifact_root, bundle)
        self.assertEqual(verify_snapshot(bundle)['artifact_count'], 1)
        restored_db, restored_files = self.root / 'restored.sqlite3', self.root / 'restored-files'
        result = restore_snapshot(bundle, restored_db, restored_files)
        self.assertTrue(result['notification_replay_disabled'])
        restored = TeamApplication(restored_db, restored_files)
        self.assertEqual(restored.service.download(self.ids['bob'], artifact['id'])[1], b'a,b\n1,2\n')
        with restored.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_sessions').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM team_notifications WHERE state IN ('pending','sending')").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_tasks WHERE remind_at IS NOT NULL').fetchone()[0], 0)

    def test_snapshot_verification_rejects_changed_files_and_database(self):
        task = self.action('bob', self.task(), 'accept')
        self.upload('bob', task)
        bundle = self.root / 'backup'
        create_snapshot(self.db, self.s.artifact_root, bundle)
        next((bundle / 'artifacts').iterdir()).write_bytes(b'changed')
        with self.assertRaises(TeamError):
            verify_snapshot(bundle)
        with (bundle / 'database.sqlite3').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(TeamError):
            verify_snapshot(bundle)

    def test_snapshot_and_restore_refuse_existing_targets(self):
        bundle = self.root / 'backup'
        create_snapshot(self.db, self.s.artifact_root, bundle)
        with self.assertRaises(TeamError):
            create_snapshot(self.db, self.s.artifact_root, bundle)
        with self.assertRaises(TeamError):
            restore_snapshot(bundle, self.db, self.s.artifact_root)

    def test_periodic_backup_runs_at_configured_interval(self):
        from zhishi.team.runtime import TeamWorkers
        workers = TeamWorkers(self.app, backup_minutes=60)
        workers.next_backup = 0
        workers.backup_tick()
        snapshots = list((self.root / 'backups').iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(verify_snapshot(snapshots[0])['ok'])
        self.assertIsNotNone(workers.backup_status['last_success'])
        workers.backup_tick()
        self.assertEqual(len(list((self.root / 'backups').iterdir())), 1)
