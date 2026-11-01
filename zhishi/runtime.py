"""Application assembly and lifecycle; importing modules starts no workers."""
import argparse
import secrets
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from .config import ROOT, DATA_ROOT
from .service import Pilot
from .web.handler import Handler
from .workflows.service import Workflows
from .workflows.two_stage import TwoStageDemo
from .workflows.abc_dispatch import ABCDispatch


def build_pilot(database, *, root=ROOT, transport=None, enable_wechat=True):
    """Inject a channel for tests or run locally without creating WeChat credentials."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    if transport is None and enable_wechat:
        from .channels.wechat.bridge import WechatBridge
        transport = WechatBridge(root)
    pilot = Pilot(database, transport=transport)
    pilot.workflow = Workflows(pilot, root)
    pilot.two_stage = TwoStageDemo(pilot, root)
    pilot.abc = ABCDispatch(pilot, root)
    return pilot


def create_server(pilot, port=8765):
    """Bind per-server state so separate servers cannot share credentials or services."""
    class BoundHandler(Handler):
        pass

    BoundHandler.pilot = pilot
    BoundHandler.token = secrets.token_urlsafe(32)
    return ThreadingHTTPServer(("127.0.0.1", port), BoundHandler)


class BackgroundWorkers:
    def __init__(self, pilot):
        self.pilot = pilot
        self.stop = threading.Event()
        self.threads = []

    def start(self):
        jobs = [
            ("reminders", self.pilot.tick, (sqlite3.Error,), "后台检查暂时失败，将在下次检查重试。"),
            ("workflows", self.pilot.workflow.tick, (Exception,), "今日工作检查暂时失败，请查看工作台。"),
            ("two-stage", self.pilot.two_stage.tick, (Exception,), "两段式验证检查暂时失败，请查看工作台。"),
            ("two-stage-notices", self.pilot.two_stage.notifications.tick, (Exception,), "两段式微信通知检查暂时失败，请查看工作台。"),
            ("abc-dispatch", self.pilot.abc.tick, (Exception,), "三角色任务检查暂时失败，请查看工作台。"),
            ("abc-notices", self.pilot.abc.notify_tick, (Exception,), "三角色通知检查暂时失败，请查看工作台。"),
        ]
        if self.pilot.transport and hasattr(self.pilot.transport,'ensure_gateway'):
            jobs.append(("wechat-health", self.pilot.transport.ensure_gateway, (Exception,), "微信通道恢复暂时失败，请查看连接状态。"))
        for name, callback, exceptions, message in jobs:
            thread = threading.Thread(target=self._run, args=(callback, exceptions, message), name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def _run(self, callback, exceptions, message):
        while not self.stop.wait(1):
            try:
                callback()
            except exceptions:
                print(message, flush=True)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description="知事 · 本地工作助理")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default=str(DATA_ROOT / "pilot.sqlite3"))
    args = parser.parse_args()
    pilot = build_pilot(args.database)
    server = create_server(pilot, args.port)
    workers = BackgroundWorkers(pilot)
    workers.start()
    print(f"工作助理: http://127.0.0.1:{server.server_port}", flush=True)
    print("本机单用户试点 / 通道状态以页面为准 / Ctrl+C 停止", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workers.close()
        server.server_close()


if __name__ == "__main__":
    main()
