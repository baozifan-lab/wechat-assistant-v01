"""Run official QR login, keep a current local QR, then configure the pilot binding."""
from __future__ import annotations

import argparse
import json
import os
import re
import queue
import secrets
import struct
import subprocess
import threading
import time
import socket
import zlib
from pathlib import Path

from .bridge import WechatBridge
from zhishi.config import ROOT

PROFILE = Path.home() / '.openclaw-zhishi-pilot'
NODE_DIR = ROOT / '.runtime' / 'node-v24.22.1-win-x64'
NODE = NODE_DIR / 'node.exe'
CLI = ROOT / '.runtime' / 'openclaw' / 'node_modules' / 'openclaw' / 'openclaw.mjs'
DATA = ROOT / 'data'


def write_status(state, **details):
    DATA.mkdir(exist_ok=True)
    temp = DATA / 'wechat-login.tmp'
    temp.write_text(json.dumps({'state': state, **details}, ensure_ascii=False), encoding='utf-8')
    temp.replace(DATA / 'wechat-login.json')
    print(state, flush=True)


def refresh_generation():
    try:
        return (DATA / 'wechat-refresh.request').read_text('ascii').strip()
    except OSError:
        return ''


def stop_login_process(process):
    """Stop only the official login child owned by this supervisor, including its child Node."""
    if process.poll() is not None:
        return
    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def login_attempt(generation):
    write_status('starting', detail='正在向微信申请新的授权二维码，请稍候。')
    process = subprocess.Popen([str(NODE), str(CLI), 'channels', 'login', '--channel', 'openclaw-weixin'],
                               env=environment(), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()

    def read_lines():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    started = time.monotonic()
    has_qr = False
    try:
        while True:
            if refresh_generation() != generation:
                write_status('refreshing', detail='正在取消旧二维码并重新申请。')
                stop_login_process(process)
                return 'refresh'
            if not has_qr and time.monotonic() - started > 75:
                stop_login_process(process)
                write_status('error', detail='二维码申请超时。请点击刷新重试，或检查本机网络。')
                return 'error'
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            match = re.search(r'https://liteapp\.weixin\.qq\.com/[^\s\x1b]+', line)
            if match:
                url = match[0]
                qr_image(url)
                has_qr = True
                write_status('waiting_scan', qr_url=url, updated_at=int(time.time()*1000),
                             qr_version=secrets.token_hex(8), detail='新二维码已生成，请用本人微信扫一扫并确认授权。')
            elif '已扫描' in line or '已扫码' in line:
                write_status('waiting_confirm', detail='已扫码，请在手机上确认连接。')
            elif '过期' in line or '刷新' in line:
                write_status('refreshing', detail='二维码已过期，正在向微信申请新码。')
        code = process.wait(timeout=10)
        if code != 0:
            write_status('expired', detail='本次登录尚未完成，请点击刷新生成新二维码。')
            return 'error'
        return 'confirmed'
    finally:
        if process.poll() is None:
            stop_login_process(process)
        reader.join(timeout=2)
        process.stdout.close()


def environment():
    result = os.environ.copy()
    result.update({'OPENCLAW_STATE_DIR': str(PROFILE), 'OPENCLAW_CONFIG_PATH': str(PROFILE / 'openclaw.json'),
                   'PATH': str(NODE_DIR) + os.pathsep + result.get('PATH', '')})
    return result


def qr_image(url):
    sources = list((PROFILE / 'npm' / 'projects').glob('*/node_modules/qrcode-terminal/vendor/QRCode/index.js'))
    if not sources:
        raise RuntimeError('官方二维码依赖尚未就绪。')
    script = "const Q=require(process.argv[1]);const q=new Q(-1,1);q.addData(process.argv[2]);q.make();process.stdout.write(JSON.stringify(q.modules));"
    result = subprocess.run([str(NODE), '-e', script, str(sources[0]), url], check=True, capture_output=True,
                            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    matrix = json.loads(result.stdout)
    scale, border = 8, 4
    size = (len(matrix) + border * 2) * scale
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            r, c = y // scale - border, x // scale - border
            raw.append(0 if 0 <= r < len(matrix) and 0 <= c < len(matrix) and matrix[r][c] else 255)

    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data) & 0xffffffff)

    image = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', size, size, 8, 0, 0, 0, 0))
             + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    DATA.mkdir(exist_ok=True)
    temp = DATA / 'wechat-login.tmp.png'
    temp.write_bytes(image)
    temp.replace(DATA / 'wechat-login.png')


def prepare_binding():
    accounts = PROFILE / 'openclaw-weixin' / 'accounts.json'
    ids = json.loads(accounts.read_text('utf-8'))
    if not isinstance(ids, list) or not ids:
        raise RuntimeError('尚未找到官方插件保存的微信账号。')
    account = ids[-1]
    if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]+', account):
        raise RuntimeError('微信账号编号格式不正确。')
    credentials = json.loads((accounts.parent / 'accounts' / (account + '.json')).read_text('utf-8'))
    peer = credentials.get('userId')
    if not isinstance(peer, str) or not peer:
        raise RuntimeError('扫码账号缺少本人会话标识，暂不启用消息路由。')
    bridge = WechatBridge(ROOT, PROFILE)
    bridge.route_path.write_text(json.dumps({'account_id': account, 'peer_id': peer}, ensure_ascii=False), encoding='utf-8')
    bridge.route_path.chmod(0o600)
    (PROFILE / 'zhishi-bridge.enabled').write_text('enabled', encoding='ascii')
    config_file = PROFILE / 'openclaw.json'
    config = json.loads(config_file.read_text('utf-8'))
    plugins = config.setdefault('plugins', {})
    plugins['allow'] = list(dict.fromkeys(plugins.get('allow', []) + ['openclaw-weixin', 'zhishi-wechat-bridge']))
    paths = plugins.setdefault('load', {}).setdefault('paths', [])
    source = str(ROOT / 'integrations' / 'zhishi-wechat')
    if source not in paths:
        paths.append(source)
    entries = plugins.setdefault('entries', {})
    entries.setdefault('openclaw-weixin', {})['enabled'] = True
    entries['zhishi-wechat-bridge'] = {'enabled': True, 'config': {
        'serviceUrl': 'http://127.0.0.1:8765', 'secretPath': str(bridge.secret_path), 'routePath': str(bridge.route_path)}}
    config.setdefault('session', {})['dmScope'] = 'per-account-channel-peer'
    config.setdefault('gateway', {}).update({'mode': 'local', 'bind': 'loopback', 'port': 18791})
    config.setdefault('tools', {})['profile'] = 'minimal'
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    write_status('bound', detail='本人账号已授权，等待微信收发测试。')


def start_gateway():
    # A second login must not create a second owner of the gateway port.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 18791)) == 0:
            return
    log = (DATA / 'wechat-gateway.log').open('ab')
    try:
        gateway = subprocess.Popen([str(NODE), str(CLI), 'gateway', 'run', '--port', '18791', '--bind', 'loopback'],
                                   env=environment(), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        log.close()
    (DATA / 'wechat-gateway.pid').write_text(str(gateway.pid), encoding='ascii')
    for _ in range(60):
        if gateway.poll() is not None:
            write_status('needs_attention', detail='微信已授权，但通道启动失败，需检查本地运行记录。')
            return
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', 18791)) == 0:
                write_status('gateway_started', detail='微信通道已启动。请在微信发送“我的待办”，验证收发。')
                return
        time.sleep(1)
    write_status('bound', detail='微信已授权，通道仍在启动，请稍后发“我的待办”测试。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--use-saved', action='store_true')
    args = parser.parse_args()
    if args.use_saved:
        if not (PROFILE / 'zhishi-binding.json').exists():
            prepare_binding()
        start_gateway()
        return
    # Only one official login process may own the pilot QR flow.
    import msvcrt
    DATA.mkdir(exist_ok=True)
    guard = (DATA / 'wechat-login.lock').open('a+b')
    guard.seek(0)
    if not guard.read(1):
        guard.write(b'1')
        guard.flush()
    guard.seek(0)
    try:
        msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        guard.close()
        (DATA / 'wechat-refresh.request').write_text(secrets.token_hex(16), encoding='ascii')
        return
    try:
        while True:
            generation = refresh_generation()
            result = login_attempt(generation)
            if result == 'refresh':
                continue
            if result != 'confirmed':
                return
            prepare_binding()
            start_gateway()
            return
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        write_status('error', detail='微信登录流程暂时失败，请点击刷新重试。')
    finally:
        guard.close()


if __name__ == '__main__':
    main()
