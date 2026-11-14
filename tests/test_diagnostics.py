import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from zhishi.core.observability import SafeJsonFormatter
from zhishi.runtime import BackgroundWorkers, build_pilot, create_server


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pilot = build_pilot(self.root / 'private.sqlite3', root=self.root, enable_wechat=False)
        self.server = create_server(self.pilot, 0)
        self.token = self.server.RequestHandlerClass.token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.pilot.background_workers = SimpleNamespace(snapshot=lambda: {'status': 'ok', 'workers': {}})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, data=None, authorized=True):
        headers = {'X-Pilot-Token': self.token} if authorized else {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, headers=headers,
                                         data=None if data is None else json.dumps(data).encode())
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, dict(response.headers), json.load(response)

    def test_health_is_authenticated_read_only_and_does_not_probe_external_services(self):
        channel = Mock()
        self.pilot.transport = channel
        with self.pilot.connect() as db:
            db.execute("INSERT INTO ai_dispatch_notices(code,revision,role,kind,text,state) VALUES ('test',1,'manager','test','private','unknown')")
        status, _, body = self.request('/api/health', authorized=False)
        self.assertEqual(status, 404)
        self.assertNotIn('queues', body)
        status, _, body = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(body['database']['ok'])
        self.assertEqual(body['queues']['ai']['unknown'], 1)
        self.assertEqual(body['attention_count'], 1)
        channel.assert_not_called()
        self.assertEqual(channel.mock_calls, [])
        self.assertNotIn('private', json.dumps(body))
        self.assertNotIn(self.token, json.dumps(body))
        with self.pilot.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM ai_dispatch_notices').fetchone()[0], 'unknown')

    def test_health_reports_database_failure_without_creating_file_or_exposing_path(self):
        self.pilot.database = str(self.root / 'missing-private.sqlite3')
        with self.assertLogs('zhishi.health', level='ERROR') as captured:
            status, _, body = self.request('/api/health')
        self.assertEqual(status, 503)
        self.assertFalse(body['database']['ok'])
        self.assertFalse(Path(self.pilot.database).exists())
        self.assertNotIn('missing-private', json.dumps(body))
        self.assertNotIn('missing-private', '\n'.join(captured.output))

    def test_get_and_post_failures_have_safe_log_location_and_response_correlation(self):
        for route, target, data in [('/api/console?access=private-query', 'state', None),
                                    ('/api/settings', 'configure', {'api_key': 'private-body'})]:
            service = self.pilot.workbench if target == 'state' else self.pilot
            with patch.object(service, target, side_effect=RuntimeError('private-exception')):
                with self.assertLogs('zhishi.http', level='ERROR') as captured:
                    status, headers, body = self.request(route, data)
            self.assertEqual(status, 500)
            record = json.loads(SafeJsonFormatter().format(captured.records[0]))
            self.assertEqual(record['request_id'], headers['X-Request-ID'])
            self.assertEqual(record['error_type'], 'RuntimeError')
            self.assertTrue(record['frames'])
            text = json.dumps(record) + json.dumps(body) + '\n'.join(captured.output)
            for secret in ('private-query', 'private-body', 'private-exception', self.token):
                self.assertNotIn(secret, text)

    def test_unrecognized_paths_cannot_expand_metric_labels(self):
        self.request('/private-one?secret=hidden')
        self.request('/private-two')
        _, _, body = self.request('/api/health')
        self.assertEqual(body['requests']['other']['calls'], 2)
        self.assertEqual(set(body['requests']), {'other'})

    def test_workers_stop_cleanly_and_do_not_start_twice(self):
        no_op = lambda: None
        notifications = SimpleNamespace(tick=no_op)
        pilot = SimpleNamespace(tick=no_op, workflow=notifications,
                                two_stage=SimpleNamespace(tick=no_op, notifications=notifications),
                                abc=SimpleNamespace(tick=no_op, notify_tick=no_op), product_demo=notifications,
                                ai_dispatch=SimpleNamespace(schedule_tick=no_op, work_tick=no_op, notify_tick=no_op),
                                transport=None, employee2_transport=None)
        workers = BackgroundWorkers(pilot)
        self.assertEqual(workers.snapshot()['status'], 'not_started')
        try:
            workers.start()
            self.assertEqual(len(workers.threads), 10)
            self.assertEqual(workers.snapshot()['status'], 'starting')
            with self.assertRaises(RuntimeError):
                workers.start()
        finally:
            workers.close()
        self.assertEqual(workers.snapshot()['status'], 'stopped')
        self.assertTrue(all(not thread.is_alive() for thread in workers.threads))


if __name__ == '__main__':
    unittest.main()
