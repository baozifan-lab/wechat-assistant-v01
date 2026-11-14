from datetime import timedelta
from unittest.mock import patch

from zhishi.core.clock import now, stamp
from zhishi.team.errors import TeamError
from zhishi.team.jobs import JobService
from tests.team_support import TeamCase, FakeSkillRunner


class TeamJobTests(TeamCase):
    def setUp(self):
        super().setUp()
        self.runner = FakeSkillRunner(self.s)
        self.app.jobs = JobService(self.s, self.runner)

    def queue(self):
        task = self.action('bob', self.task(), 'accept')
        task, artifact = self.upload('bob', task, kind='input')
        job = self.app.jobs.enqueue(self.ids['bob'], task['id'], self.data(expected_version=task['version'], skill='csv.inspect.v1', artifact_ids=[artifact['id']]))
        return task, artifact, job

    def test_registered_job_publishes_artifact_but_does_not_approve_task(self):
        task, _, _ = self.queue()
        self.app.jobs.tick()
        detail = self.s.detail(self.ids['bob'], task['id'])
        self.assertEqual(detail['state'], 'in_progress')
        self.assertEqual(detail['jobs'][0]['state'], 'completed')
        self.assertEqual(len([a for a in detail['artifacts'] if a['kind'] == 'output']), 1)
        self.assertEqual(self.runner.calls, 1)

    def test_multiple_workers_cannot_claim_same_job(self):
        self.queue()
        first = self.app.jobs.claim()
        self.assertIsNotNone(first)
        self.assertIsNone(self.app.jobs.claim())

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_publish(self):
        task, _, job = self.queue()
        old = self.app.jobs.claim()
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_jobs SET lease_until=? WHERE id=?', (stamp(now() - timedelta(seconds=1)), job['id']))
        new = self.app.jobs.claim()
        self.assertNotEqual(old['lease_token'], new['lease_token'])
        self.assertFalse(self.app.jobs.renew(old))
        self.assertFalse(self.app.jobs.finish(old, result=self.runner.execute(old)))
        self.assertTrue(self.app.jobs.finish(new, result=self.runner.execute(new)))
        detail = self.s.detail(self.ids['bob'], task['id'])
        self.assertEqual(len([a for a in detail['artifacts'] if a['kind'] == 'output']), 1)

    def test_result_is_not_published_after_task_reassignment(self):
        task, _, _ = self.queue()
        job = self.app.jobs.claim()
        self.action('alice', task, 'assign', assignee_id=self.ids['david'])
        self.assertFalse(self.app.jobs.finish(job, result=self.runner.execute(job)))
        detail = self.s.detail(self.ids['david'], task['id'])
        self.assertEqual(detail['jobs'][0]['state'], 'stale')
        self.assertFalse(any(a['kind'] == 'output' for a in detail['artifacts']))

    def test_disabled_user_cannot_publish_running_job(self):
        task, _, _ = self.queue()
        job = self.app.jobs.claim()
        self.app.auth.update_user(self.ids['admin'], self.ids['bob'], {'active': False})
        self.assertFalse(self.app.jobs.finish(job, result=self.runner.execute(job)))

    def test_unregistered_skill_and_unrelated_artifact_are_rejected(self):
        task, artifact, _ = self.queue()
        for skill in ('shell.exec', '../../any-script'):
            with self.assertRaises(TeamError):
                self.app.jobs.enqueue(self.ids['bob'], task['id'], self.data(expected_version=task['version'], skill=skill, artifact_ids=[artifact['id']]))
        other = self.action('bob', self.task(), 'accept')
        with self.assertRaises(TeamError):
            self.app.jobs.enqueue(self.ids['bob'], other['id'], self.data(expected_version=other['version'], skill='csv.inspect.v1', artifact_ids=[artifact['id']]))

    def test_job_exception_is_recorded_without_advancing_task(self):
        task, _, _ = self.queue()
        with patch.object(self.runner, 'execute', side_effect=RuntimeError('private diagnostic')):
            with self.assertLogs('zhishi.team.jobs', level='ERROR') as logs:
                self.app.jobs.tick()
        self.assertNotIn('private diagnostic', '\n'.join(logs.output))
        detail = self.s.detail(self.ids['bob'], task['id'])
        self.assertEqual(detail['state'], 'in_progress')
        self.assertEqual(detail['jobs'][0]['state'], 'failed')

    def test_expired_jobs_stop_after_three_attempts(self):
        _, _, queued = self.queue()
        job = self.app.jobs.claim()
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_jobs SET lease_until=?,attempts=3 WHERE id=?', (stamp(now() - timedelta(seconds=1)), queued['id']))
        self.assertIsNone(self.app.jobs.claim())
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM team_jobs WHERE id=?', (job['id'],)).fetchone()[0], 'failed')

    def test_real_csv_skill_runs_without_model_or_external_tools(self):
        from zhishi.team.jobs import SkillRunner
        task, artifact, _ = self.queue()
        # Replace the queued fake processor snapshot with a real one through a fresh task.
        other = self.action('bob', self.task(), 'accept')
        other, item = self.upload('bob', other, kind='input')
        self.app.jobs = JobService(self.s, SkillRunner(self.s))
        self.app.jobs.enqueue(self.ids['bob'], other['id'], self.data(expected_version=other['version'], skill='csv.inspect.v1', artifact_ids=[item['id']]))
        with self.app.store.connect(write=True) as db:
            db.execute("UPDATE team_jobs SET state='cancelled' WHERE task_id=?", (task['id'],))
        self.app.jobs.tick()
        detail = self.s.detail(self.ids['bob'], other['id'])
        self.assertEqual(detail['jobs'][0]['state'], 'completed')
        self.assertIn('"passed": true', detail['jobs'][0]['result_json'])

    def test_changed_input_is_rejected_before_result_publication(self):
        task, artifact, _ = self.queue()
        job = self.app.jobs.claim()
        result = self.runner.execute(job)
        with self.app.store.connect() as db:
            key = db.execute('SELECT storage_key FROM team_artifacts WHERE id=?', (artifact['id'],)).fetchone()[0]
        (self.s.artifact_root / key).write_bytes(b'changed after execution')
        with self.assertRaises(TeamError):
            self.app.jobs.finish(job, result=result)
        self.assertFalse(any(a['kind'] == 'output' for a in self.s.detail(self.ids['bob'], task['id'])['artifacts']))

    def test_xlsx_archive_path_traversal_is_rejected_before_node_execution(self):
        import io
        import zipfile
        from zhishi.team.jobs import SkillRunner
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../outside', 'test')
        task = self.action('bob', self.task(), 'accept')
        task, artifact = self.upload('bob', task, content=stream.getvalue(), name='unsafe.xlsx', kind='input')
        self.app.jobs = JobService(self.s, SkillRunner(self.s))
        self.app.jobs.enqueue(self.ids['bob'], task['id'], self.data(expected_version=task['version'], skill='excel.merge.v1', artifact_ids=[artifact['id']]))
        with patch('zhishi.team.jobs.subprocess.run') as execute:
            with self.assertLogs('zhishi.team.jobs', level='ERROR'):
                self.app.jobs.tick()
            execute.assert_not_called()
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['jobs'][0]['state'], 'failed')
