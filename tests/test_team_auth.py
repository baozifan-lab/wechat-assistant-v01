from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from zhishi.core.clock import now, stamp
from zhishi.team.errors import TeamError
from zhishi.team.security import password_hash, password_matches, digest
from tests.team_support import TeamCase, PASSWORD


class TeamAuthTests(TeamCase):
    def test_hashes_and_tokens_are_not_stored_in_plaintext(self):
        with self.app.store.connect() as db:
            stored = db.execute('SELECT password_hash FROM team_users WHERE id=?', (self.ids['bob'],)).fetchone()[0]
            tokens = [row[0] for row in db.execute('SELECT token_hash FROM team_sessions')]
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(password_matches(PASSWORD, stored))
        self.assertFalse(password_matches('a-wrong-password', stored))
        self.assertNotIn(self.tokens['bob'], tokens)
        self.assertIn(digest(self.tokens['bob']), tokens)

    def test_disabled_account_loses_old_session_and_cannot_login(self):
        self.app.auth.update_user(self.ids['admin'], self.ids['bob'], {'active': False})
        with self.assertRaises(TeamError) as error:
            self.app.auth.verify(self.tokens['bob'])
        self.assertEqual(error.exception.status, 401)
        with self.assertRaises(TeamError):
            self.app.auth.login('bob', PASSWORD)

    def test_password_change_revokes_every_existing_session(self):
        self.app.auth.change_password(self.ids['bob'], PASSWORD, 'New-password-12345')
        with self.assertRaises(TeamError):
            self.app.auth.verify(self.tokens['bob'])
        self.assertEqual(self.app.auth.login('bob', 'New-password-12345')['user']['id'], self.ids['bob'])

    def test_employee_cannot_reset_another_users_password_or_change_role(self):
        with self.assertRaises(TeamError):
            self.app.auth.change_password(self.ids['bob'], None, 'New-password-12345', target=self.ids['carol'])
        with self.assertRaises(TeamError):
            self.app.auth.update_user(self.ids['bob'], self.ids['bob'], {'role': 'admin'})

    def test_admin_reset_revokes_sessions(self):
        self.app.auth.change_password(self.ids['admin'], None, 'Reset-password-12345', target=self.ids['carol'])
        with self.assertRaises(TeamError):
            self.app.auth.verify(self.tokens['carol'])
        self.assertEqual(self.app.auth.login('carol', 'Reset-password-12345')['user']['id'], self.ids['carol'])

    def test_expired_sessions_logout_and_random_tokens_are_rejected(self):
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_sessions SET expires_at=? WHERE user_id=?', (stamp(now() - timedelta(seconds=1)), self.ids['bob']))
        for token in (self.tokens['bob'], 'nonexistent-token-long-enough'):
            with self.assertRaises(TeamError):
                self.app.auth.verify(token)
        self.app.auth.logout(self.tokens['carol'])
        with self.assertRaises(TeamError):
            self.app.auth.verify(self.tokens['carol'])

    def test_last_administrator_cannot_be_disabled_or_demoted(self):
        for data in ({'active': False}, {'role': 'employee'}):
            with self.assertRaises(TeamError):
                self.app.auth.update_user(self.ids['admin'], self.ids['admin'], data)

    def test_login_failures_persist_and_rate_limit(self):
        for _ in range(5):
            with self.assertRaises(TeamError) as error:
                self.app.auth.login('bob', 'wrong-password', 'test-ip')
            self.assertEqual(error.exception.status, 401)
        with self.assertRaises(TeamError) as error:
            self.app.auth.login('bob', PASSWORD, 'another-ip')
        self.assertEqual(error.exception.status, 429)

    def test_auth_connections_work_in_concurrent_http_threads(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.app.auth.verify, [self.tokens['bob']] * 20))
        self.assertEqual({r['id'] for r in results}, {self.ids['bob']})

    def test_audit_failure_rolls_back_user_creation(self):
        with patch.object(self.app.store, 'audit', side_effect=RuntimeError('fault injection')):
            with self.assertRaises(RuntimeError):
                self.app.auth.create_user(self.ids['admin'], {'username': 'rollback-user', 'password': PASSWORD, 'display_name': 'test'})
        with self.app.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM team_users WHERE username='rollback-user'").fetchone())

    def test_weak_password_and_duplicate_case_insensitive_name_are_rejected(self):
        with self.assertRaises(TeamError):
            password_hash('short')
        with self.assertRaises(TeamError) as error:
            self.app.auth.create_user(self.ids['admin'], {'username': 'BOB', 'password': PASSWORD, 'display_name': 'other'})
        self.assertEqual(error.exception.status, 409)

    def test_public_user_records_never_include_password_or_session(self):
        users = self.app.auth.users(self.ids['admin'])
        self.assertTrue(all('password_hash' not in user and 'token_hash' not in user for user in users))
        with self.assertRaises(TeamError):
            self.app.auth.users(self.ids['bob'])
