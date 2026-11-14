from zhishi.team.errors import TeamError
from tests.team_support import TeamCase


class TeamFlowTests(TeamCase):
    def to_final(self):
        flow = self.flow()
        first, artifact = self.complete_stage('alice', 'bob', flow['task'])
        flow = self.s.flow_detail(self.ids['alice'], flow['id'])
        second, _ = self.complete_stage('bob', 'carol', flow['tasks'][1])
        return self.s.flow_detail(self.ids['carol'], flow['id']), artifact

    def test_three_person_handoff_pins_input_and_requires_final_human_approval(self):
        flow = self.flow()
        self.assertEqual(len(self.s.flow_detail(self.ids['bob'], flow['id'])['tasks']), 1)
        first, artifact = self.complete_stage('alice', 'bob', flow['task'])
        flow = self.s.flow_detail(self.ids['bob'], flow['id'])
        self.assertEqual(flow['state'], 'reviewing')
        second = flow['tasks'][1]
        self.assertEqual(second['assignee_id'], self.ids['bob'])
        self.assertEqual(self.s.detail(self.ids['bob'], second['id'])['inputs'][0]['sha256'], artifact['sha256'])
        self.complete_stage('bob', 'carol', second)
        flow = self.s.flow_detail(self.ids['carol'], flow['id'])
        self.assertEqual(flow['state'], 'awaiting_approval')
        with self.assertRaises(TeamError):
            self.s.flow_action(self.ids['alice'], flow['id'], 'approve', self.data(expected_version=flow['version']))
        result = self.s.flow_action(self.ids['carol'], flow['id'], 'approve', self.data(expected_version=flow['version']))
        self.assertEqual(result['state'], 'completed')

    def test_upstream_not_approved_does_not_create_downstream_task(self):
        flow = self.flow()
        task = self.action('alice', flow['task'], 'accept')
        task, _ = self.upload('alice', task)
        self.action('alice', task, 'submit')
        current = self.s.flow_detail(self.ids['alice'], flow['id'])
        self.assertEqual(current['state'], 'preparing')
        self.assertEqual(len(current['tasks']), 1)

    def test_repeated_approval_does_not_create_duplicate_handoff(self):
        flow = self.flow()
        task = self.action('alice', flow['task'], 'accept')
        task, _ = self.upload('alice', task)
        task = self.action('alice', task, 'submit')
        payload = self.data(expected_version=task['version'])
        first = self.s.task_action(self.ids['bob'], task['id'], 'approve', payload)
        self.assertEqual(first, self.s.task_action(self.ids['bob'], task['id'], 'approve', payload))
        self.assertEqual(len(self.s.flow_detail(self.ids['alice'], flow['id'])['tasks']), 2)

    def test_two_people_can_explicitly_share_nonadjacent_final_role(self):
        with self.assertRaises(TeamError):
            self.flow(approver_id=self.ids['alice'])
        flow = self.flow(approver_id=self.ids['alice'], allow_shared_roles=True)
        self.assertEqual(flow['state'], 'preparing')
        with self.assertRaises(TeamError):
            self.flow(review_id=self.ids['alice'], allow_shared_roles=True)

    def test_unrelated_project_member_cannot_read_flow(self):
        flow = self.flow()
        with self.assertRaises(TeamError):
            self.s.flow_detail(self.ids['david'], flow['id'])
        self.assertEqual(self.app.flows(self.ids['david']), [])

    def test_final_rejection_reopens_only_second_stage_and_requires_new_outputs(self):
        self.bind()
        flow, _ = self.to_final()
        changed = self.s.flow_action(self.ids['carol'], flow['id'], 'reject', self.data(expected_version=flow['version'], reason='补充复核依据'))
        self.assertEqual(changed['state'], 'reviewing')
        tasks = self.s.flow_detail(self.ids['bob'], flow['id'])['tasks']
        self.assertEqual(tasks[0]['state'], 'completed')
        self.assertEqual(tasks[1]['state'], 'in_progress')
        notices = self.s.notifications(self.ids['bob'])
        rework = next(n for n in notices if '最终审批退回' in n['text'])
        self.assertEqual(rework['state'], 'pending')
        self.assertEqual(rework['task_version'], tasks[1]['version'])
        with self.assertRaises(TeamError):
            self.action('bob', tasks[1], 'submit')

    def test_changed_input_blocks_final_approval(self):
        flow, artifact = self.to_final()
        with self.app.store.connect() as db:
            key = db.execute('SELECT storage_key FROM team_artifacts WHERE id=?', (artifact['id'],)).fetchone()[0]
        (self.s.artifact_root / key).write_bytes(b'tampered')
        with self.assertRaises(TeamError):
            self.s.flow_action(self.ids['carol'], flow['id'], 'approve', self.data(expected_version=flow['version']))
        self.assertEqual(self.s.flow_detail(self.ids['carol'], flow['id'])['state'], 'awaiting_approval')

    def test_completed_upstream_cannot_be_silently_replaced(self):
        flow = self.flow()
        first, _ = self.complete_stage('alice', 'bob', flow['task'])
        with self.assertRaises(TeamError):
            self.upload('alice', first)
        with self.assertRaises(TeamError):
            self.action('alice', first, 'assign', assignee_id=self.ids['carol'])

    def test_flow_cancel_preserves_history_and_cancels_open_work(self):
        flow = self.flow()
        self.s.flow_action(self.ids['alice'], flow['id'], 'cancel', self.data(expected_version=flow['version']))
        result = self.s.flow_detail(self.ids['alice'], flow['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['tasks'][0]['state'], 'cancelled')
        with self.assertRaises(TeamError):
            self.action('alice', result['tasks'][0], 'accept')
