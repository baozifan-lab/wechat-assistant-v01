"""Copy-first legacy import: original records and delivery states remain unchanged."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from zhishi.core.backup import backup_database, inspect_database
from zhishi.core.clock import stamp
from .application import TeamApplication
from .errors import require
from .service import encoded, ident

LEGACY_TABLES = ('tasks', 'messages', 'outbox', 'settings', 'requests', 'workflow_runs',
                'workflow_notices', 'workflow_deliveries', 'two_stage_runs', 'two_stage_events',
                'two_stage_notices', 'abc_runs', 'abc_events', 'abc_notices', 'product_demo_runs',
                'product_demo_notices', 'ai_dispatch_runs', 'ai_dispatch_inbox', 'ai_dispatch_notices',
                'ai_dispatch_events', 'ai_dispatch_selections')


def import_legacy(source, destination, username, password, *, owner='pilot-owner', artifact_root=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    require(source.is_file() and source != destination and not destination.exists(), '迁移必须使用存在的原库和不存在的新副本路径。', 409)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require('tasks' in names and 'messages' in names and 'team_schema' not in names, '来源不是未迁移的知事试点数据库。', 409)
        require({'owner', 'due', 'next_at', 'version'} <= {r[1] for r in db.execute('PRAGMA table_info(tasks)')}, '旧待办结构不符合预期，停止迁移。', 409)
        owners = {r[0] for r in db.execute('SELECT owner FROM tasks UNION SELECT owner FROM messages')}
        require(not owners or owner in owners, '指定owner没有对应历史记录，请先核对身份映射。', 409)
    snapshot = backup_database(source, destination)
    # An interrupted import leaves a copy for diagnosis, never modifies the source
    # and never starts either application's recovery/background processes.
    app = TeamApplication(destination, artifact_root, allow_legacy_import=True)
    user = app.auth.bootstrap(username, password, '历史试点管理员')
    with app.store.connect(write=True) as db:
        project_id = ident('p_')
        db.execute('INSERT INTO team_projects VALUES (?,?,?,?,?)', (project_id, '历史试点导入', '旧工作流和发送记录作为只读历史保留；未自动重放。', user['id'], stamp()))
        db.execute("INSERT INTO team_members VALUES (?,?,'manager')", (project_id, user['id']))
        counts = {'tasks': 0, 'messages': 0, 'preserved_tables': len([name for name in LEGACY_TABLES if name in names])}
        for task in db.execute('SELECT * FROM tasks WHERE owner=? ORDER BY id', (owner,)).fetchall():
            state = {'open': 'open', 'done': 'completed', 'cancelled': 'cancelled'}.get(task['status'])
            require(state is not None, '旧任务包含未识别状态，请先确定映射。', 409)
            task_id = ident('t_')
            db.execute('INSERT INTO team_tasks(id,project_id,title,description,creator_id,assignee_id,reviewer_id,state,version,due_at,'
                       'requires_artifact,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, project_id, task['title'], '导入旧待办 #' + str(task['id']) + '；提醒未自动启用。',
                        user['id'], user['id'], user['id'], state, task['version'], task['due'], 0, task['created_at'], stamp()))
            counts['tasks'] += 1
        for message in db.execute('SELECT * FROM messages WHERE owner=? ORDER BY id', (owner,)).fetchall():
            db.execute('INSERT INTO team_messages(user_id,text,created_at) VALUES (?,?,?)',
                       (user['id'], '[' + message['role'] + '] ' + message['text'], message['created_at']))
            counts['messages'] += 1
        db.execute('INSERT INTO team_imports VALUES (?,?,?,?)', (snapshot['sha256'], user['id'], stamp(), encoded(counts)))
        app.store.audit(db, user['id'], 'import_legacy', 'database', 'legacy_copy', counts)
    validation = verify_team(destination, app.service.artifact_root)
    return {'ok': True, 'destination': str(destination), 'source_snapshot_sha256': snapshot['sha256'],
            'owner_mapped_to': user['username'], 'counts': counts, 'validation': validation,
            'original_unchanged': True, 'legacy_delivery_replayed': False}


def verify_team(database, artifact_root=None):
    database = Path(database).resolve()
    report = inspect_database(database)
    root = Path(artifact_root or database.parent / 'team-artifacts').resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        require(db.execute("SELECT 1 FROM sqlite_master WHERE name='team_schema'").fetchone(), '尚未初始化多人结构。', 409)
        versions = db.execute('SELECT version,checksum FROM team_schema').fetchall()
        # Match initialization's universal-newline normalization so a Windows
        # checkout with CRLF can verify/restore the same schema as an LF checkout.
        expected = hashlib.sha256(Path(__file__).with_name('schema.sql').read_text('utf-8').encode()).hexdigest()
        require(len(versions) == 1 and versions[0]['version'] == 1 and versions[0]['checksum'] == expected, '多人结构版本或校验值不匹配。', 409)
        legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone()
        require(not legacy or db.execute('SELECT 1 FROM team_imports').fetchone(), '旧数据副本迁移尚未完成，不能投入使用。', 409)
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), '外键完整性检查失败。', 409)
        artifacts = db.execute('SELECT storage_key,sha256 FROM team_artifacts').fetchall()
        for row in artifacts:
            path = (root / row['storage_key']).resolve()
            require(path.is_relative_to(root) and path.is_file(), '存在缺失或越界的成果文件。', 409)
            require(hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], '存在摘要不一致的成果文件。', 409)
    return {'ok': report['ok'], 'foreign_keys_ok': True, 'artifact_count': len(artifacts), 'tables': report['tables']}
