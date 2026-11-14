from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from zhishi.runtime import build_pilot
from zhishi.team.application import TeamApplication
from zhishi.team.errors import TeamError
from zhishi.team.migration import import_legacy, verify_team, LEGACY_TABLES
from zhishi.team.store import TeamStore
from tests.team_support import PASSWORD


class TeamMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_initialization_is_repeatable_and_foreign_keys_are_enabled(self):
        database = self.root / 'team.sqlite3'
        store = TeamStore(database)
        TeamStore(database)
        with store.connect(write=True) as db:
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_schema').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO team_sessions VALUES ('hash',9999,'future','now')")

    def test_schema_failure_leaves_no_partial_team_tables(self):
        database = self.root / 'bad.sqlite3'
        schema = Path('zhishi/team/schema.sql').read_text('utf-8')
        schema = schema.replace('CREATE TABLE team_projects', 'INVALID DDL team_projects')
        with patch('zhishi.team.store.Path.read_text', return_value=schema):
            with self.assertRaises(sqlite3.Error):
                TeamStore(database)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'team_%'").fetchone()[0], 0)

    def test_unknown_schema_version_is_rejected_without_repairing_in_place(self):
        database = self.root / 'future.sqlite3'
        store = TeamStore(database)
        with store.connect(write=True) as db:
            db.execute('UPDATE team_schema SET version=999')
        with self.assertRaises(TeamError):
            TeamStore(database)

    def test_verification_accepts_same_schema_with_windows_line_endings(self):
        from zhishi.team import migration
        database = self.root / 'portable.sqlite3'
        TeamStore(database)
        schema = self.root / 'schema.sql'
        original = Path('zhishi/team/schema.sql').read_text('utf-8')
        schema.write_bytes(original.replace('\n', '\r\n').encode())
        with patch('zhishi.team.migration.Path', side_effect=lambda value: schema if value == migration.__file__ else Path(value)):
            self.assertTrue(verify_team(database)['ok'])

    def test_legacy_copy_preserves_all_tables_states_and_explicit_user_mapping(self):
        source, destination = self.root / 'pilot.sqlite3', self.root / 'team.sqlite3'
        pilot = build_pilot(source, root=self.root, enable_wechat=False)
        pilot.chat('1 小时后提醒我历史导入验证', 'migration-request-001')
        with pilot.connect() as db:
            db.execute("INSERT INTO outbox(owner,task_id,version,text,state,created_at) VALUES ('pilot-owner',1,1,'sample','sending','2026-11-03T12:00:00+08:00')")
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        result = import_legacy(source, destination, 'admin', PASSWORD)
        self.assertEqual(result['counts']['preserved_tables'], 21)
        self.assertEqual(result['counts']['tasks'], 1)
        self.assertFalse(result['legacy_delivery_replayed'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        with closing(sqlite3.connect(destination)) as db:
            existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(set(LEGACY_TABLES) <= existing)
            self.assertEqual(db.execute('SELECT state FROM outbox').fetchone()[0], 'sending')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_tasks WHERE creator_id IS NULL OR assignee_id IS NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_notifications').fetchone()[0], 0)
            self.assertIsNone(db.execute('SELECT remind_at FROM team_tasks').fetchone()[0])
        app = TeamApplication(destination)
        admin = app.auth.login('admin', PASSWORD)['user']
        self.assertEqual(app.service.tasks(admin['id'])[0]['state'], 'open')
        self.assertTrue(verify_team(destination)['ok'])

    def test_existing_destination_and_source_are_never_overwritten(self):
        source = self.root / 'pilot.sqlite3'
        build_pilot(source, root=self.root, enable_wechat=False)
        with self.assertRaises(TeamError):
            import_legacy(source, source, 'admin', PASSWORD)
        destination = self.root / 'keep.sqlite3'
        destination.write_bytes(b'keep')
        with self.assertRaises(TeamError):
            import_legacy(source, destination, 'admin', PASSWORD)
        self.assertEqual(destination.read_bytes(), b'keep')

    def test_team_startup_refuses_unimported_legacy_database_without_modifying_it(self):
        source = self.root / 'legacy.sqlite3'
        build_pilot(source, root=self.root, enable_wechat=False)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertRaises(TeamError):
            TeamApplication(source)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(source)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='team_schema'").fetchone())

    def test_incomplete_import_cannot_be_started_or_verified_as_ready(self):
        source = self.root / 'incomplete.sqlite3'
        build_pilot(source, root=self.root, enable_wechat=False)
        TeamStore(source)
        with self.assertRaises(TeamError):
            TeamApplication(source)
        with self.assertRaises(TeamError):
            verify_team(source)
