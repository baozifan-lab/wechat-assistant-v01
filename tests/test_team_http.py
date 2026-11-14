import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

from zhishi.team.errors import TeamError
from zhishi.team.runtime import create_team_server, TeamWorkers
from tests.team_support import TeamCase, PASSWORD, BRIDGE_SECRET


class TeamHTTPTests(TeamCase):
    def setUp(self):
        super().setUp()
        self.server = create_team_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def request(self, path, data=None, user='alice', headers=None, raw=None):
        outgoing = {'Content-Type': 'application/json'}
        if user:
            outgoing['Authorization'] = 'Bearer ' + self.tokens[user]
        outgoing.update(headers or {})
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers=outgoing)
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.code, dict(response.headers), json.loads(content) if 'application/json' in response.headers.get('Content-Type', '') else content

    def test_unauthenticated_requests_and_legacy_endpoints_are_not_exposed(self):
        for path in ('/api/team/tasks', '/api/team/users', '/api/state', '/api/console'):
            self.assertEqual(self.request(path, user=None)[0], 401)
        for path in ('/api/state', '/api/console', '/wechat-login.png'):
            self.assertEqual(self.request(path)[0], 404)
        status, _, root = self.request('/', user=None)
        self.assertEqual(status, 200)
        self.assertFalse(root['ui'])

    def test_login_cookie_requires_csrf_for_mutations_and_logout_revokes_it(self):
        status, headers, result = self.request('/api/team/auth/login', {'username': 'bob', 'password': PASSWORD}, user=None)
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('password', json.dumps(result))
        status, _, _ = self.request('/api/team/messages', self.data(text='test'), user=None, headers={'Cookie': cookie})
        self.assertEqual(status, 403)
        status, _, _ = self.request('/api/team/messages', self.data(text='test'), user=None,
                                   headers={'Cookie': cookie, 'X-CSRF-Token': result['csrf_token']})
        self.assertEqual(status, 201)
        status, _, _ = self.request('/api/team/auth/logout', {}, user=None,
                                   headers={'Cookie': cookie, 'X-CSRF-Token': result['csrf_token']})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/api/team/me', user=None, headers={'Cookie': cookie})[0], 401)

    def test_bad_host_origin_and_nonfinite_json_are_rejected(self):
        self.assertEqual(self.request('/api/team/me', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/team/messages', self.data(text='test'), headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/team/tasks', raw=b'{"due_at": NaN}')[0], 400)
        self.assertEqual(self.request('/api/team/tasks', {'project_id': [], 'title': 'bad'})[0], 400)

    def test_mass_assignment_cannot_escalate_identity_or_account_privileges(self):
        status, _, body = self.request('/api/team/users', {'username': 'new-admin', 'password': PASSWORD, 'display_name': 'admin', 'role': 'admin'}, user='bob')
        self.assertEqual(status, 403)
        status, _, task = self.request('/api/team/tasks', self.data(project_id=self.project['id'], title='own', creator_id=self.ids['admin']), user='bob')
        self.assertEqual(status, 201)
        self.assertEqual(task['creator_id'], self.ids['bob'])
        self.assertEqual(self.request('/api/team/audit', user='bob')[0], 403)

    def test_artifact_download_checks_identity_instead_of_guessable_url(self):
        task = self.action('bob', self.task(), 'accept')
        task, artifact = self.upload('bob', task)
        path = '/api/team/artifacts/' + artifact['id']
        self.assertEqual(self.request(path, user='david')[0], 403)
        self.assertEqual(self.request(path, user=None)[0], 401)
        status, headers, content = self.request(path, user='carol')
        self.assertEqual(status, 200)
        self.assertEqual(content, b'a,b\n1,2\n')
        self.assertIn('attachment', headers['Content-Disposition'])

    def test_concurrent_http_reads_use_independent_database_connections(self):
        self.task()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request('/api/team/tasks', user='bob')[0], range(20)))
        self.assertEqual(set(results), {200})

    def test_api_errors_have_server_request_id_and_safe_payload(self):
        from unittest.mock import patch
        with patch.object(self.app.service, 'tasks', side_effect=RuntimeError('private-key and customer text')):
            with self.assertLogs('zhishi.http', level='ERROR') as logs:
                status, headers, body = self.request('/api/team/tasks')
        self.assertEqual(status, 500)
        self.assertEqual(headers['X-Request-ID'], body['request_id'])
        self.assertNotIn('private-key', json.dumps(body) + '\n'.join(logs.output))

    def test_signed_bridge_binding_and_command_use_verified_identity(self):
        status, _, challenge = self.request('/api/team/wechat/binding-code', self.data(), user='bob')
        self.assertEqual(status, 200)
        payload = {'account_id': 'http-account', 'peer_id': 'http-peer', 'is_group': False,
                   'source_message_id': 'http-bind-1', 'text': '绑定 ' + challenge['binding_code']}
        raw, timestamp = json.dumps(payload).encode(), str(int(time.time()))
        signature = hmac.new(BRIDGE_SECRET.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        status, _, result = self.request('/api/team/wechat/inbound', raw=raw, user=None,
            headers={'X-Team-Bridge-Time': timestamp, 'X-Team-Bridge-Signature': signature})
        self.assertEqual(status, 200)
        self.assertIn('已验证', result['reply'])
        self.assertEqual(self.request('/api/team/wechat/inbound', raw=raw, user=None)[0], 403)

    def test_http_three_user_workflow_completes_with_authorized_files(self):
        status, _, flow = self.request('/api/team/flows', self.data(project_id=self.project['id'], title='HTTP闭环',
            prepare_id=self.ids['alice'], review_id=self.ids['bob'], approver_id=self.ids['carol']))
        self.assertEqual(status, 201)
        task = flow['task']
        for index, (assignee, reviewer) in enumerate((('alice', 'bob'), ('bob', 'carol'))):
            def action(action_name, current, user):
                status, _, result = self.request(f'/api/team/tasks/{current["id"]}/{action_name}',
                    self.data(expected_version=current['version']), user=user)
                self.assertEqual(status, 200, result)
                return result
            task = action('accept', task, assignee)
            status, _, artifact = self.request(f'/api/team/tasks/{task["id"]}/artifacts',
                self.data(expected_version=task['version'], name='report.csv', kind='output', content_base64=base64.b64encode(b'a,b\n1,2\n').decode()), user=assignee)
            self.assertEqual(status, 201)
            task['version'] = artifact['task_version']
            task = action('submit', task, assignee)
            action('approve', task, reviewer)
            _, _, current = self.request('/api/team/flows/' + flow['id'], user='carol')
            if index == 0:
                task = current['tasks'][1]
        status, _, result = self.request('/api/team/flows/' + flow['id'] + '/approve', self.data(expected_version=current['version']), user='carol')
        self.assertEqual(status, 200)
        self.assertEqual(result['state'], 'completed')

    def test_non_loopback_binding_requires_tls_and_explicit_hosts(self):
        with self.assertRaises(TeamError):
            create_team_server(self.app, 0, host='0.0.0.0')
        with self.assertRaises(TeamError):
            create_team_server(self.app, 0, host='0.0.0.0', tls_cert='missing', tls_key='missing')

    def test_workers_report_readiness_and_shutdown_without_resetting_data(self):
        workers = TeamWorkers(self.app, 2)
        try:
            workers.start()
            self.assertEqual(len(workers.threads), 4)
            with self.assertRaises(TeamError):
                workers.start()
        finally:
            workers.close()
        self.assertEqual(workers.snapshot()['status'], 'stopped')
        self.assertTrue(all(not t.is_alive() for t in workers.threads))
