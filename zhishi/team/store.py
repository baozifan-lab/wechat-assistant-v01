"""Per-operation SQLite connections and atomic, versioned schema creation."""
from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import sqlite3

from zhishi.core.clock import stamp
from .errors import TeamError


class TeamStore:
    def __init__(self, database, *, initialize=True):
        self.path = Path(database).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self.initialize()

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=15000')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        schema = Path(__file__).with_name('schema.sql').read_text('utf-8')
        checksum = hashlib.sha256(schema.encode()).hexdigest()
        # executescript has implicit transaction semantics, so execute individual DDL
        # statements inside our explicit transaction instead.
        with closing(sqlite3.connect(self.path, timeout=15)) as setup:
            setup.execute('PRAGMA journal_mode=WAL')
        with self.connect(write=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='team_schema'").fetchone()
            if exists:
                versions = db.execute('SELECT version,checksum FROM team_schema').fetchall()
                if len(versions) != 1 or versions[0]['version'] != 1 or versions[0]['checksum'] != checksum:
                    raise TeamError('多人数据库结构版本不匹配，已停止；请使用对应迁移工具。', 409)
                return
            if db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'team_%'").fetchone():
                raise TeamError('发现未完成的多人数据结构，拒绝覆盖，请检查副本。', 409)
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            db.execute('INSERT INTO team_schema VALUES (1,?,?)', (checksum, stamp()))

    @staticmethod
    def audit(db, actor, action, entity_type, entity_id, details=None):
        db.execute('INSERT INTO team_audit(actor_id,action,entity_type,entity_id,details,created_at) VALUES (?,?,?,?,?,?)',
                   (actor, action, entity_type, str(entity_id), json.dumps(details or {}, ensure_ascii=False), stamp()))
