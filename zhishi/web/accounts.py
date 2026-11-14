"""Local connection directory. Each added employee owns a separate gateway profile."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading

from zhishi.core.clock import stamp


class AccountDirectory:
    def __init__(self, pilot, root, *, enable_wechat=True):
        self.pilot, self.root = pilot, Path(root)
        self.directory = Path(pilot.database).resolve().parent / 'accounts'
        self.enable_wechat = enable_wechat
        self.lock = threading.RLock()
        self.bridges = {}
        self.service_url = None
        with pilot.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS connection_accounts (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, wechat_note TEXT NOT NULL DEFAULT '',
                  note TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL UNIQUE, port INTEGER UNIQUE,
                  revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  last_seen TEXT);
                CREATE TABLE IF NOT EXISTS connection_account_requests (
                  request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, account_id TEXT NOT NULL);
            ''')
            if 'deleted_at' not in {r['name'] for r in db.execute('PRAGMA table_info(connection_accounts)')}:
                db.execute('ALTER TABLE connection_accounts ADD COLUMN deleted_at TEXT')
            for ident, name, channel in [('existing-primary', '负责人 / 员工1', 'primary'),
                                         ('existing-employee2', '员工2', 'employee2')]:
                db.execute('INSERT OR IGNORE INTO connection_accounts '
                           '(id,name,channel,created_at,updated_at) VALUES (?,?,?,?,?)',
                           (ident, name, channel, stamp(), stamp()))

    def rows(self, *, include_deleted=False):
        with self.pilot.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM connection_accounts " +
                ('' if include_deleted else 'WHERE deleted_at IS NULL ') + "ORDER BY "
                "CASE channel WHEN 'primary' THEN 0 WHEN 'employee2' THEN 1 ELSE 2 END,created_at,id")]

    def row(self, ident, *, include_deleted=False):
        if not isinstance(ident, str) or not re.fullmatch(r'(existing-(primary|employee2)|person-[a-f0-9]{24})', ident):
            raise ValueError('员工编号无效。')
        with self.pilot.connect() as db:
            row = db.execute('SELECT * FROM connection_accounts WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('找不到该员工。')
        if row['deleted_at'] and not include_deleted:
            raise ValueError('该员工已删除，请先在“已删除”中恢复。')
        return dict(row)

    @staticmethod
    def field(data, name, maximum, required=False):
        value = data.get(name, '')
        if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
            label = {'name': '姓名', 'wechat_note': '微信备注', 'note': '工作备注'}[name]
            raise ValueError(f'{label}须为' +
                             (f'1–{maximum}' if required else f'0–{maximum}') + '个字符。')
        return value.strip()

    def save(self, data):
        values = [self.field(data, 'name', 60, True), self.field(data, 'wechat_note', 100), self.field(data, 'note', 300)]
        ident = data.get('id')
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('缺少有效的请求编号。')
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock, self.pilot.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT * FROM connection_account_requests WHERE request_id=?', (request_id,)).fetchone()
            if saved:
                if saved['fingerprint'] != fingerprint:
                    raise ValueError('请求编号已用于其他修改。')
                return {'ok': True, 'id': saved['account_id']}
            if ident:
                self.row(ident)
                if type(data.get('revision')) is not int:
                    raise ValueError('请刷新员工信息后重试。')
                changed = db.execute('UPDATE connection_accounts SET name=?,wechat_note=?,note=?,revision=revision+1,updated_at=? '
                                     'WHERE id=? AND revision=?', (*values, stamp(), ident, data['revision'])).rowcount
                if not changed:
                    raise ValueError('员工信息已被修改，请关闭窗口后重新编辑。')
            else:
                used = {r[0] for r in db.execute('SELECT port FROM connection_accounts WHERE port IS NOT NULL')}
                port = next((p for p in range(18800, 18900) if p not in used), None)
                if port is None:
                    raise ValueError('当前最多支持100个独立员工连接。')
                ident = 'person-' + secrets.token_hex(12)
                db.execute('INSERT INTO connection_accounts (id,name,wechat_note,note,channel,port,created_at,updated_at) '
                           'VALUES (?,?,?,?,?,?,?,?)', (ident, *values, ident, port, stamp(), stamp()))
            db.execute('INSERT INTO connection_account_requests VALUES (?,?,?)', (request_id, fingerprint, ident))
        return {'ok': True, 'id': ident}

    def profile(self, row):
        return self.directory / row['id'] / 'profile'

    def login_directory(self, row):
        if row['channel'] == 'primary':
            return self.root / 'data'
        if row['channel'] == 'employee2':
            return self.root / 'data' / 'employee2-wechat'
        return self.directory / row['id'] / 'login'

    def transport(self, row, *, create=False):
        if row['channel'] in ('primary', 'employee2'):
            return self.pilot.transport if row['channel'] == 'primary' else self.pilot.employee2_transport
        if not self.enable_wechat or (not create and not self.profile(row).exists()):
            return None
        with self.lock:
            if row['id'] not in self.bridges:
                from zhishi.channels.wechat.bridge import WechatBridge
                self.bridges[row['id']] = WechatBridge(self.root, self.profile(row), port=row['port'],
                    login_script='wechat_account_connect.py', data_directory=self.login_directory(row),
                    login_args=['--database', str(Path(self.pilot.database).resolve()), '--account-id', row['id'],
                                '--service-url', self.service_url or 'http://127.0.0.1:8765'])
            return self.bridges[row['id']]

    @staticmethod
    def route_key(route):
        return (route.get('account_id'), route.get('peer_id')) if route else None

    def state(self):
        result, seen = [], {}
        for row in self.rows():
            bridge = self.transport(row)
            if row['channel'] in ('primary', 'employee2'):
                channel = self.pilot.workbench._channel(row['channel'], bridge)
            else:
                status = bridge.status() if bridge else {}
                try:
                    login = json.loads((self.login_directory(row) / 'wechat-login.json').read_text('utf-8'))
                    if not isinstance(login, dict):
                        login = {}
                except (OSError, ValueError):
                    login = {}
                channel = {'bound': bool(status.get('bound')), 'online': status.get('gateway_ready') is True and bool(status.get('enabled')),
                           'connection_state': status.get('connection_state', 'unbound'),
                           'login': {k: login.get(k) for k in ('state', 'detail', 'qr_version')},
                           'detail': status.get('connection_detail')}
            route = bridge.route() if bridge else None
            key = self.route_key(route)
            duplicate = seen.get(key) if key else None
            if key and not duplicate:
                seen[key] = row['name']
            channel['duplicate'] = duplicate
            if duplicate:
                channel['online'] = False
                channel['connection_state'] = 'duplicate'
                channel['detail'] = f'与“{duplicate}”使用了同一微信，请使用该员工自己的微信重新扫码。'
            fingerprint = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:8].upper() if key else ''
            result.append({k: row[k] for k in ('id', 'name', 'wechat_note', 'note', 'revision', 'created_at', 'updated_at', 'last_seen')} |
                          {'legacy': row['channel'] in ('primary', 'employee2'), 'shared': row['channel'] == 'primary',
                           'fingerprint': fingerprint, 'connection': channel})
        deleted = [{k: row[k] for k in ('id', 'name', 'wechat_note', 'note', 'revision', 'deleted_at')}
                   for row in self.rows(include_deleted=True) if row['deleted_at']]
        return {'accounts': result, 'deleted_accounts': deleted, 'can_connect': self.enable_wechat,
                'summary': {'total': len(result), 'online': sum(r['connection']['online'] for r in result),
                            'unbound': sum(not r['connection']['bound'] for r in result)}}

    def connect(self, data):
        with self.lock:
            return self._connect(data)

    def _connect(self, data):
        if not self.enable_wechat:
            raise ValueError('当前服务未启用微信连接。')
        row = self.row(data.get('id'))
        if row['channel'] not in ('primary', 'employee2'):
            node = self.root / '.runtime/node-v24.22.1-win-x64/node.exe'
            cli = self.root / '.runtime/openclaw/node_modules/openclaw/openclaw.mjs'
            if not node.is_file() or not cli.is_file():
                raise ValueError('本机微信运行环境尚未安装，无法申请二维码。')
        bridge = self.transport(row, create=True)
        if bridge is None:
            raise ValueError('该微信通道暂不可用。')
        return bridge.start_login()

    def pause(self, data):
        row = self.row(data.get('id'))
        bridge = self.transport(row)
        if bridge is None:
            raise ValueError('这个员工尚未建立微信连接。')
        from zhishi.channels.wechat.gateway import startup_lock
        with startup_lock(bridge.profile / 'authorization') as acquired:
            if not acquired:
                raise ValueError('授权状态正在更新，请稍后重试。')
            (bridge.profile / 'zhishi-admin-paused').write_text(secrets.token_hex(16), encoding='ascii')
        return {'ok': True, 'reply': '微信连接已停用，授权资料保留；重新连接需要再次扫码确认。'}

    def change_membership(self, data, *, restore=False):
        """Remove employees reversibly while revoking their live channel access."""
        from zhishi.channels.wechat.gateway import startup_lock
        with self.lock, self.pilot.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.row(data.get('id'), include_deleted=True)
            if bool(row['deleted_at']) != restore:
                return {'ok': True, 'id': row['id']}
            if type(data.get('revision')) is not int or data['revision'] != row['revision']:
                raise ValueError('员工资料已变化，请刷新列表后再操作。')
            bridge = self.transport(row)
            if bridge:
                with startup_lock(bridge.profile / 'authorization') as acquired:
                    if not acquired:
                        raise ValueError('微信授权正在更新，请稍后重试。')
                    marker = bridge.profile / 'zhishi-account-deleted'
                    # A restore never silently reactivates an old authorization.
                    (bridge.profile / 'zhishi-admin-paused').write_text(secrets.token_hex(16), encoding='ascii')
                    if restore:
                        marker.unlink(missing_ok=True)
                    else:
                        marker.write_text('deleted', encoding='ascii')
            db.execute('UPDATE connection_accounts SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?',
                       (None if restore else stamp(), stamp(), row['id']))
        return {'ok': True, 'id': row['id']}

    def qr_file(self, ident):
        row = self.row(ident)
        directory = self.login_directory(row)
        try:
            login = json.loads((directory / 'wechat-login.json').read_text('utf-8'))
        except (OSError, ValueError):
            raise ValueError('二维码尚未生成。') from None
        if not isinstance(login, dict) or login.get('state') != 'waiting_scan':
            raise ValueError('二维码已失效，请刷新连接状态。')
        file = directory / 'wechat-login.png'
        if not file.is_file():
            raise ValueError('二维码尚未生成。')
        return file

    def maintain(self):
        for row in self.rows():
            if row['channel'] in ('primary', 'employee2'):
                continue
            bridge = self.transport(row)
            if bridge:
                bridge.ensure_gateway()

    def inbound(self, data, secret):
        """Additional employees never enter the owner's pilot task authorization path."""
        for row in self.rows():
            if row['channel'] in ('primary', 'employee2'):
                continue
            bridge = self.transport(row)
            if bridge and secrets.compare_digest(secret, bridge.secret) and bridge.authorize(data):
                key = self.route_key(bridge.route())
                for other in self.rows():
                    if other['id'] == row['id']:
                        continue
                    other_bridge = self.transport(other)
                    if other_bridge and self.route_key(other_bridge.route()) == key:
                        raise ValueError('该微信已用于其他员工，请重新扫码绑定独立微信。')
                with self.pilot.connect() as db:
                    db.execute('UPDATE connection_accounts SET last_seen=? WHERE id=?', (stamp(), row['id']))
                reply = self.pilot.work_orders.command(data.get('text', ''), row['channel'])
                if reply is None:
                    reply = self.pilot.invoice_demo.command(data.get('text', ''), row['channel'])
                if reply is None:
                    reply = self.pilot.ai_dispatch.command(data.get('text', ''), row['channel'], bridge.request_id(data))
                return {'reply': reply or f'已连接知事，你的账户是“{row["name"]}”。请按任务通知回复。'}
        raise ValueError('微信账号或会话未获准访问。')

    def accepts_secret(self, secret):
        if not secret:
            return False
        for row in self.rows():
            if row['channel'] not in ('primary', 'employee2'):
                bridge = self.transport(row)
                if bridge and secrets.compare_digest(secret, bridge.secret):
                    return True
        return False
