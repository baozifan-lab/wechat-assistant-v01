"""Read-only local readiness and queue counts, without invoking channel or model tools."""
from contextlib import closing
import logging
from pathlib import Path
import sqlite3

from zhishi import __version__
from .clock import stamp
from .observability import log_failure

QUEUE_TABLES = {'reminders': 'outbox', 'workflows': 'workflow_notices',
                'two_stage': 'two_stage_notices', 'abc': 'abc_notices',
                'products': 'product_demo_notices', 'ai': 'ai_dispatch_notices'}


def health_status(pilot):
    result = {'at': stamp(), 'version': __version__, 'status': 'ok', 'database': {'ok': False}, 'queues': {}}
    try:
        uri = Path(pilot.database).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute('BEGIN')
            db.execute('SELECT 1').fetchone()
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for label, table in QUEUE_TABLES.items():
                if table in tables:
                    counts = dict(db.execute(f'SELECT state, COUNT(*) FROM {table} GROUP BY state'))
                    result['queues'][label] = {state: counts.get(state, 0)
                                              for state in ('pending', 'sending', 'unknown', 'blocked')}
            result['database']['ok'] = True
    except (OSError, sqlite3.Error) as error:
        result['status'] = 'degraded'
        result['database']['error_type'] = type(error).__name__
        log_failure(logging.getLogger('zhishi.health'), 'database_probe_failed')
    workers = getattr(pilot, 'background_workers', None)
    result['background'] = workers.snapshot() if workers else {'status': 'not_started', 'workers': {}}
    if result['background']['status'] != 'ok':
        result['status'] = 'degraded'
    result['attention_count'] = sum(q['unknown'] + q['blocked'] for q in result['queues'].values())
    return result
