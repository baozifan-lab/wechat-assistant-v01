import json
import hashlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from zhishi.core.clock import now, stamp
from zhishi.runtime import build_pilot, create_server


class ReadyModel:
    def settings(self):
        return {'ready': True, 'model': 'fake', 'base_url': '', 'has_key': False}

    def interpret(self, context, text):
        return {'action': 'accept', 'confidence': 1, 'reason': '测试'}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan = self.root / 'projects/ai-dispatch/tasks.json'
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text(json.dumps({'tasks': [{
            'id': 'test-product', 'title': '测试商品排查', 'enabled': True,
            'schedule': {'kind': 'manual'},
            'roles': dict.fromkeys(('manager', 'employee1', 'employee2'), 'local'),
            'source': 'file', 'input_directory': str(self.root / 'inputs'),
            'skills': {'employee1': 'product.inputs.v1', 'employee2': 'product.compare.v1', 'final': 'product.verify.v1'},
        }]}), encoding='utf-8')
        self.pilot = build_pilot(self.root / 'test.db', root=self.root, enable_wechat=False)
        self.pilot.ai_dispatch.model = ReadyModel()
        self.server = create_server(self.pilot, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.token = self.server.RequestHandlerClass.token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, data=None, token=True):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Pilot-Token'] = self.token
        request = urllib.request.Request(self.base + path, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def create(self, request_id='workbench-create-0001', title='开始今日工作'):
        return self.request('/api/console/reminder', {'action': 'create', 'title': title,
            'due': stamp(now() + timedelta(hours=1)), 'request_id': request_id})

    def test_console_is_read_only_and_requires_credential(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console', token=False)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        for _ in range(2):
            state = self.request('/api/console')
            self.assertEqual(state['tasks'], [])
            self.assertEqual(state['dashboard']['open'], 0)
            self.assertTrue(state['templates'][0]['can_start'])
        self.assertEqual(self.pilot.ai_dispatch.state()['runs'], [])

    def test_invoice_appears_and_download_requires_auth_and_intact_file(self):
        code = 'FP-20261106-ABCDEF12'
        file = self.root/'outputs'/'invoice-demo'/code/'sample.xlsx'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'example-file')
        detail = {'display_name':'发票1','start':'2026-11-01','end':'2026-11-07',
                  'file':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(), 'count':1}
        with self.pilot.connect() as db:
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)', (code,'employee1_wait',json.dumps(detail),stamp()))
            db.execute("INSERT INTO invoice_demo_notices(code,kind,state,detail) VALUES (?,'employee1_review','unknown','')", (code,))
        state = self.request('/api/console')
        item = next(t for t in state['tasks'] if t['kind']=='invoice')
        self.assertEqual(item['title'], '发票1')
        self.assertEqual(item['status'], 'blocked')
        self.assertEqual(state['dashboard']['blocked'], 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(item['download'], token=False)
        error.exception.close()
        request = urllib.request.Request(self.base+item['download'], headers={'X-Pilot-Token':self.token})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), b'example-file')
        file.write_bytes(b'changed')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_close_ai_stops_pending_work_and_rejects_stale_state(self):
        code = self.pilot.ai_dispatch.start('test-product', 'close-start-0001')
        with self.pilot.connect() as db:
            db.execute("INSERT INTO ai_dispatch_inbox(request_id,code,role,revision,text,state,created) VALUES (?,?,?,0,?,'pending',?)",
                       ('close-pending-reply',code,'manager','确认',stamp()))
        action = {'id':'ai:'+code, 'state':'manager_wait', 'revision':0, 'request_id':'close-req-0001'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/task/close', action, token=False)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/task/close', dict(action, revision=99))
        error.exception.close()
        self.pilot.ai_dispatch.worker_lock.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/console/task/close', action)
            error.exception.close()
        finally:
            self.pilot.ai_dispatch.worker_lock.release()
        self.assertEqual(self.pilot.ai_dispatch.row(code)['state'], 'manager_wait')
        self.assertIn('已关闭', self.request('/api/console/task/close', action)['reply'])
        self.assertIn('已关闭', self.request('/api/console/task/close', action)['reply'])
        self.pilot.ai_dispatch.notify_tick()
        with self.pilot.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM ai_dispatch_notices WHERE code=?',(code,)).fetchone()['state'], 'cancelled')
            self.assertEqual(db.execute('SELECT state FROM ai_dispatch_inbox WHERE code=?',(code,)).fetchone()['state'], 'cancelled')
        item = next(t for t in self.request('/api/console')['tasks'] if t['id']=='ai:'+code)
        self.assertEqual(item['status'], 'cancelled')
        self.assertNotIn('close', item['actions'])
        self.assertNotEqual(self.pilot.ai_dispatch.start('test-product', 'close-start-0002'), code)

    def test_close_invoice_cancels_pending_notice_and_busy_worker_is_safe(self):
        code = 'FP-20261106-FAFAFAFA'
        detail = {'display_name':'发票1','start':'2026-11-01','end':'2026-11-07'}
        with self.pilot.connect() as db:
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)', (code,'manager_wait',json.dumps(detail),stamp()))
            db.execute("INSERT INTO invoice_demo_notices(code,kind,state,detail) VALUES (?,'manager','pending','')", (code,))
        action = {'id':'invoice:'+code,'state':'manager_wait','request_id':'close-invoice-0001'}
        self.pilot.invoice_demo.lock.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/console/task/close', action)
            error.exception.close()
        finally:
            self.pilot.invoice_demo.lock.release()
        self.assertIn('已关闭', self.request('/api/console/task/close', action)['reply'])
        self.pilot.invoice_demo.tick()
        with self.pilot.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM invoice_demo_runs WHERE code=?',(code,)).fetchone()['state'], 'cancelled')
            self.assertEqual(db.execute('SELECT state FROM invoice_demo_notices WHERE code=?',(code,)).fetchone()['state'], 'cancelled')
        item = next(t for t in self.request('/api/console')['tasks'] if t['id']=='invoice:'+code)
        self.assertEqual(item['status'], 'cancelled')

    def test_close_legacy_tasks_cancels_their_pending_notifications(self):
        created = stamp()
        with self.pilot.connect() as db:
            db.execute("INSERT INTO workflow_runs(uid,code,version,task_key,work_date,fingerprint,state,created_at,review_token,plan_json) VALUES ('w1','JOB-X',1,'demo','2026-11-06','f','awaiting_review',?,'t',?)",(created,json.dumps({'title':'文档示例'})))
            db.execute("INSERT INTO workflow_notices(run_uid,kind,state) VALUES ('w1','review','pending')")
            db.execute("INSERT INTO two_stage_runs(uid,code,version,state,plan_json,access_token,created_at,updated_at) VALUES ('t1','PAIR-X',1,'a_review','{}','t',?,?)",(created,created))
            db.execute("INSERT INTO two_stage_notices(run_uid,kind,state,created_at) VALUES ('t1','a_review','pending',?)",(created,))
            db.execute("INSERT INTO abc_runs(uid,code,state,scheduled_at,route_json,plan_json,access_token,created_at,updated_at) VALUES ('a1','ABC-X','scheduled',?,'{}','{}','t',?,?)",(created,created,created))
            db.execute("INSERT INTO abc_notices(run_uid,kind,state) VALUES ('a1','A_task','pending')")
        for kind,uid,state,table,notices in [('work','w1','awaiting_review','workflow_runs','workflow_notices'),
                ('two','t1','a_review','two_stage_runs','two_stage_notices'),('abc','a1','scheduled','abc_runs','abc_notices')]:
            response = self.request('/api/console/task/close', {'id':kind+':'+uid,'state':state,'request_id':'close-legacy-'+uid})
            self.assertIn('已关闭', response['reply'])
            with self.pilot.connect() as db:
                self.assertEqual(db.execute('SELECT state FROM '+table+' WHERE uid=?',(uid,)).fetchone()['state'], 'cancelled')
                self.assertEqual(db.execute('SELECT state FROM '+notices+' WHERE run_uid=?',(uid,)).fetchone()['state'], 'cancelled')
        state = self.request('/api/console')
        self.assertTrue(all(next(item for item in state['tasks'] if item['id']==kind+':'+uid)['status']=='cancelled'
                            for kind,uid in [('work','w1'),('two','t1'),('abc','a1')]))

    def test_structured_create_does_not_interpret_title_as_a_command_and_replays_once(self):
        data = {'action': 'create', 'title': '开始今日工作',
                'due': stamp(now() + timedelta(hours=1)), 'request_id': 'workbench-same-create'}
        with patch.object(self.pilot, 'interpret', side_effect=AssertionError('must not call AI')):
            first = self.request('/api/console/reminder', data)
            self.assertEqual(first, self.request('/api/console/reminder', data))
        state = self.request('/api/console')
        self.assertEqual(len(state['tasks']), 1)
        self.assertEqual(state['tasks'][0]['title'], '开始今日工作')
        self.assertEqual(state['dashboard']['scheduled'], 1)
        self.assertEqual(self.pilot.workflow.state()['jobs'], [])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/reminder', dict(data, title='另一件事'))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_done_and_cancel_use_distinct_groups_and_stop_reminders(self):
        self.create('workbench-done-create')
        self.create('workbench-cancel-create')
        for ident, action in [(1, 'done'), (2, 'cancel')]:
            data = {'action': action, 'id': ident, 'request_id': f'workbench-action-{action}'}
            self.request('/api/console/reminder', data)
            self.request('/api/console/reminder', data)
        state = self.request('/api/console')
        self.assertEqual(state['dashboard'], {'open': 0, 'done': 1, 'blocked': 0, 'cancelled': 1, 'scheduled': 0})
        self.assertTrue(all(not task['actions'] and task['next_at'] is None for task in state['tasks']))

    def test_ai_and_reminders_share_overview_but_ai_cannot_be_checked_done(self):
        self.create()
        code = self.pilot.ai_dispatch.start('test-product', 'workbench-ai-start')
        state = self.request('/api/console')
        self.assertEqual(state['dashboard']['open'], 2)
        self.assertEqual({t['kind'] for t in state['tasks']}, {'ai', 'reminder'})
        self.assertFalse(state['templates'][0]['can_start'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/reminder', {'action': 'done', 'id': 'ai:' + code, 'request_id': 'workbench-bad-ai-done'})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.pilot.ai_dispatch.row(code)['state'], 'manager_wait')

    def test_context_chat_keeps_stage_revision_and_channel_checks(self):
        flow = self.pilot.ai_dispatch
        code = flow.start('test-product', 'workbench-reply-start')
        flow.notify_tick()
        data = {'target': 'ai:' + code, 'revision': 0, 'text': '开始吧', 'request_id': 'workbench-local-reply'}
        self.request('/api/console/chat', data)
        self.request('/api/console/chat', data)
        flow.work_tick()
        self.assertEqual(flow.row(code)['state'], 'employee1_wait')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/chat', dict(data, request_id='workbench-stale-reply'))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        run = flow.row(code)
        run['task']['roles'] = dict(manager='primary', employee1='primary', employee2='employee2')
        with self.pilot.connect() as db:
            db.execute('UPDATE ai_dispatch_runs SET task_json=? WHERE code=?', (json.dumps(run['task']), code))
        state = self.request('/api/console')
        task = next(t for t in state['tasks'] if t['kind'] == 'ai')
        self.assertNotIn('reply', task['actions'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/chat', dict(data, revision=run['revision'], request_id='workbench-wrong-channel'))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(flow.row(code)['state'], 'employee1_wait')

    def test_overview_includes_runs_older_than_legacy_thirty_run_window(self):
        flow = self.pilot.ai_dispatch
        for i in range(32):
            code = flow.start('test-product', f'workbench-history-{i:04}')
            with self.pilot.connect() as db:
                db.execute("UPDATE ai_dispatch_runs SET state='complete' WHERE code=?", (code,))
        self.assertEqual(len(flow.state()['runs']), 30)
        state = self.request('/api/console')
        self.assertEqual(state['dashboard']['done'], 32)
        self.assertEqual(len(state['tasks']), 32)

    def test_console_does_not_expose_channel_credentials(self):
        class PrivateChannel:
            enabled = True

            def status(self):
                return {'bound': True, 'enabled': True, 'gateway_listening': True,
                        'secret': 'private-bridge-secret', 'account_id': 'private-account'}

        self.pilot.transport = PrivateChannel()
        value = self.request('/api/console')
        self.assertTrue(value['settings']['channels'][0]['online'])
        self.assertNotIn('private-bridge-secret', json.dumps(value))
        self.assertNotIn('private-account', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
