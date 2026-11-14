import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from zhishi.runtime import build_pilot, create_server


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pilot = build_pilot(self.root / 'test.db', root=self.root, enable_wechat=False)
        self.accounts = self.pilot.accounts
        self.server = create_server(self.pilot, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, data=None, *, credential=True, bridge=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if credential:
            headers['X-Pilot-Token'] = self.server.RequestHandlerClass.token
        if bridge is not None:
            headers['X-Wechat-Bridge'] = bridge
        if origin:
            headers['Origin'] = origin
        request = urllib.request.Request(self.base + path, headers=headers,
            data=json.dumps(data).encode() if data is not None else None)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def add(self, request_id='account-create-01', name='员工3'):
        return self.request('/api/accounts/save', {'name': name, 'wechat_note': '财务微信',
            'note': '发票复核', 'request_id': request_id})['id']

    def test_save_replays_once_and_survives_service_recreation(self):
        first = self.add(); self.assertEqual(self.add(), first)
        second = self.add('account-create-02', '员工4')
        a, b = self.accounts.row(first), self.accounts.row(second)
        self.assertNotEqual(a['port'], b['port'])
        self.assertNotEqual(self.accounts.profile(a), self.accounts.profile(b))
        self.assertFalse(self.accounts.directory.exists())  # Listing/adding does not create credentials.
        from zhishi.web.accounts import AccountDirectory
        recreated = AccountDirectory(self.pilot, self.root, enable_wechat=False)
        self.assertEqual(len(recreated.state()['accounts']), 4)
        self.assertEqual(recreated.row(first)['name'], '员工3')

    def test_edit_rejects_stale_revision_and_invalid_fields(self):
        ident = self.add()
        data = {'id': ident, 'revision': 1, 'name': '张明', 'request_id': 'account-edit-01'}
        self.request('/api/accounts/save', data)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/accounts/save', {**data, 'name': '旧页面改名', 'request_id': 'account-edit-02'})
        self.assertEqual(error.exception.code, 400); error.exception.close()
        self.assertEqual(self.accounts.row(ident)['name'], '张明')
        with self.assertRaises(ValueError):
            self.accounts.save({'name': ' ', 'request_id': 'bad-account-001'})
        with self.assertRaises(ValueError):
            self.accounts.row('../../private')

    def test_all_account_data_and_writes_require_local_credentials(self):
        for path, data in [('/api/accounts', None), ('/api/accounts/qr?id=existing-primary', None),
                           ('/api/accounts/save', {'name': 'Unauthenticated'}),
                           ('/api/accounts/pause', {'id': 'existing-primary'}),
                           ('/api/accounts/delete', {'id': 'existing-primary', 'revision': 1}),
                           ('/api/accounts/restore', {'id': 'existing-primary', 'revision': 1}),
                           ('/api/accounts/connect', {'id': 'existing-primary'})]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, data, credential=False)
            self.assertIn(error.exception.code, (403, 404)); error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/accounts/save', {'name': '不应创建'}, origin='https://example.org')
        self.assertEqual(error.exception.code, 403); error.exception.close()
        self.assertEqual(len(self.accounts.rows()), 2)

    def test_delete_removes_member_and_recreation_does_not_reseed_it(self):
        from zhishi.web.accounts import AccountDirectory
        ident = self.add()
        self.request('/api/accounts/delete', {'id': ident, 'revision': 1})
        state = self.request('/api/accounts')
        self.assertNotIn(ident, [a['id'] for a in state['accounts']])
        self.assertIn(ident, [a['id'] for a in state['deleted_accounts']])
        with self.assertRaises(ValueError):
            self.accounts.row(ident)
        with self.assertRaises(ValueError):
            self.accounts.save({'id': ident, 'revision': 1, 'name': '已删除员工', 'request_id': 'edit-deleted-01'})
        self.request('/api/accounts/delete', {'id': 'existing-primary', 'revision': 1})
        recreated = AccountDirectory(self.pilot, self.root, enable_wechat=False)
        self.assertEqual([a['id'] for a in recreated.state()['accounts']], ['existing-employee2'])
        self.assertEqual(len(recreated.state()['deleted_accounts']), 2)

    def test_delete_stops_authorization_and_restore_keeps_it_paused(self):
        from zhishi.channels.wechat import login
        ident = self.add(); row = self.accounts.row(ident)
        self.accounts.enable_wechat = True
        bridge = self.accounts.transport(row, create=True)
        route = {'account_id': 'employee', 'peer_id': 'peer'}
        bridge.route_path.write_text(json.dumps(route))
        (bridge.profile / 'zhishi-bridge.enabled').write_text('enabled')
        self.request('/api/accounts/delete', {'id': ident, 'revision': 1})
        self.assertFalse(bridge.enabled)
        self.assertFalse(self.accounts.accepts_secret(bridge.secret))
        with patch('zhishi.channels.wechat.bridge.subprocess.Popen') as start:
            self.accounts.maintain(); start.assert_not_called()
        with patch.object(login, 'PROFILE', bridge.profile), patch.object(login, 'LOGIN_PAUSE_GENERATION', login_pause := (bridge.profile / 'zhishi-admin-paused').read_text('ascii')):
            with self.assertRaises(login.BindingRejected):
                login.enable_after_login()
        self.assertTrue(bridge.route_path.exists())
        deleted = self.accounts.row(ident, include_deleted=True)
        self.request('/api/accounts/restore', {'id': ident, 'revision': deleted['revision']})
        self.assertIsNone(self.accounts.row(ident)['deleted_at'])
        self.assertFalse(bridge.enabled)
        self.assertFalse((bridge.profile / 'zhishi-account-deleted').exists())
        self.assertNotEqual(login_pause, (bridge.profile / 'zhishi-admin-paused').read_text('ascii'))

    def test_delete_stale_revision_is_rejected_and_repeated_delete_is_safe(self):
        ident = self.add()
        self.accounts.save({'id': ident, 'revision': 1, 'name': '新姓名', 'request_id': 'rename-before-delete'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/accounts/delete', {'id': ident, 'revision': 1})
        self.assertEqual(error.exception.code, 400); error.exception.close()
        self.assertEqual(self.accounts.row(ident)['name'], '新姓名')
        data = {'id': ident, 'revision': 2}
        self.assertEqual(self.request('/api/accounts/delete', data), self.request('/api/accounts/delete', data))

    def test_dynamic_bridge_uses_private_paths_and_service_port(self):
        ident = self.add(); row = self.accounts.row(ident)
        self.accounts.enable_wechat = True
        bridge = self.accounts.transport(row, create=True)
        self.assertEqual(bridge.profile, self.accounts.profile(row))
        self.assertEqual(bridge.data_directory, self.accounts.login_directory(row))
        self.assertIn(self.base, bridge.login_args)
        with patch('zhishi.channels.wechat.bridge.subprocess.Popen') as start:
            bridge.start_login()
            command = start.call_args.args[0]
            self.assertIn('wechat_account_connect.py', command[3])
            self.assertIn(ident, command)
            self.assertNotIn('--use-saved', command)
        bridge.request_refresh()
        self.assertTrue((self.accounts.login_directory(row) / 'wechat-refresh.request').is_file())
        self.assertFalse((self.root / 'data/wechat-refresh.request').exists())

    def test_pause_preserves_credentials_and_cannot_be_undone_by_saved_recovery(self):
        ident = self.add(); row = self.accounts.row(ident)
        self.accounts.enable_wechat = True
        bridge = self.accounts.transport(row, create=True)
        bridge.route_path.write_text(json.dumps({'account_id': 'account', 'peer_id': 'peer'}))
        (bridge.profile / 'zhishi-bridge.enabled').write_text('enabled')
        secret = bridge.secret
        self.request('/api/accounts/pause', {'id': ident})
        self.assertFalse(bridge.enabled)
        self.assertTrue(bridge.route_path.is_file()); self.assertEqual(bridge.secret, secret)
        with patch('zhishi.channels.wechat.bridge.subprocess.Popen') as start:
            bridge.ensure_gateway(); start.assert_not_called()
        for relative in ('.runtime/node-v24.22.1-win-x64/node.exe', '.runtime/openclaw/node_modules/openclaw/openclaw.mjs'):
            file = self.root / relative; file.parent.mkdir(parents=True, exist_ok=True); file.touch()
        with patch.object(bridge, 'start_login', return_value={'ok': True}):
            self.accounts.connect({'id': ident})
        self.assertFalse(bridge.enabled)  # Remains paused until a fresh QR authorization succeeds.

    def test_official_supervisor_is_configured_per_employee_and_rejects_reuse(self):
        import wechat_account_connect
        ident = self.add()
        home = self.root / 'home'
        primary = home / '.openclaw-zhishi-pilot'
        plugin = primary / 'npm/projects/official/node_modules/@tencent-weixin/openclaw-weixin'
        plugin.mkdir(parents=True)
        fake_login = SimpleNamespace(BindingRejected=RuntimeError)
        with patch.object(wechat_account_connect, 'login', fake_login), patch.object(Path, 'home', return_value=home):
            wechat_account_connect.configure(self.pilot.database, ident, self.base)
            self.assertEqual(fake_login.PROFILE, self.accounts.profile(self.accounts.row(ident)))
            self.assertEqual(fake_login.DATA, self.accounts.login_directory(self.accounts.row(ident)))
            self.assertEqual(fake_login.SERVICE_URL, self.base)
            self.assertNotIn(fake_login.PORT, (18791, 18792))
            self.assertTrue((fake_login.PROFILE / 'openclaw.json').is_file())
            (primary / 'zhishi-binding.json').write_text(json.dumps({'account_id': 'taken', 'peer_id': 'owner'}))
            with self.assertRaises(RuntimeError):
                fake_login.BINDING_CHECK('taken', 'different-peer')

    def test_pause_during_scan_cannot_be_overridden_by_old_login(self):
        from zhishi.channels.wechat import login
        profile = self.root / 'login-profile'; profile.mkdir()
        marker = profile / 'zhishi-admin-paused'; marker.write_text('new-pause')
        with patch.object(login, 'PROFILE', profile), patch.object(login, 'LOGIN_PAUSE_GENERATION', None):
            with self.assertRaises(login.BindingRejected):
                login.enable_after_login()
        self.assertTrue(marker.exists())
        with patch.object(login, 'PROFILE', profile), patch.object(login, 'LOGIN_PAUSE_GENERATION', 'new-pause'):
            login.enable_after_login()
        self.assertFalse(marker.exists())
        self.assertTrue((profile / 'zhishi-bridge.enabled').exists())

    def test_new_employee_cannot_enter_owner_chat_and_secrets_stay_private(self):
        ident = self.add(); row = self.accounts.row(ident)
        self.accounts.enable_wechat = True
        bridge = self.accounts.transport(row, create=True)
        route = {'account_id': 'private-account', 'peer_id': 'private-peer'}
        bridge.route_path.write_text(json.dumps(route), encoding='utf-8')
        (bridge.profile / 'zhishi-bridge.enabled').write_text('enabled')
        body = {**route, 'is_group': False, 'text': '我的待办', 'source_message_id': 'employee-msg-01'}
        with patch.object(self.pilot, 'chat') as owner_chat:
            result = self.request('/api/wechat/inbound', body, credential=False, bridge=bridge.secret)
            self.assertIn('员工3', result['reply']); owner_chat.assert_not_called()
        self.assertTrue(self.accounts.row(ident)['last_seen'])
        public = json.dumps(self.request('/api/accounts'))
        for private in (bridge.secret, 'private-account', 'private-peer', str(bridge.profile)):
            self.assertNotIn(private, public)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/wechat/inbound', {**body, 'peer_id': 'other-peer'}, credential=False, bridge=bridge.secret)
        self.assertEqual(error.exception.code, 403); error.exception.close()

    def test_duplicate_wechat_is_flagged_and_denied(self):
        ids = [self.add(), self.add('account-create-02', '员工4')]
        self.accounts.enable_wechat = True
        route = {'account_id': 'same-account', 'peer_id': 'same-peer'}
        for ident in ids:
            bridge = self.accounts.transport(self.accounts.row(ident), create=True)
            bridge.route_path.write_text(json.dumps(route), encoding='utf-8')
            (bridge.profile / 'zhishi-bridge.enabled').write_text('enabled')
        state = self.accounts.state()
        self.assertTrue(any(a['connection'].get('duplicate') for a in state['accounts']))
        with self.assertRaises(ValueError):
            self.accounts.inbound({**route, 'is_group': False}, bridge.secret)

    def test_qr_requires_current_scan_and_never_returns_old_image(self):
        ident = self.add(); row = self.accounts.row(ident)
        directory = self.accounts.login_directory(row); directory.mkdir(parents=True)
        (directory / 'wechat-login.png').write_bytes(b'png-test')
        status = directory / 'wechat-login.json'
        status.write_text(json.dumps({'state': 'waiting_scan', 'qr_version': 'test'}))
        self.assertEqual(self.accounts.qr_file(ident).read_bytes(), b'png-test')
        status.write_text(json.dumps({'state': 'gateway_started'}))
        with self.assertRaises(ValueError):
            self.accounts.qr_file(ident)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/accounts/qr?id=' + ident)
        self.assertEqual(error.exception.code, 404); error.exception.close()


if __name__ == '__main__':
    unittest.main()
