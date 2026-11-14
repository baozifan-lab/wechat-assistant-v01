from pathlib import Path
from contextlib import closing
import sqlite3

from .store import TeamStore
from .auth import AuthService, actor, admin
from .service import TeamService
from .jobs import JobService
from .notifications import NotificationService
from .wechat import TeamWechat
from .security import text


class TeamApplication:
    def __init__(self, database, artifact_root=None, *, bridge_secret=None, sender=None, runner=None, allow_legacy_import=False):
        from .errors import require
        database = Path(database).resolve()
        if database.is_file() and not allow_legacy_import:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'tasks' in names:
                    require('team_schema' in names and 'team_imports' in names and db.execute('SELECT 1 FROM team_imports').fetchone(),
                            '旧试点库尚未完成副本迁移，请使用team_admin.py import；未修改原库。', 409)
        self.store = TeamStore(database)
        self.auth = AuthService(self.store)
        self.service = TeamService(self.store, artifact_root or self.store.path.parent / 'team-artifacts')
        self.jobs = JobService(self.service, runner)
        self.notifier = NotificationService(self.service, sender)
        self.wechat = TeamWechat(self.service, bridge_secret)
        self.workers = None

    def flows(self, user_id):
        from .errors import TeamError
        with self.store.connect() as db:
            user = actor(db, user_id)
            result = []
            for row in db.execute('SELECT * FROM team_flows ORDER BY created_at DESC'):
                try:
                    result.append(self.service.flow(db, row['id'], user))
                except TeamError as exc:
                    if exc.status != 403:
                        raise
            return result

    def messages(self, user_id, task_id=None):
        with self.store.connect() as db:
            user = actor(db, user_id)
            if task_id:
                self.service.task(db, task_id, user)
                rows = db.execute('SELECT * FROM team_messages WHERE task_id=? ORDER BY id DESC LIMIT 200', (task_id,))
            else:
                rows = db.execute('SELECT * FROM team_messages WHERE user_id=? AND task_id IS NULL ORDER BY id DESC LIMIT 200', (user_id,))
            return [dict(row) for row in rows]

    def post_message(self, user_id, data):
        from zhishi.core.clock import stamp
        def operation(db, user):
            task_id = data.get('task_id')
            if task_id:
                task = self.service.task(db, task_id, user)
                self.service.project(db, task['project_id'], user, write=True)
            value = text(data.get('text'), '消息', 4000)
            cursor = db.execute('INSERT INTO team_messages(user_id,task_id,text,created_at) VALUES (?,?,?,?)', (user_id, task_id, value, stamp()))
            self.store.audit(db, user_id, 'post_message', 'message', cursor.lastrowid)
            return {'id': cursor.lastrowid}
        return self.service.mutate(user_id, data.get('request_id'), 'post_message', data, operation)

    def health(self, user_id):
        with self.store.connect() as db:
            admin(db, user_id)
            db.execute('SELECT 1').fetchone()
            jobs = dict(db.execute('SELECT state,COUNT(*) FROM team_jobs GROUP BY state'))
            notifications = dict(db.execute('SELECT state,COUNT(*) FROM team_notifications GROUP BY state'))
        background = self.workers.snapshot() if self.workers else {'status': 'not_started', 'workers': {}}
        return {'mode': 'team', 'schema_version': 1, 'database': {'ok': True}, 'background': background,
                'status': 'ok' if background['status'] == 'ok' else 'degraded', 'jobs': jobs, 'notifications': notifications}

    def legacy_inventory(self, user_id):
        from .migration import LEGACY_TABLES
        with self.store.connect() as db:
            admin(db, user_id)
            existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {'read_only_archive': True, 'tables': {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                                                       for name in LEGACY_TABLES if name in existing}}
