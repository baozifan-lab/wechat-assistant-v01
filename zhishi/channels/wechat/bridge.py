"""Official OpenClaw channel transport. Credentials stay in its isolated profile."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


class WechatBridge:
    def __init__(self, root, profile=None):
        self.root = Path(root)
        self.profile = Path(profile) if profile else Path.home() / ".openclaw-zhishi-pilot"
        self.route_path = self.profile / "zhishi-binding.json"
        self.secret_path = self.profile / "zhishi-bridge.secret"
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            with self.secret_path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(48))
            self.secret_path.chmod(0o600)
        except FileExistsError:
            pass
        self.secret = self.secret_path.read_text("utf-8").strip()
        self.login_lock = threading.Lock()
        self.login_process = None
        self.send_lock = threading.Lock()
        self.gateway_lock = threading.Lock()
        self.gateway_recovery = None
        self.gateway_last_start = None

    def start_login(self):
        with self.login_lock:
            if self.login_process is not None and self.login_process.poll() is None:
                self.request_refresh()
                return {'ok': True, 'detail': '正在结束旧的扫码请求并申请新二维码。'}
            self.login_process = subprocess.Popen([sys.executable, '-X', 'utf8', str(self.root / 'wechat_connect.py')],
                cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {'ok': True, 'detail': '正在生成微信授权二维码。'}

    def request_refresh(self):
        data = self.root / 'data'
        data.mkdir(exist_ok=True)
        (data / 'wechat-refresh.request').write_text(secrets.token_hex(16), encoding='ascii')

    @property
    def enabled(self):
        return (self.profile / "zhishi-bridge.enabled").exists()

    def route(self):
        try:
            value = json.loads(self.route_path.read_text("utf-8"))
            if isinstance(value, dict) and value.get("account_id") and value.get("peer_id"):
                return value
        except (OSError, ValueError):
            pass
        return None

    def authorize(self, data):
        route = self.route()
        return bool(self.enabled and route and data.get("account_id") == route["account_id"]
                    and data.get("peer_id") == route["peer_id"] and not data.get("is_group"))

    @staticmethod
    def request_id(data):
        ident = str(data.get("source_message_id", ""))
        if not ident or len(ident) > 256:
            raise ValueError("微信消息缺少有效编号，未执行操作。")
        scope = json.dumps([data.get("account_id"), data.get("peer_id"), ident], ensure_ascii=False)
        return "wechat-" + hashlib.sha256(scope.encode()).hexdigest()

    def send(self, text):
        return self.send_package(text, [])

    def send_package(self, text, media, *, expected_route=None):
        # Serialize this pilot's CLI sends and stage only approved output files.
        with self.send_lock:
            if expected_route is not None and self.route() != expected_route:
                return 'blocked', '本人微信绑定已变化，未向新的会话发送旧任务材料。'
            staged = []
            for value in media:
                file = Path(value).resolve()
                if not file.is_relative_to((self.root / 'outputs').resolve()) or file.suffix not in {'.xlsx','.png','.csv'}:
                    return 'blocked', '附件不在允许的验收结果范围。'
                checksum = hashlib.sha256(file.read_bytes()).hexdigest()
                target = self.profile / 'media' / 'zhishi-review' / checksum / file.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                    return 'blocked', '微信附件副本校验失败。'
                staged.append(target)
            return self._send(text, staged, expected_route=expected_route)

    def _send(self, text, media, *, expected_route=None):
        route = self.route()
        if expected_route is not None and route != expected_route:
            return 'blocked', '本人微信绑定已变化，未发送。'
        if not self.enabled or not route:
            return "blocked", "微信尚未绑定，未调用发送接口。"
        node_dir = self.root / ".runtime" / "node-v24.22.1-win-x64"
        node = node_dir / "node.exe"
        cli = self.root / ".runtime" / "openclaw" / "node_modules" / "openclaw" / "openclaw.mjs"
        if not node.exists() or not cli.exists():
            return "blocked", "本机微信运行环境缺失，未调用发送接口。"
        env = os.environ.copy()
        env.update({"OPENCLAW_STATE_DIR": str(self.profile),
                    "OPENCLAW_CONFIG_PATH": str(self.profile / "openclaw.json"),
                    "PATH": str(node_dir) + os.pathsep + env.get("PATH", "")})
        # The installed standalone `message send` activates another runtime and can
        # replace the live Gateway's state-ownership lease. Delegate to the existing
        # Gateway instead: its CLI RPC client opens shared state read-only.
        payload = {"channel":"openclaw-weixin", "accountId":route["account_id"],
                   "to":route["peer_id"], "message":text,
                   "idempotencyKey":"zhishi-"+secrets.token_hex(16)}
        if media:
            payload['mediaUrls'] = [str(file) for file in media]
        command = [str(node), str(cli), "gateway", "call", "send",
                   "--params", json.dumps(payload,ensure_ascii=False),
                   "--port", "18791", "--expect-url", "ws://127.0.0.1:18791",
                   "--timeout", "120000" if media else "60000", "--json"]
        try:
            result = subprocess.run(command, env=env, cwd=self.root, capture_output=True,
                                    timeout=150 if media else 90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return "unknown", "微信发送等待超时，结果未知；为避免重复，不自动重发。"
        except OSError:
            return "blocked", "无法启动微信发送程序，未调用发送接口。"
        # Do not expose raw CLI output: it may contain channel/account diagnostics.
        if result.returncode:
            return "unknown", "微信发送程序返回异常，结果未确认；请先核对手机。"
        return "submitted_unverified", "已提交微信通道，尚无手机可见／已读确认。"

    @staticmethod
    def gateway_listening():
        try:
            with socket.create_connection(('127.0.0.1',18791),timeout=0.25):
                return True
        except OSError:
            return False

    def ensure_gateway(self):
        """Recover only this already-authorized pilot; never start a QR/login flow."""
        if not self.enabled or not self.route() or self.gateway_listening():
            return
        with self.gateway_lock:
            if self.gateway_recovery is not None and self.gateway_recovery.poll() is None:
                return
            if self.login_process is not None and self.login_process.poll() is None:
                return
            current = time.monotonic()
            if self.gateway_last_start is not None and current-self.gateway_last_start<90:
                return
            self.gateway_last_start = current
            self.gateway_recovery = subprocess.Popen(
                [sys.executable,'-X','utf8',str(self.root/'wechat_connect.py'),'--use-saved'],
                cwd=self.root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def status(self):
        return {"enabled": self.enabled, "bound": bool(self.route()),
                "plugin_installed": (self.profile / "openclaw.json").exists(),
                "gateway_listening": self.gateway_listening()}
