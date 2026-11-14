"""Loopback HTTP boundary: authentication, request validation and route dispatch."""
import json
import logging
import mimetypes
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from zhishi.config import ROOT, WEB_ROOT
from zhishi.core.clock import stamp
from zhishi.core.health import health_status
from zhishi.core.observability import log_failure
from zhishi.service import Pilot

STATIC_FILES = {
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/admin.css": ("admin.css", "text/css; charset=utf-8"),
    **{f"/static/{name}.js": (f"{name}.js", "text/javascript; charset=utf-8")
       for name in ("api", "ui", "app", "workbench", "accounts", "navigation", "workflow", "two_stage", "abc", "ai_dispatch", "admin")},
}

# Only fixed route names become metric labels. Queries can contain access tokens.
API_ROUTES = frozenset('''/api/health /api/state /api/chat /api/settings
    /api/accounts /api/accounts/save /api/accounts/connect /api/accounts/pause /api/accounts/delete /api/accounts/restore /api/accounts/qr
    /api/console /api/console/reminder /api/console/chat /api/console/legacy-action /api/console/invoice/start /api/console/invoice/asset /api/console/task/close
    /api/console/task/create /api/console/task/update /api/console/file/replace /api/console/file/version
    /api/workflows /api/two-stage /api/abc /api/abc/asset /api/two-stage/asset /api/workflow/asset
    /api/wechat/login /api/wechat/inbound /api/wechat/connect /api/wechat2/status /api/wechat2/connect
    /api/ai-dispatch /api/ai-dispatch/preview /api/ai-dispatch/asset /api/ai-dispatch/start
    /api/ai-dispatch/reply /api/ai-dispatch/connect-wechat /api/ai-dispatch/retry-reply
    /api/ai-dispatch/model /api/ai-dispatch/model/test'''.split())
logger = logging.getLogger('zhishi.http')

class Handler(BaseHTTPRequestHandler):
    pilot: Pilot
    token: str

    def _diagnostic_context(self):
        path = getattr(self, 'path', '').partition('?')[0]
        return {'request_id': self.request_id, 'route': path if path in API_ROUTES else 'other',
                'method': self.command if self.command in ('GET', 'POST', 'HEAD') else 'other'}

    def handle_one_request(self):
        self.request_id = secrets.token_hex(16)
        self.response_status = None
        self.command = None
        started = time.perf_counter()
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:
            log_failure(logger, 'request_failed', **self._diagnostic_context())
            self.close_connection = True
            if self.response_status is None:
                try:
                    self.output({'error': '服务暂时出错，请稍后重试。'}, 500)
                except OSError:
                    pass
        finally:
            if self.response_status is not None:
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                context = self._diagnostic_context()
                metrics = getattr(self.server, 'metrics', None)
                if metrics:
                    metrics.record(context['route'], self.response_status, elapsed)
                if elapsed >= 1000:
                    logger.info('request_slow', extra={**context, 'status': self.response_status, 'duration_ms': elapsed})

    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)

    def end_headers(self):
        self.send_header('X-Request-ID', self.request_id)
        super().end_headers()

    def log_message(self, fmt, *args):
        # Avoid logging user input, task titles and any tokens.
        return

    def output(self, value, status=200, kind="application/json; charset=utf-8", filename=None):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        return self.headers.get("Host", "") in expected and secrets.compare_digest(
            self.headers.get("X-Pilot-Token", ""), self.token)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "") not in expected:
            return self.output({"error": "仅允许本机访问。"}, 403)
        if path in ("/", "/wechat", "/work", "/two-stage", "/abc", "/ai-workflow", "/wechat2", "/accounts"):
            return self.output((WEB_ROOT / "index.html").read_bytes().replace(b"__PILOT_TOKEN__", self.token.encode()), kind="text/html; charset=utf-8")
        if path == '/api/health' and self.authorized():
            health = health_status(self.pilot)
            metrics = getattr(self.server, 'metrics', None)
            health['requests'] = metrics.snapshot() if metrics else {}
            return self.output(health, 200 if health['status'] == 'ok' else 503)
        if path == '/api/console' and self.authorized():
            return self.output(self.pilot.workbench.state())
        if path == '/api/console/file/version' and self.authorized():
            try:
                ident = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('id', [''])[0]
                file, name = self.pilot.corrections.asset(ident)
                return self.output(file.read_bytes(), kind=mimetypes.guess_type(name)[0] or 'application/octet-stream', filename=name)
            except (OSError, ValueError):
                return self.output({'error':'找不到可下载的文件版本。'},404)
        if path == '/api/accounts' and self.authorized():
            return self.output(self.pilot.accounts.state())
        if path == '/api/accounts/qr' and self.authorized():
            try:
                ident = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('id', [''])[0]
                return self.output(self.pilot.accounts.qr_file(ident).read_bytes(), kind='image/png')
            except (ValueError, OSError):
                return self.output({'error': '二维码尚未生成或已经失效。'}, 404)
        if path == '/api/console/invoice/asset' and self.authorized() and self.pilot.invoice_demo:
            from pathlib import Path
            from zhishi.workflows.invoice_demo import file_hash
            code = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('code', [''])[0]
            try:
                with self.pilot.connect() as db:
                    row = db.execute('SELECT detail FROM invoice_demo_runs WHERE code=?', (code,)).fetchone()
                if not row:
                    raise ValueError('找不到任务。')
                detail = json.loads(row['detail'])
                file = Path(detail['file']).resolve()
                folder = (self.pilot.invoice_demo.output / code).resolve()
                if not folder.is_relative_to(self.pilot.invoice_demo.output.resolve()) or not file.is_relative_to(folder) or file.suffix != '.xlsx' or file_hash(file) != detail['sha256']:
                    raise ValueError('成果文件校验失败。')
                return self.output(file.read_bytes(), kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=file.name)
            except (OSError, ValueError, KeyError, TypeError):
                return self.output({'error':'成果文件不可用或校验未通过。'},404)
        if path == '/api/ai-dispatch' and self.authorized() and self.pilot.ai_dispatch:
            return self.output(self.pilot.ai_dispatch.state())
        if path == '/api/ai-dispatch/preview' and self.authorized() and self.pilot.ai_dispatch:
            try:
                return self.output(self.pilot.ai_dispatch.message_preview())
            except ValueError as error:
                return self.output({'error':str(error)},400)
        if path == '/api/ai-dispatch/asset' and self.authorized() and self.pilot.ai_dispatch:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                run = self.pilot.ai_dispatch.row(query.get('code', [''])[0])
                assets = self.pilot.ai_dispatch.skills.attachments(run)
                if not assets:
                    raise ValueError('尚无成果文件。')
                file = assets[0]
                return self.output(file.read_bytes(), kind='text/csv; charset=utf-8', filename=file.name)
            except (OSError, ValueError):
                return self.output({'error': '成果不可用或校验未通过。'}, 404)
        if path == '/wechat2-qr.png':
            file = ROOT / 'data' / 'employee2-wechat' / 'wechat-login.png'
            if file.exists():
                return self.output(file.read_bytes(), kind='image/png')
            return self.output({'error':'二维码尚未生成'}, 404)
        if path == '/api/wechat2/status' and self.authorized():
            t = self.pilot.employee2_transport
            try:
                state = json.loads((ROOT/'data'/'employee2-wechat'/'wechat-login.json').read_text('utf-8'))
            except (OSError, ValueError):
                state = {'state':'not_started','detail':'尚未开始连接。'}
            return self.output({'login':state, 'channel':t.status() if t else {}})
        if path in STATIC_FILES:
            name, kind = STATIC_FILES[path]
            return self.output((WEB_ROOT / 'static' / name).read_bytes(), kind=kind)
        if path == "/api/state" and self.authorized():
            return self.output(self.pilot.state())
        if path == '/api/workflows' and self.authorized() and self.pilot.workflow:
            return self.output(self.pilot.workflow.state())
        if path == '/api/two-stage' and self.authorized() and self.pilot.two_stage:
            return self.output(self.pilot.two_stage.state())
        if path == '/api/abc' and self.authorized() and self.pilot.abc:
            return self.output(self.pilot.abc.state())
        if path == '/api/abc/asset' and self.pilot.abc:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                file = self.pilot.abc.asset(query.get('uid',[''])[0],query.get('stage',[''])[0],
                                            query.get('name',[''])[0],query.get('access',[''])[0])
                return self.output(file.read_bytes(),kind=mimetypes.guess_type(file.name)[0] or 'application/octet-stream',
                                   filename=None if file.suffix=='.png' else file.name)
            except (OSError,ValueError):
                return self.output({'error':'成果文件不可用或访问凭证无效。'},404)
        if path == '/api/two-stage/asset' and self.pilot.two_stage:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                file = self.pilot.two_stage.asset(query.get('uid',[''])[0],query.get('stage',[''])[0],
                                                 query.get('name',[''])[0],query.get('access',[''])[0])
                return self.output(file.read_bytes(),kind=mimetypes.guess_type(file.name)[0] or 'application/octet-stream',
                                   filename=None if file.suffix=='.png' else file.name)
            except (OSError,ValueError):
                return self.output({'error':'验收文件不可用或访问凭证无效。'},404)
        if path == '/api/workflow/asset' and self.pilot.workflow:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                file = self.pilot.workflow.asset(query.get('uid',[''])[0], query.get('name',[''])[0], query.get('access',[''])[0])
                return self.output(file.read_bytes(), kind=mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=None if file.suffix=='.png' else file.name)
            except (OSError, ValueError):
                return self.output({'error':'文件不可用或访问凭证无效。'}, 404)
        if path == '/api/wechat/login' and self.authorized():
            try:
                state = json.loads((ROOT / 'data' / 'wechat-login.json').read_text('utf-8'))
            except (OSError, ValueError):
                state = {'state':'not_started', 'detail':'微信尚未开始连接。'}
            return self.output(state)
        if path == '/wechat-login.png':
            image_path = ROOT / 'data' / 'wechat-login.png'
            if image_path.exists():
                return self.output(image_path.read_bytes(), kind='image/png')
        return self.output({"error": "未找到页面或缺少本机访问凭证。"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        bridge_call = path == '/api/wechat/inbound'
        if bridge_call:
            transport = self.pilot.transport
            valid_host = self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            supplied_secret = self.headers.get('X-Wechat-Bridge', '')
            primary_secret = bool(transport and secrets.compare_digest(supplied_secret, transport.secret))
            extra_secret = bool(not primary_secret and getattr(self.pilot, 'accounts', None) and self.pilot.accounts.accepts_secret(supplied_secret))
            if not valid_host or not (primary_secret or extra_secret):
                return self.output({'error':'微信桥接凭证无效。'}, 403)
        elif not self.authorized():
            return self.output({"error": "缺少本机访问凭证。"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.output({"error": "请求来源不匹配。"}, 403)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.output({"error": "仅接受 JSON。"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            maximum = 29 * 1024 * 1024 if path == '/api/console/file/replace' else 16000
            if not 0 < length <= maximum:
                raise ValueError("请求体大小不正确。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求须为一个对象。")
            if bridge_call:
                second = self.pilot.employee2_transport
                role = ('primary' if self.pilot.transport and self.pilot.transport.authorize(data) else
                        'employee2' if second and second.authorize(data) else None) if primary_secret else None
                if extra_secret and not primary_secret:
                    try:
                        result = self.pilot.accounts.inbound(data, supplied_secret)
                    except ValueError as error:
                        return self.output({'error': str(error)}, 403)
                elif role is None:
                    return self.output({'error':'微信账号或会话未获准访问待办。'}, 403)
                else:
                    result = self.pilot.chat(data.get('text'), self.pilot.transport.request_id(data), wechat_role=role)
                    with self.pilot.connect() as db:
                        db.execute("INSERT OR REPLACE INTO settings VALUES ('wechat_last_inbound',?)", (stamp(),))
            elif path == '/api/accounts/save':
                result = self.pilot.accounts.save(data)
            elif path == '/api/accounts/connect':
                result = self.pilot.accounts.connect(data)
            elif path == '/api/accounts/pause':
                result = self.pilot.accounts.pause(data)
            elif path == '/api/accounts/delete':
                result = self.pilot.accounts.change_membership(data)
            elif path == '/api/accounts/restore':
                result = self.pilot.accounts.change_membership(data, restore=True)
            elif path == "/api/chat":
                result = self.pilot.chat(data.get("text"), data.get("request_id"))
            elif path == '/api/console/reminder':
                result = self.pilot.workbench.reminder_action(data)
            elif path == '/api/console/task/create':
                result = self.pilot.work_orders.create(data)
            elif path == '/api/console/task/update':
                result = self.pilot.work_orders.update(data)
            elif path == '/api/console/file/replace':
                result = self.pilot.corrections.replace(data)
            elif path == '/api/console/invoice/start' and self.pilot.invoice_demo:
                result = self.pilot.workbench.start_invoice(data.get('request_id'))
            elif path == '/api/console/task/close':
                result = self.pilot.workbench.close_task(data)
            elif path == '/api/console/chat':
                result = self.pilot.workbench.chat(data)
            elif path == '/api/console/legacy-action':
                result = self.pilot.workbench.legacy_action(data)
            elif path == "/api/settings":
                self.pilot.configure(data)
                result = {"ok": True}
            elif path == '/api/ai-dispatch/start' and self.pilot.ai_dispatch:
                code = self.pilot.ai_dispatch.start(data.get('task_id'), data.get('request_id'), data.get('display_name'))
                result = {'code': code}
            elif path == '/api/ai-dispatch/reply' and self.pilot.ai_dispatch:
                result = {'reply': self.pilot.ai_dispatch.submit(data.get('code'), data.get('role'), data.get('text'), data.get('request_id'))}
            elif path == '/api/ai-dispatch/connect-wechat' and self.pilot.ai_dispatch:
                result = {'reply': self.pilot.ai_dispatch.connect_wechat(data.get('code'))}
            elif path == '/api/ai-dispatch/retry-reply' and self.pilot.ai_dispatch:
                result = {'reply': self.pilot.ai_dispatch.retry_reply(data.get('code'), data.get('request_id'))}
            elif path == '/api/ai-dispatch/model' and self.pilot.ai_dispatch:
                result = {'model': self.pilot.ai_dispatch.model.configure(data)}
            elif path == '/api/ai-dispatch/model/test' and self.pilot.ai_dispatch:
                result = self.pilot.ai_dispatch.model.test_connection()
            elif path == '/api/wechat/connect' and self.pilot.transport:
                result = self.pilot.transport.start_login()
            elif path == '/api/wechat2/connect' and self.pilot.employee2_transport:
                result = self.pilot.employee2_transport.start_login()
            else:
                return self.output({"error": "未找到接口。"}, 404)
            self.output(result)
        except (ValueError, TypeError, OverflowError) as error:
            self.output({"error": str(error)}, 400)
        except Exception:
            log_failure(logger, 'request_failed', **self._diagnostic_context())
            self.output({"error": "服务暂时出错，未确认的操作请检查待办后再重试。"}, 500)
