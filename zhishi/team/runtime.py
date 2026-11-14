"""Opt-in team mode in the existing application; no pilot recovery is invoked."""
import json
import logging
import os
from pathlib import Path
import ssl
import threading
import time
import secrets

from zhishi.config import DATA_ROOT, ROOT
from zhishi.core.observability import WorkerMonitor, configure_logging, log_failure
from .application import TeamApplication
from .errors import require
from .http import TeamHandler
from .wechat import WechatSender


class TeamWorkers:
    def __init__(self, app, concurrency=2, backup_minutes=0):
        require(type(concurrency) is int and 1 <= concurrency <= 4, '技能并发数须在1至4之间。', 400)
        self.app, self.concurrency = app, concurrency
        require(type(backup_minutes) is int and 0 <= backup_minutes <= 10080, '备份间隔须在0至10080分钟之间。', 400)
        self.backup_minutes = backup_minutes
        self.next_backup = time.monotonic() + backup_minutes * 60
        self.backup_status = {'enabled': bool(backup_minutes), 'last_success': None, 'last_error': None}
        self.stop = threading.Event()
        self.monitor, self.threads = WorkerMonitor(), []
        app.workers = self

    def start(self):
        require(not self.threads and not self.stop.is_set(), '后台任务不能重复启动。', 409)
        jobs = [(f'team-skills-{index}', self.app.jobs.tick) for index in range(self.concurrency)]
        jobs += [('team-notices', self.app.notifier.tick), ('team-reminders', self.app.notifier.reminders)]
        if self.backup_minutes:
            jobs.append(('team-backup', self.backup_tick))
        for name, callback in jobs:
            self.monitor.register(name)
            thread = threading.Thread(target=self.run, args=(name, callback), name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def run(self, name, callback):
        while not self.stop.wait(1):
            self.monitor.run(name, callback)

    def snapshot(self):
        items = self.monitor.snapshot()
        alive = {t.name: t.is_alive() for t in self.threads}
        for name, item in items.items():
            item['alive'] = alive.get(name, False)
        status = 'stopped' if self.stop.is_set() else 'not_started' if not items else 'ok'
        if status == 'ok' and any(not i['alive'] or i['consecutive_errors'] for i in items.values()):
            status = 'degraded'
        elif status == 'ok' and any(not i['calls'] for i in items.values()):
            status = 'starting'
        if status == 'ok' and self.backup_status['last_error']:
            status = 'degraded'
        return {'status': status, 'workers': items, 'backup': dict(self.backup_status)}

    def backup_tick(self):
        if time.monotonic() < self.next_backup:
            return
        from .backup import create_snapshot
        from zhishi.core.clock import now, stamp
        self.next_backup = time.monotonic() + self.backup_minutes * 60
        destination = self.app.store.path.parent / 'backups' / ('team-' + now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        try:
            create_snapshot(self.app.store.path, self.app.service.artifact_root, destination)
            self.backup_status.update(last_success=stamp(), last_error=None)
        except Exception as exc:
            self.backup_status['last_error'] = type(exc).__name__
            raise

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(3)


def create_team_server(app, port=8766, *, host='127.0.0.1', allowed_hosts=None, tls_cert=None, tls_key=None):
    from zhishi.runtime import create_server
    require(host in ('127.0.0.1', 'localhost', '::1') or (tls_cert and tls_key and allowed_hosts),
            '员工设备访问须配置TLS证书、密钥及allowed-host。', 400)
    require(bool(tls_cert) == bool(tls_key), 'TLS证书和密钥必须同时设置。', 400)
    # The current server is IPv4; reject unsupported IPv6 instead of silently exposing it.
    require(host != '::1', '当前请使用127.0.0.1或IPv4监听地址。', 400)
    server = create_server(app, port, handler_class=TeamHandler, host=host)
    server.allowed_hosts = set(allowed_hosts or ()) | {f'127.0.0.1:{server.server_port}', f'localhost:{server.server_port}'}
    server.tls_enabled = bool(tls_cert)
    server.handle_error = lambda request, address: log_failure(logging.getLogger('zhishi.team'), 'team_connection_failed')
    try:
        if tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(tls_cert, tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    except BaseException:
        server.server_close()
        raise
    return server


def run_team(args):
    database = Path(args.database or DATA_ROOT / 'team.sqlite3').resolve()
    require(database != (DATA_ROOT / 'pilot.sqlite3').resolve(), '多人模式请使用独立数据库或迁移副本，不能直接打开正在使用的pilot.sqlite3。', 400)
    require(args.host in ('127.0.0.1', 'localhost') or (args.tls_cert and args.tls_key and args.allowed_host),
            '非本机监听需要TLS和allowed-host。', 400)
    sender = None
    if args.enable_wechat:
        from zhishi.channels.wechat.bridge import WechatBridge
        profiles = json.loads(os.environ.get('TEAM_WECHAT_GATEWAYS', '[]'))
        require(isinstance(profiles, list) and profiles, '启用微信发送前须配置TEAM_WECHAT_GATEWAYS。', 400)
        bridges = []
        for entry in profiles:
            profile = Path(entry['profile']).expanduser().resolve()
            require((profile / 'zhishi-binding.json').is_file() and (profile / 'zhishi-bridge.secret').is_file(),
                    '仅允许使用已经授权的微信配置。', 400)
            bridges.append(WechatBridge(ROOT, profile, int(entry['port'])))
        sender = WechatSender(bridges)
    logger = configure_logging(args.log_directory or database.parent / 'team-logs')
    app = TeamApplication(database, args.artifact_root, bridge_secret=os.environ.get('TEAM_BRIDGE_SECRET'), sender=sender)
    server = create_team_server(app, args.port if args.port is not None else 8766, host=args.host,
                                allowed_hosts=args.allowed_host, tls_cert=args.tls_cert, tls_key=args.tls_key)
    workers = TeamWorkers(app, args.skill_workers, args.backup_interval_minutes)
    try:
        workers.start()
        logger.info('team_service_started')
        scheme = 'https' if args.tls_cert else 'http'
        print(f'知事多人后端：{scheme}://{args.host}:{server.server_port}/api/team', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log_failure(logging.getLogger('zhishi.team'), 'team_service_failed')
        raise
    finally:
        workers.close()
        server.server_close()
        logger.info('team_service_stopped')
