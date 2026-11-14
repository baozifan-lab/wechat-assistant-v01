"""Authenticated API boundary. No legacy pilot endpoint is exposed in team mode."""
import base64
from http.cookies import SimpleCookie
import hmac
import json
import re
import ssl
import urllib.parse

from zhishi.core.observability import log_failure
from zhishi.web.handler import Handler, logger
from .errors import TeamError, require
from .security import csrf


class TeamHandler(Handler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    def _diagnostic_context(self):
        context = super()._diagnostic_context()
        context['route'] = 'team_api' if getattr(self, 'path', '').startswith('/api/team/') else 'other'
        return context

    @property
    def app(self):
        return self.pilot

    def guard(self):
        host = self.headers.get('Host', '')
        require(host in self.server.allowed_hosts, '访问主机不在允许范围。', 403)
        origin = self.headers.get('Origin')
        scheme = 'https' if self.server.tls_enabled else 'http'
        require(not origin or origin == scheme + '://' + host, '请求来源不匹配。', 403)

    def credentials(self, mutation=False):
        authorization = self.headers.get('Authorization', '')
        cookie_mode = not authorization
        if authorization:
            require(authorization.startswith('Bearer '), '认证方式无效。', 401)
            token = authorization[7:]
        else:
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
            except Exception:
                raise TeamError('会话格式无效。', 401) from None
            token = cookies['zhishi_team'].value if 'zhishi_team' in cookies else ''
        user = self.app.auth.verify(token)
        if mutation and cookie_mode:
            require(hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf(token)), '缺少有效CSRF凭证。', 403)
        return user, token

    def cookie(self, token, *, clear=False):
        self._set_cookie = 'zhishi_team=' + token + '; Path=/api/team; HttpOnly; SameSite=Strict; Max-Age=' + ('0' if clear else '86400')
        if self.server.tls_enabled:
            self._set_cookie += '; Secure'

    def end_headers(self):
        if getattr(self, '_set_cookie', None):
            self.send_header('Set-Cookie', self._set_cookie)
            self._set_cookie = None
        super().end_headers()

    def read_json(self, maximum=16000):
        require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', '仅接受JSON。', 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise TeamError('请求长度无效。') from None
        require(0 < length <= maximum, '请求大小不符合要求。', 413)
        def invalid(value):
            raise ValueError('nonfinite')
        try:
            data = json.loads(self.rfile.read(length), parse_constant=invalid)
        except (ValueError, UnicodeError):
            raise TeamError('JSON格式无效。') from None
        require(isinstance(data, dict), '请求须为JSON对象。', 400)
        return data

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, mutation):
        try:
            self.guard()
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if not mutation and path == '/':
                return self.output({'service': '知事多人协作后端', 'mode': 'team', 'api': '/api/team', 'ui': False})
            if mutation and path == '/api/team/wechat/inbound':
                require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', '仅接受JSON。', 415)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise TeamError('请求长度无效。') from None
                require(0 < size <= 16000, '桥接消息过大。', 413)
                raw = self.rfile.read(size)
                self.app.wechat.verify_signature(raw, self.headers.get('X-Team-Bridge-Time'), self.headers.get('X-Team-Bridge-Signature'))
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise TeamError('桥接消息格式无效。') from None
                require(isinstance(payload, dict), '桥接消息须为对象。', 400)
                return self.output(self.app.wechat.receive(payload))
            if mutation and path == '/api/team/auth/login':
                data = self.read_json()
                result = self.app.auth.login(data.get('username'), data.get('password'), self.client_address[0])
                self.cookie(result['access_token'])
                result['csrf_token'] = csrf(result['access_token'])
                return self.output(result)
            user, token = self.credentials(mutation)
            user_id = user['id']
            if not mutation:
                if path == '/api/team/me':
                    return self.output({'user': user, 'csrf_token': csrf(token)})
                if path == '/api/team/health':
                    health = self.app.health(user_id)
                    return self.output(health, 200 if health['status'] == 'ok' else 503)
                if path == '/api/team/users':
                    return self.output({'users': self.app.auth.users(user_id)})
                if path == '/api/team/projects':
                    return self.output({'projects': self.app.service.projects(user_id)})
                if path == '/api/team/tasks':
                    return self.output({'tasks': self.app.service.tasks(user_id, query.get('project_id', [None])[0])})
                if path == '/api/team/flows':
                    return self.output({'flows': self.app.flows(user_id)})
                if path == '/api/team/notifications':
                    return self.output({'notifications': self.app.service.notifications(user_id)})
                if path == '/api/team/messages':
                    return self.output({'messages': self.app.messages(user_id, query.get('task_id', [None])[0])})
                if path == '/api/team/wechat/binding':
                    return self.output(self.app.wechat.binding(user_id))
                if path == '/api/team/audit':
                    return self.output({'logs': self.app.service.audit_logs(user_id, query.get('limit', [100])[0])})
                if path == '/api/team/legacy':
                    return self.output(self.app.legacy_inventory(user_id))
                match = re.fullmatch(r'/api/team/(projects|tasks|flows|artifacts)/([a-z]_[a-f0-9]{24})', path)
                if match:
                    kind, item = match.groups()
                    if kind == 'artifacts':
                        name, content = self.app.service.download(user_id, item)
                        return self.output(content, kind='application/octet-stream', filename=name)
                    method = {'projects': self.app.service.project_detail, 'tasks': self.app.service.detail, 'flows': self.app.service.flow_detail}[kind]
                    return self.output(method(user_id, item))
                raise TeamError('接口不存在。', 404)
            data = self.read_json(14_000_000 if path.endswith('/artifacts') else 16000)
            if path == '/api/team/auth/logout':
                self.app.auth.logout(token)
                self.cookie('', clear=True)
                return self.output({'ok': True})
            if path == '/api/team/auth/password':
                return self.output(self.app.auth.change_password(user_id, data.get('old_password'), data.get('new_password')))
            if path == '/api/team/users':
                return self.output(self.app.auth.create_user(user_id, data), 201)
            match = re.fullmatch(r'/api/team/users/(\d+)/(update|reset-password)', path)
            if match:
                target = int(match[1])
                if match[2] == 'reset-password':
                    require(target != user_id, '本人改密请使用auth/password。', 400)
                    return self.output(self.app.auth.change_password(user_id, None, data.get('new_password'), target=target))
                return self.output(self.app.auth.update_user(user_id, target, data))
            if path == '/api/team/projects':
                return self.output(self.app.service.create_project(user_id, data), 201)
            match = re.fullmatch(r'/api/team/projects/(p_[a-f0-9]{24})/members/(\d+)', path)
            if match:
                return self.output(self.app.service.set_member(user_id, match[1], int(match[2]), data))
            if path == '/api/team/tasks':
                return self.output(self.app.service.create_task(user_id, data), 201)
            match = re.fullmatch(r'/api/team/tasks/(t_[a-f0-9]{24})/(accept|submit|approve|reject|assign|snooze|cancel|artifacts|jobs)', path)
            if match:
                item, action = match.groups()
                if action == 'artifacts':
                    try:
                        content = base64.b64decode(data.get('content_base64', ''), validate=True)
                    except (ValueError, TypeError):
                        raise TeamError('文件编码无效。') from None
                    return self.output(self.app.service.upload(user_id, item, data, content), 201)
                if action == 'jobs':
                    return self.output(self.app.jobs.enqueue(user_id, item, data), 202)
                return self.output(self.app.service.task_action(user_id, item, action, data))
            if path == '/api/team/flows':
                return self.output(self.app.service.start_flow(user_id, data), 201)
            match = re.fullmatch(r'/api/team/flows/(f_[a-f0-9]{24})/(approve|reject|cancel)', path)
            if match:
                return self.output(self.app.service.flow_action(user_id, match[1], match[2], data))
            match = re.fullmatch(r'/api/team/notifications/(n_[a-f0-9]{24})/(read|retry)', path)
            if match:
                return self.output(self.app.service.read_notification(user_id, match[1]) if match[2] == 'read'
                                   else self.app.notifier.retry(user_id, match[1], data))
            if path == '/api/team/wechat/binding-code':
                return self.output(self.app.wechat.begin_binding(user_id, data))
            if path == '/api/team/wechat/unbind':
                return self.output(self.app.wechat.unbind(user_id, data))
            if path == '/api/team/messages':
                return self.output(self.app.post_message(user_id, data), 201)
            raise TeamError('接口不存在。', 404)
        except TeamError as exc:
            self.output({'error': str(exc), 'code': exc.code, 'request_id': self.request_id}, exc.status)
        except (ValueError, TypeError, OverflowError):
            self.output({'error': '请求参数无效。', 'code': 'invalid_request', 'request_id': self.request_id}, 400)
        except Exception:
            log_failure(logger, 'team_request_failed', **self._diagnostic_context())
            self.output({'error': '服务暂时出错，请凭请求编号核对。', 'request_id': self.request_id}, 500)
