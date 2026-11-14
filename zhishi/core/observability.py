"""Bounded local diagnostics; never serialize request data or exception messages."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
import sys

from .clock import stamp


def exception_details(info=None):
    kind, _, trace = info or sys.exc_info()
    if kind is None:
        return {}
    frames = []
    while trace:
        code = trace.tb_frame.f_code
        frames.append({'file': Path(code.co_filename).name,
                       'function': code.co_name, 'line': trace.tb_lineno})
        trace = trace.tb_next
    return {'error_type': kind.__name__, 'frames': frames[-12:]}


def log_failure(logger, event, **context):
    # Sanitize before emitting too: embedding Pilot must not leak via another logger's formatter.
    logger.error(event, extra={**context, **exception_details()})


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        result = {'at': stamp(), 'level': record.levelname, 'event': record.msg}
        for name in ('worker', 'request_id', 'route', 'method', 'status', 'duration_ms', 'failures', 'error_type', 'frames'):
            if hasattr(record, name):
                result[name] = getattr(record, name)
        if record.exc_info:
            # No exception text, source lines, locals, chained errors or full paths.
            result.update(exception_details(record.exc_info))
        return json.dumps(result, ensure_ascii=False)


def configure_logging(directory):
    """Configure only our logger at explicit startup, with 12 MB maximum retention."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('zhishi')
    handler = RotatingFileHandler(directory / 'runtime.log', maxBytes=2_000_000,
                                  backupCount=5, encoding='utf-8')
    handler.setFormatter(SafeJsonFormatter())
    for old in list(logger.handlers):
        logger.removeHandler(old)
        old.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


class WorkerMonitor:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.lock = threading.Lock()
        self.workers = {}
        self.last_error_log = {}
        self.logger = logging.getLogger('zhishi.workers')

    def register(self, name):
        with self.lock:
            self.workers[name] = {'calls': 0, 'errors': 0, 'consecutive_errors': 0,
                                  'busy': False, 'last_success': None, 'last_failure': None,
                                  'error_type': None, 'last_ms': 0, 'total_ms': 0, 'max_ms': 0}

    def run(self, name, callback):
        start = self.clock()
        with self.lock:
            self.workers[name]['busy'] = True
        try:
            callback()
        except Exception as error:
            with self.lock:
                item = self.workers[name]
                item['errors'] += 1
                item['consecutive_errors'] += 1
                item['last_failure'] = stamp()
                item['error_type'] = type(error).__name__
                failures = item['consecutive_errors']
                log_error = failures == 1 or start - self.last_error_log[name] >= 60
                if log_error:
                    self.last_error_log[name] = start
            if log_error:
                log_failure(self.logger, 'worker_failed', worker=name, failures=failures)
        else:
            with self.lock:
                item = self.workers[name]
                recovered = item['consecutive_errors'] > 0
                item['consecutive_errors'] = 0
                item['last_success'] = stamp()
                item['error_type'] = None
            if recovered:
                self.logger.info('worker_recovered', extra={'worker': name})
        finally:
            elapsed = round((self.clock() - start) * 1000, 3)
            with self.lock:
                item = self.workers[name]
                item['busy'] = False
                item['calls'] += 1
                item['last_ms'] = elapsed
                item['total_ms'] += elapsed
                item['max_ms'] = max(item['max_ms'], elapsed)

    def snapshot(self):
        with self.lock:
            return {name: {**item, 'avg_ms': round(item['total_ms'] / item['calls'], 3) if item['calls'] else 0}
                    for name, item in self.workers.items()}


class RequestMetrics:
    """Finite route labels and histogram buckets, rather than retaining request histories."""
    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}

    def record(self, route, status, elapsed_ms):
        with self.lock:
            item = self.routes.setdefault(route, {'calls': 0, 'errors': 0, 'total_ms': 0, 'max_ms': 0,
                                                  'duration_buckets': dict.fromkeys(('lt50', 'lt200', 'lt1000', 'gte1000'), 0)})
            item['calls'] += 1
            item['errors'] += int(status >= 500)
            item['total_ms'] += elapsed_ms
            item['max_ms'] = max(item['max_ms'], elapsed_ms)
            bucket = 'lt50' if elapsed_ms < 50 else 'lt200' if elapsed_ms < 200 else 'lt1000' if elapsed_ms < 1000 else 'gte1000'
            item['duration_buckets'][bucket] += 1

    def snapshot(self):
        with self.lock:
            return {route: {**item, 'duration_buckets': dict(item['duration_buckets']),
                            'avg_ms': round(item['total_ms'] / item['calls'], 3)}
                    for route, item in self.routes.items()}
