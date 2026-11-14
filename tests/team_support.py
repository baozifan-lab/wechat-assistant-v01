import json
from pathlib import Path
import tempfile
import unittest
import uuid

from zhishi.core.backup import backup_database
from zhishi.team.application import TeamApplication

PASSWORD = 'Team-test-password-123'
BRIDGE_SECRET = 'unit-test-team-bridge-secret-1234567890'


class TeamCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_temp = tempfile.TemporaryDirectory()
        cls.base_path = Path(cls.base_temp.name) / 'base.sqlite3'
        app = TeamApplication(cls.base_path, bridge_secret=BRIDGE_SECRET)
        cls.users = {'admin': app.auth.bootstrap('admin', PASSWORD)}
        for name, role in [('alice', 'manager'), ('bob', 'employee'), ('carol', 'employee'), ('david', 'employee'), ('viewer', 'employee')]:
            cls.users[name] = app.auth.create_user(cls.users['admin']['id'], {'username': name, 'password': PASSWORD, 'display_name': name, 'role': role})
        cls.ids = {k: v['id'] for k, v in cls.users.items()}
        cls.project = app.service.create_project(cls.ids['alice'], {'name': '测试项目', 'request_id': str(uuid.uuid4())})
        for name in ('bob', 'carol', 'david', 'viewer'):
            app.service.set_member(cls.ids['alice'], cls.project['id'], cls.ids[name], {'role': 'viewer' if name == 'viewer' else 'member', 'request_id': str(uuid.uuid4())})
        cls.tokens = {name: app.auth.login(name, PASSWORD, 'fixture-' + name)['access_token'] for name in cls.users}

    @classmethod
    def tearDownClass(cls):
        cls.base_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'team.sqlite3'
        backup_database(self.base_path, self.db)
        self.app = TeamApplication(self.db, bridge_secret=BRIDGE_SECRET)
        self.s = self.app.service

    def tearDown(self):
        self.temp.cleanup()

    def data(self, **kwargs):
        return {'request_id': str(uuid.uuid4()), **kwargs}

    def task(self, **kwargs):
        values = {'project_id': self.project['id'], 'title': '核对资料', 'assignee_id': self.ids['bob'],
                  'reviewer_id': self.ids['carol'], **kwargs}
        return self.s.create_task(self.ids['alice'], self.data(**values))

    def action(self, user, task, action, **kwargs):
        return self.s.task_action(self.ids[user], task['id'], action, self.data(expected_version=task['version'], **kwargs))

    def upload(self, user, task, content=b'a,b\n1,2\n', name='result.csv', kind='output'):
        result = self.s.upload(self.ids[user], task['id'], self.data(expected_version=task['version'], name=name, kind=kind), content)
        return self.s.detail(self.ids[user], task['id']), result['artifact']

    def flow(self, **kwargs):
        values = {'project_id': self.project['id'], 'title': '整合与复核', 'prepare_id': self.ids['alice'],
                  'review_id': self.ids['bob'], 'approver_id': self.ids['carol'], **kwargs}
        return self.s.start_flow(self.ids['alice'], self.data(**values))

    def complete_stage(self, assignee, reviewer, task):
        task = self.action(assignee, task, 'accept')
        task, artifact = self.upload(assignee, task)
        task = self.action(assignee, task, 'submit')
        task = self.action(reviewer, task, 'approve')
        return task, artifact

    def bind(self, user='bob', peer='test-peer', *, push=True):
        data = self.data(push_enabled=push)
        code = self.app.wechat.begin_binding(self.ids[user], data)['binding_code']
        result = self.app.wechat.receive({'account_id': 'test-account', 'peer_id': peer,
            'source_message_id': str(uuid.uuid4()), 'is_group': False, 'text': '绑定 ' + code})
        self.assertIn('已验证', result['reply'])
        return code


class FakeSkillRunner:
    def __init__(self, service):
        self.service = service
        self.calls = 0

    def checksum(self, skill):
        return 'fixed-test-skill-version'

    def execute(self, job):
        self.calls += 1
        folder = self.service.artifact_root / '.jobs' / job['id'] / job['lease_token']
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'result.json'
        path.write_text(json.dumps({'passed': True}), encoding='utf-8')
        return {'passed': True}, [(path.name, path)]
