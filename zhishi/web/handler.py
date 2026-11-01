"""Loopback HTTP boundary: authentication, request validation and route dispatch."""
import json
import mimetypes
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler
from zhishi.config import ROOT, WEB_ROOT
from zhishi.core.clock import stamp
from zhishi.service import Pilot

STATIC_FILES = {
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    **{f"/static/{name}.js": (f"{name}.js", "text/javascript; charset=utf-8")
       for name in ("api", "ui", "app", "workflow", "two_stage", "abc")},
}

class Handler(BaseHTTPRequestHandler):
    pilot: Pilot
    token: str

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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
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
        if path in ("/", "/wechat", "/work", "/two-stage", "/abc"):
            return self.output((WEB_ROOT / "index.html").read_bytes().replace(b"__PILOT_TOKEN__", self.token.encode()), kind="text/html; charset=utf-8")
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
            if not transport or not valid_host or not secrets.compare_digest(self.headers.get('X-Wechat-Bridge', ''), transport.secret):
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
            if not 0 < length <= 16000:
                raise ValueError("请求体大小不正确。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求须为一个对象。")
            if bridge_call:
                if not self.pilot.transport.authorize(data):
                    return self.output({'error':'微信账号或会话未获准访问待办。'}, 403)
                result = self.pilot.chat(data.get('text'), self.pilot.transport.request_id(data))
                with self.pilot.connect() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('wechat_last_inbound',?)", (stamp(),))
            elif path == "/api/chat":
                result = self.pilot.chat(data.get("text"), data.get("request_id"))
            elif path == "/api/settings":
                self.pilot.configure(data)
                result = {"ok": True}
            elif path == '/api/wechat/connect' and self.pilot.transport:
                result = self.pilot.transport.start_login()
            else:
                return self.output({"error": "未找到接口。"}, 404)
            self.output(result)
        except (ValueError, TypeError, OverflowError) as error:
            self.output({"error": str(error)}, 400)
        except Exception:
            self.output({"error": "服务暂时出错，未确认的操作请检查待办后再重试。"}, 500)
