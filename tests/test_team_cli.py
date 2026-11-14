import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from zhishi.config import ROOT
from zhishi.team.application import TeamApplication
from tests.team_support import PASSWORD


class TeamCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, script, *args, env=None):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / script), *map(str, args)],
                                cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_admin_initialization_and_verification_do_not_print_password(self):
        env = {**os.environ, 'TEAM_TEST_BOOTSTRAP_PASSWORD': PASSWORD}
        database = self.root / 'team.sqlite3'
        result = self.run_script('team_admin.py', 'init', '--database', database, '--password-env', 'TEAM_TEST_BOOTSTRAP_PASSWORD', env=env)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertTrue(json.loads(self.run_script('team_admin.py', 'verify', '--database', database).stdout)['ok'])

    def test_demo_creates_random_private_credentials_and_refuses_overwrite(self):
        folder = self.root / 'demo'
        result = self.run_script('create_team_demo.py', '--directory', folder)
        accounts = json.loads((folder / 'accounts.json').read_text('utf-8'))
        self.assertEqual(len(accounts['accounts']), 4)
        for account in accounts['accounts']:
            self.assertGreaterEqual(len(account['password']), 24)
            self.assertNotIn(account['password'], result.stdout)
        repeated = subprocess.run([sys.executable, str(ROOT / 'scripts/create_team_demo.py'), '--directory', str(folder)], cwd=ROOT, capture_output=True, timeout=10)
        self.assertNotEqual(repeated.returncode, 0)

    def test_full_backup_cli_round_trip(self):
        database = self.root / 'team.sqlite3'
        app = TeamApplication(database)
        app.auth.bootstrap('admin', PASSWORD)
        bundle = self.root / 'bundle'
        self.assertTrue(json.loads(self.run_script('team_backup.py', 'create', '--database', database, '--directory', bundle).stdout)['ok'])
        self.assertTrue(json.loads(self.run_script('team_backup.py', 'verify', '--directory', bundle).stdout)['ok'])
        restored = self.root / 'restored/team.sqlite3'
        result = self.run_script('team_backup.py', 'restore', '--directory', bundle, '--database', restored)
        self.assertTrue(json.loads(result.stdout)['notification_replay_disabled'])

    def test_source_package_contains_schema_and_tools_but_no_private_runtime_state(self):
        from scripts.package import source_files
        files = {str(p.relative_to(ROOT)).replace('\\', '/') for p in source_files()}
        self.assertIn('zhishi/team/schema.sql', files)
        self.assertIn('scripts/team_admin.py', files)
        self.assertIn('scripts/verify_team_backend.py', files)
        self.assertIn('wechat_employee2_connect.py', files)
        self.assertFalse(any(p.startswith(('data/', 'outputs/', '.runtime/')) for p in files))
