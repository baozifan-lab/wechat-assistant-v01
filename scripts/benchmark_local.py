"""Measure a temporary, channel-free instance; never load the user's task database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.core.clock import now, stamp
from zhishi.runtime import build_pilot, create_server


def summarize(results):
    timings = sorted(duration for duration, ok in results)
    return {'requests': len(results), 'errors': sum(not ok for _, ok in results),
            'mean_ms': round(sum(timings) / len(timings), 3),
            'p50_ms': round(timings[math.ceil(len(timings) * .5) - 1], 3),
            'p95_ms': round(timings[math.ceil(len(timings) * .95) - 1], 3),
            'max_ms': round(timings[-1], 3)}


def main():
    parser = argparse.ArgumentParser(description='在临时本机服务中测量读写接口，不连接微信或模型')
    parser.add_argument('--requests', type=int, default=100, help='每种接口的请求数')
    parser.add_argument('--concurrency', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.requests <= 10000 or not 1 <= args.concurrency <= 32:
        parser.error('请求数须在1至10000之间，并发数须在1至32之间')
    with tempfile.TemporaryDirectory(prefix='zhishi-benchmark-') as temporary:
        root = Path(temporary)
        pilot = build_pilot(root / 'benchmark.sqlite3', root=root, enable_wechat=False)
        server = create_server(pilot, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        token = server.RequestHandlerClass.token
        due = stamp(now() + timedelta(days=1))

        def measure(write):
            path = '/api/console/reminder' if write else '/api/console'
            data = json.dumps({'action': 'create', 'title': '隔离的性能测试事项', 'due': due,
                               'request_id': str(uuid.uuid4())}).encode() if write else None
            request = urllib.request.Request(base + path, data=data,
                headers={'X-Pilot-Token': token, 'Content-Type': 'application/json'})
            started = time.perf_counter()
            try:
                # Disable proxy discovery for an explicitly loopback measurement.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=30) as response:
                    response.read()
                    ok = response.status == 200
            except OSError:
                ok = False
            return ((time.perf_counter() - started) * 1000, ok)

        try:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                read = summarize(list(pool.map(measure, [False] * args.requests)))
                write = summarize(list(pool.map(measure, [True] * args.requests)))
            result = {'at': stamp(), 'isolated': True, 'background_workers': False,
                      'wechat_connected': False, 'model_called': False,
                      'concurrency': args.concurrency, 'read_console': read, 'create_reminder': write}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return int(bool(read['errors'] or write['errors']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    raise SystemExit(main())
