"""Possession-based account binding and signed, replay-safe direct-message ingress."""
from datetime import timedelta
import hashlib
import hmac
import json
import re
import secrets
import time

from zhishi.core.clock import now, stamp
from .auth import actor
from .errors import TeamError, require
from .security import digest
from .service import encoded


class TeamWechat:
    def __init__(self, service, secret=None):
        self.service, self.store = service, service.store
        self.secret = secret
        require(secret is None or len(secret) >= 32, 'TEAM_BRIDGE_SECRET至少32个字符。', 400)

    def begin_binding(self, user_id, data):
        def operation(db, user):
            push = data.get('push_enabled', False)
            require(type(push) is bool, 'push_enabled须为布尔值。', 400)
            code = secrets.token_hex(4).upper()
            db.execute('DELETE FROM team_binding_codes WHERE user_id=?', (user_id,))
            expires = stamp(now() + timedelta(minutes=10))
            db.execute('INSERT INTO team_binding_codes VALUES (?,?,?,?)', (digest(code), user_id, expires, int(push)))
            self.store.audit(db, user_id, 'request_wechat_binding', 'user', user_id)
            return {'binding_code': code, 'expires_at': expires, 'instruction': '在需要绑定的微信私聊中发送：绑定 ' + code}
        require(self.secret, '尚未配置可信微信桥接。', 503)
        return self.service.mutate(user_id, data.get('request_id'), 'begin_binding', data, operation)

    def binding(self, user_id):
        with self.store.connect() as db:
            actor(db, user_id)
            row = db.execute('SELECT push_enabled,created_at FROM team_bindings WHERE user_id=?', (user_id,)).fetchone()
            return {'bound': bool(row), **(dict(row) if row else {})}

    def unbind(self, user_id, data):
        def operation(db, user):
            db.execute('DELETE FROM team_bindings WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM team_binding_codes WHERE user_id=?', (user_id,))
            db.execute("UPDATE team_notifications SET state='cancelled',detail='用户已解绑微信。' WHERE user_id=? AND state='pending'", (user_id,))
            self.store.audit(db, user_id, 'unbind_wechat', 'user', user_id)
            return {'ok': True}
        return self.service.mutate(user_id, data.get('request_id'), 'unbind_wechat', data, operation)

    def verify_signature(self, raw, timestamp, signature):
        require(self.secret, '微信桥接未启用。', 503)
        require(isinstance(timestamp, str) and timestamp.isdigit() and len(timestamp) <= 12, '桥接签名无效。', 403)
        require(abs(time.time() - int(timestamp)) <= 300, '桥接请求已过期。', 403)
        expected = hmac.new(self.secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        require(isinstance(signature, str) and hmac.compare_digest(expected, signature), '桥接签名无效。', 403)

    def receive(self, data):
        # Only call after verify_signature at the HTTP boundary (or from a trusted adapter).
        for field, maximum in (('account_id', 200), ('peer_id', 300), ('source_message_id', 256), ('text', 2000)):
            require(isinstance(data.get(field), str) and 1 <= len(data[field]) <= maximum, '微信消息格式无效。', 400)
        require(data.get('is_group') is False, '仅支持已验证的私聊。', 403)
        scope = digest(encoded([data['account_id'], data['peer_id'], data['source_message_id']]))
        fingerprint = digest(encoded(data))
        with self.store.connect() as db:
            saved = db.execute('SELECT * FROM team_inbound WHERE scope=?', (scope,)).fetchone()
            if saved:
                require(saved['fingerprint'] == fingerprint, '消息编号已用于不同内容。', 409)
                return json.loads(saved['response'])
        command = data['text'].strip()
        if command.startswith('绑定 '):
            return self._bind(data, scope, fingerprint, command[3:].strip().upper())
        with self.store.connect() as db:
            binding = db.execute('SELECT b.user_id,b.account_id,b.peer_id,b.generation FROM team_bindings b JOIN team_users u ON u.id=b.user_id '
                                 'WHERE b.account_id=? AND b.peer_id=? AND u.active=1', (data['account_id'], data['peer_id'])).fetchone()
        require(binding is not None, '此微信未绑定有效员工账号。', 403)
        user_id, request_id = binding['user_id'], 'wx_' + scope
        with self.store.connect() as db:
            previous = db.execute('SELECT response FROM team_requests WHERE user_id=? AND request_id=?', (user_id, request_id)).fetchone()
        if previous:
            reply = self._action_reply(json.loads(previous['response']))
        elif command == '我的任务':
            tasks = self.service.tasks(user_id)
            tasks = [t for t in tasks if t['state'] not in ('completed', 'cancelled')]
            reply = {'reply': '\n'.join(f'{t["title"]} · {t["state"]} · {t["id"]} V{t["version"]}' for t in tasks[:20]) or '当前没有进行中的任务。'}
        else:
            match = re.fullmatch(r'(接单|提交|通过|退回)\s+(t_[a-f0-9]{24})\s+[Vv]?(\d+)(?:\s+(.+))?', command)
            action, task = None, None
            if match:
                action = {'接单': 'accept', '提交': 'submit', '通过': 'approve', '退回': 'reject'}[match[1]]
                task_id, version, reason = match[2], int(match[3]), match[4]
            elif command in ('收到', '接单', '好了', '提交', '没问题', '通过'):
                action = 'accept' if command in ('收到', '接单') else 'submit' if command in ('好了', '提交') else 'approve'
                candidates = [t for t in self.service.tasks(user_id) if (
                    action == 'accept' and t['assignee_id'] == user_id and t['state'] == 'open' or
                    action == 'submit' and t['assignee_id'] == user_id and t['state'] == 'in_progress' or
                    action == 'approve' and t['reviewer_id'] == user_id and t['state'] == 'awaiting_review')]
                if len(candidates) == 1:
                    task = candidates[0]
                    task_id, version, reason = task['id'], task['version'], None
                else:
                    action = None
            if action:
                try:
                    result = self.service.task_action(user_id, task_id, action, {'request_id': request_id, 'expected_version': version, 'reason': reason},
                                                      binding_guard=dict(binding))
                    reply = self._action_reply(result)
                except TeamError as exc:
                    reply = {'reply': str(exc)}
            else:
                reply = {'reply': '请在任务页查看本版成果，或使用“接单/提交/通过 任务编号 V版本”。多件工作等待时需要指定任务。'}
        with self.store.connect(write=True) as db:
            db.execute('INSERT OR IGNORE INTO team_inbound VALUES (?,?,?,?)', (scope, fingerprint, encoded(reply), stamp()))
            saved = db.execute('SELECT * FROM team_inbound WHERE scope=?', (scope,)).fetchone()
            require(saved['fingerprint'] == fingerprint, '消息编号冲突。', 409)
            return json.loads(saved['response'])

    @staticmethod
    def _action_reply(result):
        return {'reply': f'已按当前身份处理：{result["title"]}，状态 {result["state"]}，V{result["version"]}。'}

    def _bind(self, data, scope, fingerprint, code):
        reply = {'reply': '绑定码无效、过期或微信已被占用，请在账号设置中重新申请。'}
        throttle = digest('binding:' + data['account_id'] + ':' + data['peer_id'])
        with self.store.connect(write=True) as db:
            saved = db.execute('SELECT * FROM team_inbound WHERE scope=?', (scope,)).fetchone()
            if saved:
                require(saved['fingerprint'] == fingerprint, '消息编号冲突。', 409)
                return json.loads(saved['response'])
            limit = db.execute('SELECT * FROM team_login_limits WHERE scope=?', (throttle,)).fetchone()
            require(not limit or limit['until_at'] <= stamp() or limit['failures'] < 5, '绑定尝试过多，请10分钟后重试。', 429)
            challenge = db.execute('SELECT c.* FROM team_binding_codes c JOIN team_users u ON u.id=c.user_id '
                                   'WHERE c.code_hash=? AND c.expires_at>? AND u.active=1', (digest(code), stamp())).fetchone()
            existing = db.execute('SELECT user_id FROM team_bindings WHERE account_id=? AND peer_id=?', (data['account_id'], data['peer_id'])).fetchone()
            if challenge and (not existing or existing['user_id'] == challenge['user_id']):
                user_id = challenge['user_id']
                db.execute('DELETE FROM team_bindings WHERE user_id=?', (user_id,))
                db.execute('INSERT INTO team_bindings VALUES (?,?,?,?,?,?)',
                           (user_id, data['account_id'], data['peer_id'], secrets.token_hex(16), challenge['push_enabled'], stamp()))
                db.execute('DELETE FROM team_binding_codes WHERE user_id=?', (user_id,))
                db.execute("UPDATE team_notifications SET state='cancelled',detail='微信绑定发生变化。' WHERE user_id=? AND state='pending'", (user_id,))
                db.execute('DELETE FROM team_login_limits WHERE scope=?', (throttle,))
                self.store.audit(db, user_id, 'verify_wechat_binding', 'user', user_id)
                reply = {'reply': '已验证并绑定你的员工身份。可发送“我的任务”查看工作。'}
            else:
                db.execute('INSERT INTO team_login_limits VALUES (?,1,?) ON CONFLICT(scope) DO UPDATE SET '
                           'failures=CASE WHEN until_at<=? THEN 1 ELSE failures+1 END,until_at=excluded.until_at',
                           (throttle, stamp(now() + timedelta(minutes=10)), stamp()))
            db.execute('INSERT INTO team_inbound VALUES (?,?,?,?)', (scope, fingerprint, encoded(reply), stamp()))
            return reply


class WechatSender:
    """Only configured, already-bound gateway profiles can send real notifications."""
    def __init__(self, bridges):
        self.bridges = list(bridges)

    def __call__(self, binding, message):
        route = {'account_id': binding['account_id'], 'peer_id': binding['peer_id']}
        for bridge in self.bridges:
            source = bridge.route()
            if bridge.enabled and source and source['account_id'] == route['account_id']:
                if not bridge.gateway_listening():
                    return 'blocked', '微信网关尚未运行，站内通知可用。'
                # The team binding was verified by a signed inbound message and a
                # one-use user challenge. Keep the pilot's fixed recipient intact;
                # create a scoped adapter that rechecks only the gateway account.
                from zhishi.channels.wechat.bridge import WechatBridge
                class VerifiedRecipientBridge(WechatBridge):
                    def route(self):
                        current = super().route()
                        return dict(route) if current and current['account_id'] == route['account_id'] else None
                scoped = VerifiedRecipientBridge(bridge.root, bridge.profile, bridge.port)
                return scoped.send_package(message, [], expected_route=route)
        return 'blocked', '没有与已验证绑定匹配的发送通道，站内通知可用。'
