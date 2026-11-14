"""SQLite online snapshots without running application recovery or overwriting files."""
from contextlib import closing
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .clock import stamp


def inspect_database(path):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.execute('BEGIN')
        check = db.execute('PRAGMA quick_check').fetchall()
        if check != [('ok',)]:
            raise ValueError('数据库完整性检查未通过。')
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in names}
    return {'checked_at': stamp(), 'ok': True, 'tables': counts}


def backup_database(source, destination, *, timeout=30):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('备份超时须为正数。')
    if not source.is_file():
        raise FileNotFoundError('源数据库不存在，未创建新数据库。')
    if source == destination or destination.exists():
        raise FileExistsError('备份目标已存在，拒绝覆盖。')
    # SQLite sidecars must never be used as backup destinations.
    if destination in (Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('备份目标不能是数据库日志文件。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.zhishi-backup-', suffix='.tmp', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > timeout:
            raise TimeoutError('备份超时，未发布不完整备份。')

    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=1)) as original:
            with closing(sqlite3.connect(temporary)) as target:
                original.backup(target, pages=128, progress=progress, sleep=0.05)
        report = inspect_database(temporary)
        with temporary.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        # Same-directory hard link publishes a complete file and refuses races/overwrite.
        os.link(temporary, destination)
        return {**report, 'file': str(destination), 'bytes': destination.stat().st_size,
                'sha256': checksum, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3)}
    finally:
        temporary.unlink(missing_ok=True)
