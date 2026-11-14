from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from zhishi.team.errors import TeamError
from tests.team_support import TeamCase


class TeamServiceTests(TeamCase):
    def test_creator_assignee_reviewer_see_task_but_other_member_does_not(self):
        task = self.task()
        for name in ('alice', 'bob', 'carol'):
            self.assertEqual(self.s.detail(self.ids[name], task['id'])['id'], task['id'])
        self.assertEqual(self.s.tasks(self.ids['david']), [])
        with self.assertRaises(TeamError):
            self.s.detail(self.ids['david'], task['id'])

    def test_only_assignee_accepts_and_only_reviewer_approves(self):
        task = self.task()
        with self.assertRaises(TeamError):
            self.action('carol', task, 'accept')
        task = self.action('bob', task, 'accept')
        task, _ = self.upload('bob', task)
        task = self.action('bob', task, 'submit')
        with self.assertRaises(TeamError):
            self.action('bob', task, 'approve')
        task = self.action('carol', task, 'approve')
        self.assertEqual(task['state'], 'completed')

    def test_missing_output_cannot_be_submitted_or_called_complete(self):
        task = self.action('bob', self.task(), 'accept')
        with self.assertRaises(TeamError):
            self.action('bob', task, 'submit')
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['state'], 'in_progress')

    def test_rejection_requires_a_new_round_of_outputs(self):
        task = self.action('bob', self.task(), 'accept')
        task, _ = self.upload('bob', task)
        task = self.action('bob', task, 'submit')
        task = self.action('carol', task, 'reject', reason='需要重新核对')
        self.assertEqual(task['result_round'], 2)
        with self.assertRaises(TeamError):
            self.action('bob', task, 'submit')
        task, _ = self.upload('bob', task, b'a,b\n3,4\n')
        self.assertEqual(self.action('bob', task, 'submit')['state'], 'awaiting_review')

    def test_duplicate_requests_are_scoped_to_users_and_reuse_is_checked(self):
        data = self.data(project_id=self.project['id'], title='same-request', requires_artifact=False)
        a = self.s.create_task(self.ids['alice'], data)
        self.assertEqual(a, self.s.create_task(self.ids['alice'], data))
        b = self.s.create_task(self.ids['bob'], data)
        self.assertNotEqual(a['id'], b['id'])
        with self.assertRaises(TeamError):
            self.s.create_task(self.ids['alice'], {**data, 'title': 'different'})

    def test_concurrent_replays_create_only_one_task_and_notice(self):
        data = self.data(project_id=self.project['id'], title='concurrent', assignee_id=self.ids['bob'])
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.s.create_task(self.ids['alice'], data), range(12)))
        self.assertEqual(len({r['id'] for r in results}), 1)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_tasks').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_notifications').fetchone()[0], 1)

    def test_concurrent_state_changes_with_same_version_have_one_winner(self):
        task = self.task()
        def accept(_):
            try:
                return self.action('bob', task, 'accept')['state']
            except TeamError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, range(2)))
        self.assertCountEqual(results, ['in_progress', 409])

    def test_assignment_changes_access_and_requires_explicit_project_management(self):
        task = self.task()
        with self.assertRaises(TeamError):
            self.action('bob', task, 'assign', assignee_id=self.ids['david'])
        task = self.action('alice', task, 'assign', assignee_id=self.ids['david'])
        self.assertEqual(task['assignee_id'], self.ids['david'])
        with self.assertRaises(TeamError):
            self.s.detail(self.ids['bob'], task['id'])

    def test_readonly_member_cannot_create_work_or_be_assigned(self):
        with self.assertRaises(TeamError):
            self.s.create_task(self.ids['viewer'], self.data(project_id=self.project['id'], title='denied'))
        with self.assertRaises(TeamError):
            self.s.create_task(self.ids['alice'], self.data(project_id=self.project['id'], title='denied', assignee_id=self.ids['viewer']))

    def test_artifact_download_is_authorized_and_tampering_blocks_approval(self):
        task = self.action('bob', self.task(), 'accept')
        task, artifact = self.upload('bob', task)
        self.assertEqual(self.s.download(self.ids['carol'], artifact['id'])[1], b'a,b\n1,2\n')
        with self.assertRaises(TeamError):
            self.s.download(self.ids['david'], artifact['id'])
        task = self.action('bob', task, 'submit')
        with self.app.store.connect() as db:
            key = db.execute('SELECT storage_key FROM team_artifacts WHERE id=?', (artifact['id'],)).fetchone()[0]
        (self.s.artifact_root / key).write_bytes(b'changed')
        with self.assertRaises(TeamError):
            self.action('carol', task, 'approve')
        with self.assertRaises(TeamError):
            self.s.download(self.ids['bob'], artifact['id'])

    def test_upload_rejects_paths_old_versions_and_waiting_review(self):
        task = self.action('bob', self.task(), 'accept')
        for name in ('../escape.csv', 'C:\\secret.csv', '.hidden.csv', 'script.exe'):
            with self.assertRaises(TeamError):
                self.upload('bob', task, name=name)
        current, _ = self.upload('bob', task)
        with self.assertRaises(TeamError):
            self.upload('bob', task)
        waiting = self.action('bob', current, 'submit')
        with self.assertRaises(TeamError):
            self.upload('bob', waiting)

    def test_upload_failure_rolls_back_version_and_removes_unregistered_file(self):
        task = self.action('bob', self.task(), 'accept')
        before = list(self.s.artifact_root.iterdir())
        with patch.object(self.app.store, 'audit', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.upload('bob', task)
        self.assertEqual(before, list(self.s.artifact_root.iterdir()))
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['version'], task['version'])

    def test_snooze_preserves_deadline(self):
        from zhishi.core.clock import now, stamp
        from datetime import timedelta
        task = self.task(due_at=stamp(now() + timedelta(hours=1)))
        changed = self.action('bob', task, 'snooze', minutes=10)
        self.assertEqual(changed['due_at'], task['due_at'])
        self.assertNotEqual(changed['remind_at'], task['remind_at'])

    def test_removing_active_assignee_or_flow_participant_is_blocked(self):
        self.task()
        with self.assertRaises(TeamError):
            self.s.set_member(self.ids['alice'], self.project['id'], self.ids['bob'], self.data(role='remove'))

    def test_notifications_and_private_messages_cannot_be_read_by_another_user(self):
        self.task()
        bob = self.s.notifications(self.ids['bob'])
        self.assertTrue(bob)
        self.assertEqual(self.s.notifications(self.ids['david']), [])
        with self.assertRaises(TeamError):
            self.s.read_notification(self.ids['david'], bob[0]['id'])
        self.app.post_message(self.ids['bob'], self.data(text='private note'))
        self.assertEqual(self.app.messages(self.ids['david']), [])
