import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from zhishi.channels.wechat import login
from zhishi.channels.wechat.bridge import WechatBridge
from zhishi.channels.wechat.gateway import (gateway_readiness, process_alive, startup_lock,
                                             save_gateway_identity, saved_gateway_alive)
from zhishi.web.workbench import Workbench


class GatewayStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / 'profile'
        self.data = self.root / 'data'
        self.profile.mkdir()
        self.data.mkdir()
        self.patches = [patch.object(login, 'PROFILE', self.profile),
                        patch.object(login, 'DATA', self.data)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def bridge(self):
        bridge = WechatBridge(self.root, self.profile)
        bridge.route_path.write_text(json.dumps({'account_id': 'test', 'peer_id': 'owner'}))
        (self.profile / 'zhishi-bridge.enabled').touch()
        return bridge

    def test_start_lock_excludes_another_process_and_releases_on_close(self):
        code = ('from zhishi.channels.wechat.gateway import startup_lock; import sys\n'
                'with startup_lock(sys.argv[1]) as acquired: sys.exit(0 if acquired else 7)')
        def attempt(profile):
            return subprocess.run([sys.executable, '-c', code, str(profile)],
                                  capture_output=True, timeout=10).returncode
        with startup_lock(self.profile) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(attempt(self.profile), 7)
            self.assertEqual(attempt(self.root / 'employee2'), 0)
        self.assertEqual(attempt(self.profile), 0)

    def test_process_liveness_does_not_signal_or_stop_current_process(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(-1))

    def test_saved_gateway_rejects_reused_or_unverified_pid(self):
        (self.data / 'wechat-gateway.pid').write_text(str(os.getpid()), encoding='ascii')
        self.assertFalse(saved_gateway_alive(self.data))
        save_gateway_identity(self.data, os.getpid())
        self.assertTrue(saved_gateway_alive(self.data))
        identity_file = self.data / 'wechat-gateway.identity.json'
        identity = json.loads(identity_file.read_text('utf-8'))
        identity['started'] = 'different-process-instance'
        identity_file.write_text(json.dumps(identity), encoding='utf-8')
        self.assertFalse(saved_gateway_alive(self.data))

    def test_busy_start_lock_does_not_launch_or_overwrite_status(self):
        with startup_lock(self.profile), patch.object(login.subprocess, 'Popen') as launch, \
             patch.object(login, 'write_status') as status:
            login.start_gateway()
        launch.assert_not_called()
        status.assert_not_called()

    def test_cold_start_survives_60_seconds_and_waits_for_readiness(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(login, 'gateway_readiness', side_effect=[{}, {}, {}, {'ready': True}]), \
             patch.object(login, 'saved_gateway_alive', return_value=False), \
             patch.object(login.socket, 'socket') as socket, \
             patch.object(login.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(login.time, 'monotonic', side_effect=[0, 1, 61, 107]), \
             patch.object(login.time, 'sleep'):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            login.start_gateway()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(json.loads((self.data / 'wechat-login.json').read_text('utf-8'))['state'],
                         'gateway_started')

    def test_existing_child_before_port_bind_is_reused(self):
        with patch.object(login, 'gateway_readiness', side_effect=[{}, {'ready': True}]), \
             patch.object(login, 'saved_gateway_alive', return_value=True), \
             patch.object(login.socket, 'socket') as socket, \
             patch.object(login.subprocess, 'Popen') as launch:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            login.start_gateway()
        launch.assert_not_called()

    def test_listening_but_unready_gateway_is_not_started_again(self):
        with patch.object(login, 'gateway_readiness', side_effect=[{}, {'ready': True}]), \
             patch.object(login.socket, 'socket') as socket, \
             patch.object(login.subprocess, 'Popen') as launch:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            login.start_gateway()
        launch.assert_not_called()

    def test_timeout_keeps_live_child_for_next_recovery(self):
        with patch.object(login, 'gateway_readiness', return_value={}), \
             patch.object(login, 'saved_gateway_alive', return_value=True), \
             patch.object(login.socket, 'socket') as socket, \
             patch.object(login.subprocess, 'Popen') as launch, \
             patch.object(login.time, 'monotonic', side_effect=[0, 301, 302, 603]):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            login.start_gateway()
            login.start_gateway()
        launch.assert_not_called()
        self.assertEqual(json.loads((self.data / 'wechat-login.json').read_text('utf-8'))['state'],
                         'needs_attention')

    def test_launch_failure_is_reported_without_losing_authorization(self):
        self.bridge()
        with patch.object(login, 'gateway_readiness', return_value={}), \
             patch.object(login, 'saved_gateway_alive', return_value=False), \
             patch.object(login.socket, 'socket') as socket, \
             patch.object(login.subprocess, 'Popen', side_effect=OSError('fixture')):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            login.start_gateway()
        self.assertTrue((self.profile / 'zhishi-binding.json').exists())
        self.assertEqual(json.loads((self.data / 'wechat-login.json').read_text('utf-8'))['state'],
                         'needs_attention')

    def test_status_distinguishes_listener_startup_and_ready(self):
        bridge = self.bridge()
        with patch.object(bridge, 'gateway_listening', return_value=True), \
             patch('zhishi.channels.wechat.bridge.gateway_readiness', return_value={'ready': False, 'core_ready': False}):
            status = bridge.status()
            self.assertFalse(status['gateway_ready'])
            self.assertEqual(status['connection_state'], 'starting')
            channel = Workbench(None, self.root)._channel('primary', bridge)
            self.assertFalse(channel['online'])
            self.assertIn('正在启动', channel['login']['detail'])
        with patch.object(bridge, 'gateway_listening', return_value=True), \
             patch('zhishi.channels.wechat.bridge.gateway_readiness', return_value={'ready': True, 'core_ready': True}):
            self.assertEqual(bridge.status()['connection_state'], 'ready')
            self.assertTrue(Workbench(None, self.root)._channel('primary', bridge)['online'])

    def test_stale_success_file_cannot_make_an_offline_gateway_online(self):
        bridge = self.bridge()
        (self.data / 'wechat-login.json').write_text(json.dumps({'state': 'gateway_started', 'detail': 'old success'}))
        with patch.object(bridge, 'gateway_listening', return_value=False):
            channel = Workbench(None, self.root)._channel('primary', bridge)
        self.assertFalse(channel['online'])
        self.assertEqual(channel['connection_state'], 'offline')
        self.assertNotEqual(channel['login']['detail'], 'old success')

    def test_unready_channel_preserves_message_as_pending_without_sending(self):
        bridge = self.bridge()
        for name in ('.runtime/node-v24.22.1-win-x64/node.exe',
                     '.runtime/openclaw/node_modules/openclaw/openclaw.mjs'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with patch('zhishi.channels.wechat.bridge.gateway_readiness', return_value={}), \
             patch('zhishi.channels.wechat.bridge.subprocess.run') as send:
            self.assertEqual(bridge.send('fixture')[0], 'pending')
        send.assert_not_called()

    def test_http_readiness_rejects_port_only_html_and_suppressed_channel(self):
        class Handler(BaseHTTPRequestHandler):
            body = b'{}'
            status = 200
            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()
                self.wfile.write(self.body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for code, body, expected in [
                (503, b'{"ready":false,"coreReady":false}', False),
                (200, b'<html>listening</html>', False),
                (200, b'{"ready":true,"coreReady":true,"suppressed":["openclaw-weixin"]}', False),
                (200, b'{"ready":true,"coreReady":true}', True),
            ]:
                Handler.status, Handler.body = code, body
                self.assertEqual(bool(gateway_readiness(server.server_port).get('ready')), expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
