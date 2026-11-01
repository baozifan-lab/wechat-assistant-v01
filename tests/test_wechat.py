import json
import io
import tempfile
import unittest
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from zhishi.web.handler import Handler
from zhishi.service import Pilot
from zhishi.core.clock import now, stamp
from http.server import ThreadingHTTPServer
from zhishi.channels.wechat.bridge import WechatBridge
from zhishi.channels.wechat import login as wechat_connect


class FakeChannel:
    enabled = True

    def __init__(self, bound=True, result='submitted_unverified'):
        self.bound = bound
        self.result = result
        self.sent = []

    def route(self):
        return {'account_id':'test', 'peer_id':'owner'} if self.bound else None

    def send(self, text):
        self.sent.append(text)
        return self.result, 'test transport result'

    def status(self):
        return {'enabled':True, 'bound':self.bound}


class WechatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def due(self, pilot):
        pilot.chat('1 分钟后提醒我检查微信', 'test-request-001')
        with pilot.connect() as db:
            db.execute('UPDATE tasks SET next_at=?', (stamp(now()-timedelta(seconds=1)),))

    def test_live_transport_does_not_report_demo_delivery(self):
        transport=FakeChannel()
        pilot=Pilot(self.root/'test.db', transport)
        self.due(pilot)
        pilot.tick()
        pilot.tick()
        self.assertEqual(len(transport.sent), 1)
        row=pilot.state()['outbox'][0]
        self.assertEqual(row['state'], 'submitted_unverified')
        self.assertEqual(row['channel'], 'wechat')

    def test_unknown_send_is_not_automatically_retried(self):
        transport=FakeChannel(result='unknown')
        pilot=Pilot(self.root/'test.db', transport)
        self.due(pilot)
        pilot.tick()
        pilot.tick()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(pilot.state()['outbox'][0]['state'], 'unknown')

    def test_unbound_channel_keeps_reminder_pending(self):
        transport=FakeChannel(bound=False)
        pilot=Pilot(self.root/'test.db', transport)
        self.due(pilot)
        pilot.tick()
        self.assertEqual(pilot.state()['outbox'][0]['state'], 'pending')
        self.assertEqual(transport.sent, [])
        pilot.chat('完成 1', 'test-request-002')
        transport.bound=True
        pilot.tick()
        self.assertEqual(transport.sent, [])

    def test_crash_during_send_becomes_unknown(self):
        pilot=Pilot(self.root/'test.db', FakeChannel())
        self.due(pilot)
        with pilot.connect() as db:
            db.execute("INSERT INTO outbox(owner,task_id,version,text,state,created_at,channel) VALUES (?,?,?,?,?,?,?)",
                       ('pilot-owner',1,1,'test','sending',stamp(),'wechat'))
        restarted=Pilot(self.root/'test.db', FakeChannel())
        self.assertEqual(restarted.state()['outbox'][0]['state'], 'unknown')

    def test_binding_requires_exact_account_peer_and_direct_chat(self):
        bridge=WechatBridge(self.root, self.root/'profile')
        bridge.route_path.write_text(json.dumps({'account_id':'account-a', 'peer_id':'owner'}))
        (bridge.profile/'zhishi-bridge.enabled').write_text('enabled')
        good={'account_id':'account-a', 'peer_id':'owner'}
        self.assertTrue(bridge.authorize(good))
        self.assertFalse(bridge.authorize({**good,'peer_id':'other'}))
        self.assertFalse(bridge.authorize({**good,'account_id':'account-b'}))
        self.assertFalse(bridge.authorize({**good,'is_group':True}))

    def test_inbound_message_id_is_scoped_and_stable(self):
        data={'account_id':'a','peer_id':'p','source_message_id':'12345678901234567890'}
        self.assertEqual(WechatBridge.request_id(data), WechatBridge.request_id(data))
        self.assertNotEqual(WechatBridge.request_id(data), WechatBridge.request_id({**data,'account_id':'b'}))
        with self.assertRaises(ValueError):
            WechatBridge.request_id({**data,'source_message_id':''})

    def test_review_package_stages_and_passes_all_media(self):
        bridge=WechatBridge(self.root,self.root/'profile')
        bridge.route_path.write_text(json.dumps({'account_id':'test','peer_id':'owner'}))
        (bridge.profile/'zhishi-bridge.enabled').touch()
        (self.root/'.runtime/node-v24.22.1-win-x64').mkdir(parents=True)
        (self.root/'.runtime/node-v24.22.1-win-x64/node.exe').touch()
        cli=self.root/'.runtime/openclaw/node_modules/openclaw/openclaw.mjs'
        cli.parent.mkdir(parents=True);cli.touch()
        output=self.root/'outputs';output.mkdir()
        files=[]
        for name in ['test.xlsx','preview.png','issues.csv']:
            file=output/name;file.write_bytes(b'example');files.append(file)
        with patch('zhishi.channels.wechat.bridge.subprocess.run',return_value=Mock(returncode=0)) as execute:
            state,_=bridge.send_package('review package',files)
        self.assertEqual(state,'submitted_unverified')
        command=execute.call_args.args[0]
        self.assertEqual(command[2:5],['gateway','call','send'])
        payload=json.loads(command[command.index('--params')+1])
        self.assertEqual(payload['to'],'owner')
        self.assertEqual(payload['accountId'],'test')
        self.assertEqual(payload['channel'],'openclaw-weixin')
        self.assertEqual(payload['message'],'review package')
        self.assertTrue(payload['idempotencyKey'])
        self.assertEqual(len(payload['mediaUrls']),3)
        self.assertEqual(command[command.index('--expect-url')+1],'ws://127.0.0.1:18791')
        for value in payload['mediaUrls']:
            staged=Path(value);self.assertTrue(staged.is_relative_to(bridge.profile/'media'))
            self.assertEqual(staged.read_bytes(),b'example')

    def test_gateway_recovery_requires_authorization_and_offline_gateway(self):
        bridge=WechatBridge(self.root,self.root/'profile')
        with patch.object(bridge,'gateway_listening',return_value=False), patch('zhishi.channels.wechat.bridge.subprocess.Popen') as launch:
            bridge.ensure_gateway()
        launch.assert_not_called()
        bridge.route_path.write_text(json.dumps({'account_id':'test','peer_id':'owner'}))
        (bridge.profile/'zhishi-bridge.enabled').touch()
        with patch.object(bridge,'gateway_listening',return_value=True), patch('zhishi.channels.wechat.bridge.subprocess.Popen') as launch:
            bridge.ensure_gateway()
        launch.assert_not_called()

    def test_gateway_recovery_uses_saved_login_and_backs_off(self):
        bridge=WechatBridge(self.root,self.root/'profile')
        bridge.route_path.write_text(json.dumps({'account_id':'test','peer_id':'owner'}))
        (bridge.profile/'zhishi-bridge.enabled').touch()
        process=Mock();process.poll.return_value=None
        with patch.object(bridge,'gateway_listening',return_value=False), \
             patch('zhishi.channels.wechat.bridge.subprocess.Popen',return_value=process) as launch, \
             patch('zhishi.channels.wechat.bridge.time.monotonic',side_effect=[100,120,191]):
            bridge.ensure_gateway()
            bridge.ensure_gateway()
            self.assertEqual(launch.call_count,1)
            command=launch.call_args.args[0]
            self.assertEqual(command[-1],'--use-saved')
            self.assertEqual(Path(command[-2]),self.root/'wechat_connect.py')
            process.poll.return_value=0
            bridge.ensure_gateway()
            self.assertEqual(launch.call_count,1)
            bridge.ensure_gateway()
            self.assertEqual(launch.call_count,2)

    def test_package_rejects_files_outside_outputs(self):
        bridge=WechatBridge(self.root,self.root/'profile')
        outside=self.root/'private.csv';outside.write_text('private')
        with patch('zhishi.channels.wechat.bridge.subprocess.run') as execute:
            self.assertEqual(bridge.send_package('review',[outside])[0],'blocked')
        execute.assert_not_called()

    def test_pinned_binding_is_checked_again_before_cli_send(self):
        bridge=WechatBridge(self.root,self.root/'profile')
        old={'account_id':'test','peer_id':'owner'}
        new={'account_id':'test','peer_id':'other'}
        with patch.object(bridge,'route',side_effect=[old,new]), patch('zhishi.channels.wechat.bridge.subprocess.run') as execute:
            state,_=bridge.send_package('review',[],expected_route=old)
        self.assertEqual(state,'blocked')
        execute.assert_not_called()

    def test_refresh_requests_a_new_qr_while_login_is_running(self):
        bridge=WechatBridge(self.root, self.root/'profile')
        bridge.login_process=Mock()
        bridge.login_process.poll.return_value=None
        with patch('zhishi.channels.wechat.bridge.subprocess.Popen') as launch:
            first=bridge.start_login()
            old=(self.root/'data'/'wechat-refresh.request').read_text('ascii')
            bridge.start_login()
            new=(self.root/'data'/'wechat-refresh.request').read_text('ascii')
        self.assertTrue(first['ok'])
        self.assertNotEqual(old,new)
        launch.assert_not_called()

    def test_supervisor_restarts_owned_login_when_refresh_requested(self):
        process=Mock()
        process.stdout=io.StringIO('')
        process.poll.return_value=0
        with patch('zhishi.channels.wechat.login.subprocess.Popen',return_value=process), \
             patch('zhishi.channels.wechat.login.refresh_generation',return_value='new-request'), \
             patch('zhishi.channels.wechat.login.stop_login_process') as stop, \
             patch('zhishi.channels.wechat.login.write_status'):
            result=wechat_connect.login_attempt('old-request')
        self.assertEqual(result,'refresh')
        stop.assert_called_once_with(process)

    def test_real_http_bridge_auth_and_deduplication(self):
        bridge=WechatBridge(self.root, self.root/'profile')
        bridge.route_path.write_text(json.dumps({'account_id':'a','peer_id':'p'}))
        (bridge.profile/'zhishi-bridge.enabled').write_text('enabled')
        pilot=Pilot(self.root/'http.db', bridge)
        class TestHandler(Handler):
            pass
        TestHandler.pilot=pilot
        TestHandler.token='fixture-browser-only'
        server=ThreadingHTTPServer(('127.0.0.1',0),TestHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f'http://127.0.0.1:{server.server_port}/api/wechat/inbound'
        data={'account_id':'a','peer_id':'p','source_message_id':'live-http-1','text':'1 分钟后提醒我检查微信'}
        def post(payload,key):
            request=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={
                'Content-Type':'application/json','X-Wechat-Bridge':key})
            return json.load(urllib.request.urlopen(request,timeout=5))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(data,'wrong')
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as error:
                post({**data,'peer_id':'other'},bridge.secret)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            one=post(data,bridge.secret)
            two=post(data,bridge.secret)
            self.assertEqual(one,two)
            self.assertEqual(len(pilot.state()['tasks']),1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
