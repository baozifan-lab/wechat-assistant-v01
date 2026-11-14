"""Application assembly and lifecycle; importing modules starts no workers."""
import argparse
import secrets
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from .config import ROOT, DATA_ROOT
from .core.observability import configure_logging, log_failure, WorkerMonitor, RequestMetrics
from .core.local_postgres import ensure_local_postgres
from .service import Pilot
from .web.handler import Handler
from .web.workbench import Workbench
from .web.accounts import AccountDirectory
from .web.work_orders import WorkOrders
from .web.corrections import Corrections
from .workflows.service import Workflows
from .workflows.two_stage import TwoStageDemo
from .workflows.abc_dispatch import ABCDispatch
from .workflows.product_demo import ProductDemo
from .workflows.invoice_demo import InvoiceDemo
from .workflows.ai_dispatch import AIDispatch


def build_pilot(database, *, root=ROOT, transport=None, enable_wechat=True):
    """Inject a channel for tests or run locally without creating WeChat credentials."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    if transport is None and enable_wechat:
        from .channels.wechat.bridge import WechatBridge
        transport = WechatBridge(root)
    pilot = Pilot(database, transport=transport)
    pilot.employee2_transport = None
    if enable_wechat and transport is not None:
        from .channels.wechat.bridge import WechatBridge
        pilot.employee2_transport = WechatBridge(root, Path.home()/'.openclaw-zhishi-employee2', port=18792, login_script='wechat_employee2_connect.py')
    pilot.workflow = Workflows(pilot, root)
    pilot.two_stage = TwoStageDemo(pilot, root)
    pilot.abc = ABCDispatch(pilot, root)
    pilot.product_demo = ProductDemo(pilot, root)
    pilot.invoice_demo = InvoiceDemo(pilot, root)
    pilot.ai_dispatch = AIDispatch(pilot, root)
    pilot.workbench = Workbench(pilot, root)
    pilot.accounts = AccountDirectory(pilot, root, enable_wechat=enable_wechat)
    pilot.work_orders = WorkOrders(pilot)
    pilot.corrections = Corrections(pilot, root)
    return pilot


def create_server(pilot, port=8765, *, handler_class=Handler, host='127.0.0.1'):
    """Bind per-server state so separate servers cannot share credentials or services."""
    class BoundHandler(handler_class):
        pass

    BoundHandler.pilot = pilot
    BoundHandler.token = secrets.token_urlsafe(32)
    class LoopbackServer(ThreadingHTTPServer):
        # Windows otherwise permits two HTTPServers to bind the same port.
        allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

        def server_bind(self):
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            return super().server_bind()

    server = LoopbackServer((host, port), BoundHandler)
    if pilot is not None and getattr(pilot, 'accounts', None):
        pilot.accounts.service_url = f'http://127.0.0.1:{server.server_port}'
    server.metrics = RequestMetrics()
    return server


class BackgroundWorkers:
    def __init__(self, pilot):
        self.pilot = pilot
        self.stop = threading.Event()
        self.threads = []
        self.monitor = WorkerMonitor()
        self.pilot.background_workers = self

    def start(self):
        if self.threads or self.stop.is_set():
            raise RuntimeError('后台任务已启动或已停止，不能重复启动。')
        jobs = [
            ("reminders", self.pilot.tick),
            ("workflows", self.pilot.workflow.tick),
            ("two-stage", self.pilot.two_stage.tick),
            ("two-stage-notices", self.pilot.two_stage.notifications.tick),
            ("abc-dispatch", self.pilot.abc.tick),
            ("abc-notices", self.pilot.abc.notify_tick),
            ("product-demo", self.pilot.product_demo.tick),
            ("ai-schedule", self.pilot.ai_dispatch.schedule_tick),
            ("ai-dispatch", self.pilot.ai_dispatch.work_tick),
            ("ai-notices", self.pilot.ai_dispatch.notify_tick),
        ]
        if getattr(self.pilot, 'invoice_demo', None):
            jobs.append(("invoice-demo", self.pilot.invoice_demo.tick))
        if self.pilot.transport and hasattr(self.pilot.transport,'ensure_gateway'):
            jobs.append(("wechat-health", self.pilot.transport.ensure_gateway))
        if self.pilot.employee2_transport:
            jobs.append(("employee2-wechat-health", self.pilot.employee2_transport.ensure_gateway))
        if getattr(self.pilot, 'accounts', None):
            jobs.append(("employee-wechat-health", self.pilot.accounts.maintain))
        if getattr(self.pilot, 'work_orders', None):
            jobs.append(("staff-assignments", self.pilot.work_orders.tick))
        for name, callback in jobs:
            self.monitor.register(name)
            thread = threading.Thread(target=self._run, args=(name, callback), name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def _run(self, name, callback):
        while not self.stop.wait(1):
            self.monitor.run(name, callback)

    def snapshot(self):
        items = self.monitor.snapshot()
        alive = {thread.name: thread.is_alive() for thread in self.threads}
        for name, item in items.items():
            item['alive'] = alive.get(name, False)
        status = 'stopped' if self.stop.is_set() else 'not_started' if not items else 'ok'
        if status == 'ok' and any(not item['alive'] or item['consecutive_errors'] for item in items.values()):
            status = 'degraded'
        elif status == 'ok' and any(not item['calls'] for item in items.values()):
            status = 'starting'
        return {'status': status, 'workers': items}

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description="知事 · 本地工作助理")
    parser.add_argument('--mode', choices=('pilot', 'team'), default='pilot')
    parser.add_argument("--port", type=int)
    parser.add_argument("--database")
    parser.add_argument("--log-directory", type=Path, help="运行日志目录；默认放在数据库旁的 logs 目录")
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--allowed-host', action='append')
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
    parser.add_argument('--artifact-root', type=Path)
    parser.add_argument('--skill-workers', type=int, default=2)
    parser.add_argument('--backup-interval-minutes', type=int, default=0, help='team模式完整备份间隔；0表示仅手动备份')
    parser.add_argument('--enable-wechat', action='store_true', help='仅用于team模式，需显式配置已授权网关')
    args = parser.parse_args()
    if args.mode == 'team':
        from .team.runtime import run_team
        return run_team(args)
    if args.host != '127.0.0.1':
        parser.error('本机试点模式只允许127.0.0.1；员工访问请使用team模式。')
    args.port = 8765 if args.port is None else args.port
    args.database = args.database or str(DATA_ROOT / 'pilot.sqlite3')
    # Claim the port before database recovery: a duplicate launch must not reset
    # inbox/notices belonging to an already running instance.
    server = create_server(None, args.port)
    workers = None
    logger = None
    try:
        logger = configure_logging(args.log_directory or Path(args.database).resolve().parent / 'logs')
        db_status, db_message = ensure_local_postgres()
        if db_status in ('failed', 'unavailable'):
            logger.warning('local_postgres_%s', db_status)
        else:
            logger.info('local_postgres_%s', db_status)
        print(db_message, flush=True)
        pilot = build_pilot(args.database)
        pilot.accounts.service_url = f'http://127.0.0.1:{server.server_port}'
        server.RequestHandlerClass.pilot = pilot
        workers = BackgroundWorkers(pilot)
        workers.start()
        logger.info('service_started')
        print(f"工作助理: http://127.0.0.1:{server.server_port}", flush=True)
        print("本机单用户试点 / 通道状态以页面为准 / Ctrl+C 停止", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        if logger:
            log_failure(logger, 'service_failed')
        raise
    finally:
        if workers:
            workers.close()
        server.server_close()
        if logger:
            logger.info('service_stopped')


if __name__ == "__main__":
    main()
