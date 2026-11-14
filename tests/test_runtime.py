import importlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from zhishi.config import ROOT
from zhishi.runtime import build_pilot, create_server
from zhishi.service import Pilot
from zhishi.web.handler import Handler


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.temp.cleanup()

    def serve(self, name):
        root = Path(self.temp.name)/('workspace-'+Path(name).stem)
        plan = root/'projects'/'ai-dispatch'/'tasks.json'
        plan.parent.mkdir(parents=True)
        plan.write_text(json.dumps({'tasks':[{
            'id':'product-investigation','title':'隔离的测试任务','enabled':False,'schedule':{'kind':'manual'},
            'roles':dict.fromkeys(('manager','employee1','employee2'),'local'),
            'allow_shared_routes':False,'input_directory':str(root/'inputs'),'source':'file',
            'skills':{'employee1':'product.inputs.v1','employee2':'product.compare.v1','final':'product.verify.v1'}
        }]}), encoding='utf-8')
        pilot = build_pilot(Path(self.temp.name)/name, root=root, enable_wechat=False)
        self.assertIsNone(pilot.transport)
        server = create_server(pilot, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        base = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base) as response:
            html = response.read().decode()
        token = re.search(r'<meta name="pilot-token" content="([^"]+)">', html)[1]
        return pilot, base, token

    def test_static_modules_and_existing_routes(self):
        _, base, _ = self.serve('static.db')
        for route in ['/', '/work', '/wechat', '/two-stage', '/abc', '/ai-workflow', '/wechat2']:
            with urllib.request.urlopen(base+route) as response:
                html = response.read().decode()
                self.assertIn('type="module" src="/static/workbench.js"', html)
                self.assertNotIn('__PILOT_TOKEN__', html)
        for name in ['api.js','ui.js','app.js','workbench.js','navigation.js','workflow.js','two_stage.js','abc.js','styles.css']:
            with urllib.request.urlopen(base+'/static/'+name) as response:
                self.assertEqual(response.status, 200)
                self.assertIn('text/css' if name.endswith('.css') else 'text/javascript', response.headers['Content-Type'])
        for route in ['/static/../app.py','/static/not-registered.js','/api/workflows','/api/abc']:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base+route)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

    def test_servers_do_not_share_tokens_or_application_state(self):
        first, url1, token1 = self.serve('first.db')
        second, url2, token2 = self.serve('second.db')
        self.assertNotEqual(token1, token2)
        def post(url, token):
            request = urllib.request.Request(url+'/api/chat', data=json.dumps({
                'text':'1 小时后提醒我验证模块隔离','request_id':'runtime-isolation-001'}).encode(),
                headers={'Content-Type':'application/json','X-Pilot-Token':token})
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as error:
            post(url2, token1)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        post(url1, token1)
        self.assertEqual(len(first.state()['tasks']), 1)
        self.assertEqual(second.state()['tasks'], [])

    def test_running_server_owns_its_port(self):
        pilot, base, _ = self.serve('exclusive.db')
        port = int(base.rsplit(':', 1)[1])
        with self.assertRaises(OSError):
            create_server(pilot, port)

    def test_duplicate_start_does_not_run_database_recovery(self):
        from zhishi.runtime import main
        _, base, _ = self.serve('duplicate-start.db')
        with patch('sys.argv', ['zhishi', '--port', base.rsplit(':', 1)[1]]), patch('zhishi.runtime.build_pilot') as build:
            with self.assertRaises(OSError):
                main()
            build.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows PostgreSQL startup')
    def test_local_postgres_starts_once_and_reports_failures(self):
        from zhishi.core.local_postgres import ensure_local_postgres
        base = Path(self.temp.name) / 'postgres'
        control = base / 'pgsql' / 'bin' / 'pg_ctl.exe'
        control.parent.mkdir(parents=True)
        control.touch()
        (base / 'data').mkdir()
        (base / 'data' / 'PG_VERSION').touch()
        with patch('zhishi.core.local_postgres.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(ensure_local_postgres(base)[0], 'running')
            self.assertEqual(run.call_count, 1)
        with patch('zhishi.core.local_postgres.subprocess.run', side_effect=[SimpleNamespace(returncode=3), SimpleNamespace(returncode=0)]) as run:
            self.assertEqual(ensure_local_postgres(base)[0], 'started')
            self.assertIn('-c listen_addresses=127.0.0.1', run.call_args.args[0])
        with patch('zhishi.core.local_postgres.subprocess.run', side_effect=OSError()):
            self.assertEqual(ensure_local_postgres(base)[0], 'failed')

    def test_ai_workflow_page_and_api_keep_unconfigured_tasks_disabled(self):
        pilot, base, token = self.serve('ai-workflow.db')
        with urllib.request.urlopen(base+'/ai-workflow') as response:
            html = response.read().decode()
            self.assertIn('/static/workbench.js', html)
            self.assertNotIn('__PILOT_TOKEN__', html)
        with urllib.request.urlopen(base+'/static/ai_dispatch.js') as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base+'/api/ai-dispatch')
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        request = urllib.request.Request(base+'/api/ai-dispatch', headers={'X-Pilot-Token': token})
        with urllib.request.urlopen(request) as response:
            state = json.load(response)
        self.assertEqual(state['runs'], [])
        self.assertFalse(state['tasks'][0]['enabled'])
        request=urllib.request.Request(base+'/api/ai-dispatch/preview',headers={'X-Pilot-Token':token})
        with urllib.request.urlopen(request) as response:
            preview=json.load(response)
        self.assertTrue(preview['preview'])
        self.assertEqual(len(preview['messages']),6)
        self.assertEqual(pilot.ai_dispatch.state()['runs'],[])
        data = {'task_id':'product-investigation', 'request_id':'test-ai-start-001'}
        for credential, code in [('',403),(token,400)]:
            request = urllib.request.Request(base+'/api/ai-dispatch/start', data=json.dumps(data).encode(),
                                            headers={'X-Pilot-Token':credential,'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
        self.assertEqual(pilot.ai_dispatch.state()['runs'], [])

    def test_compatibility_imports_and_both_start_commands(self):
        from zhishi.channels.wechat.bridge import WechatBridge
        from zhishi.workflows.service import Workflows
        self.assertIs(importlib.import_module('app').Pilot, Pilot)
        self.assertIs(importlib.import_module('app').Handler, Handler)
        self.assertIs(importlib.import_module('wechat_bridge').WechatBridge, WechatBridge)
        self.assertIs(importlib.import_module('workflow').Workflows, Workflows)
        for command in [[sys.executable, 'app.py', '--help'],[sys.executable,'-m','zhishi','--help']]:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertIn(b'--database', result.stdout)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows encrypted API configuration')
    def test_ai_model_configuration_is_authenticated_private_and_not_a_task(self):
        from unittest.mock import patch
        from zhishi.models.workflow import WorkflowModel
        pilot, base, token = self.serve('ai-model-config.db')
        pilot.ai_dispatch.model = WorkflowModel(Path(self.temp.name)/'workflow-model.json')
        data = {'base_url':'https://example.test/v1','model':'qwen3.8-max','api_key':'private-http-test-key'}

        def post(route, data, credential=token):
            request = urllib.request.Request(base+route, data=json.dumps(data).encode(),
                                            headers={'Content-Type':'application/json','X-Pilot-Token':credential})
            with urllib.request.urlopen(request) as response:
                return json.load(response)

        with self.assertRaises(urllib.error.HTTPError) as error:
            post('/api/ai-dispatch/model', data, '')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        result = post('/api/ai-dispatch/model', data)
        self.assertTrue(result['model']['has_key'])
        self.assertNotIn('private-http-test-key', json.dumps(result))
        with patch.object(WorkflowModel, '_open', side_effect=urllib.error.URLError('offline')):
            result = post('/api/ai-dispatch/model/test', {})
        self.assertFalse(result['ok'])
        self.assertEqual(pilot.ai_dispatch.state()['runs'], [])
        self.assertNotIn('private-http-test-key', json.dumps(pilot.ai_dispatch.state()))


if __name__ == '__main__':
    unittest.main()
