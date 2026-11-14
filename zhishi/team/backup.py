"""Consistent DB-plus-immutable-artifact snapshots and guarded restore to new paths."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from zhishi.core.backup import backup_database
from zhishi.core.clock import stamp
from .errors import require
from .migration import verify_team


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def create_snapshot(database, artifact_root, directory):
    database, artifact_root, directory = Path(database).resolve(), Path(artifact_root).resolve(), Path(directory).resolve()
    require(not directory.exists(), '备份目录已存在，拒绝覆盖。', 409)
    directory.mkdir(parents=True)
    destination = directory / 'database.sqlite3'
    backup_database(database, destination)
    (directory / 'artifacts').mkdir()
    with closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT storage_key,sha256 FROM team_artifacts').fetchall()
    for key, expected in rows:
        source = (artifact_root / key).resolve()
        target = (directory / 'artifacts' / key).resolve()
        require(source.is_relative_to(artifact_root) and target.is_relative_to(directory / 'artifacts'), '成果路径越界。', 409)
        content = source.read_bytes()
        require(hashlib.sha256(content).hexdigest() == expected, '源成果摘要不一致，未完成备份。', 409)
        with target.open('xb') as stream:
            stream.write(content)
    verify_team(destination, directory / 'artifacts')
    manifest = {'format': 1, 'created_at': stamp(), 'database_sha256': checksum(destination), 'artifact_count': len(rows)}
    with (directory / 'manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    return {'ok': True, 'directory': str(directory), **manifest}


def verify_snapshot(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
    require(manifest.get('format') == 1, '备份格式不支持或尚未完成。', 409)
    require(checksum(directory / 'database.sqlite3') == manifest.get('database_sha256'), '备份数据库摘要不一致。', 409)
    result = verify_team(directory / 'database.sqlite3', directory / 'artifacts')
    require(result['artifact_count'] == manifest.get('artifact_count'), '备份成果数量不一致。', 409)
    return {'ok': True, **manifest}


def restore_snapshot(directory, database, artifact_root):
    directory, database, artifact_root = Path(directory).resolve(), Path(database).resolve(), Path(artifact_root).resolve()
    verify_snapshot(directory)
    require(not database.exists() and not artifact_root.exists(), '恢复必须使用全新的数据库及成果目录，拒绝覆盖。', 409)
    database.parent.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True)
    staging = database.parent / ('.team-restore-' + secrets.token_hex(12) + '.sqlite3')
    try:
        backup_database(directory / 'database.sqlite3', staging)
        with closing(sqlite3.connect(staging)) as db:
            rows = db.execute('SELECT storage_key,sha256 FROM team_artifacts').fetchall()
            for key, expected in rows:
                source, target = directory / 'artifacts' / key, artifact_root / key
                require(target.resolve().is_relative_to(artifact_root), '恢复路径越界。', 409)
                content = source.read_bytes()
                require(hashlib.sha256(content).hexdigest() == expected, '恢复成果摘要不一致。', 409)
                with target.open('xb') as stream:
                    stream.write(content)
            # A snapshot may precede actual delivery. Do not replay old notifications
            # or timers just because the database has been rolled back.
            db.execute("UPDATE team_notifications SET state='unknown',detail='从快照恢复，需核对真实送达后人工决定。' WHERE state IN ('pending','sending')")
            db.execute('UPDATE team_tasks SET remind_at=NULL')
            db.execute('DELETE FROM team_sessions')
            db.execute('DELETE FROM team_binding_codes')
            db.execute("INSERT INTO team_audit(actor_id,action,entity_type,entity_id,details,created_at) VALUES (NULL,'restore_snapshot','database','snapshot','{}',?)", (stamp(),))
            db.commit()
        verify_team(staging, artifact_root)
        backup_database(staging, database)
        return {'ok': True, 'database': str(database), 'artifact_root': str(artifact_root),
                'artifact_count': len(rows), 'sessions_revoked': True, 'notification_replay_disabled': True,
                'reminders_require_rescheduling': True}
    finally:
        staging.unlink(missing_ok=True)
