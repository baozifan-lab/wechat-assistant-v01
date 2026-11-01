import importlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        pilot = build_pilot(Path(self.temp.name)/name, enable_wechat=False)
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
        for route in ['/', '/work', '/wechat', '/two-stage', '/abc']:
            with urllib.request.urlopen(base+route) as response:
                html = response.read().decode()
                self.assertIn('type="module" src="/static/workflow.js"', html)
                self.assertNotIn('__PILOT_TOKEN__', html)
        for name in ['api.js','ui.js','app.js','workflow.js','two_stage.js','abc.js','styles.css']:
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


if __name__ == '__main__':
    unittest.main()
