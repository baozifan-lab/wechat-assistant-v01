"""SQLite connection ownership, schema migration and conversation storage."""
import sqlite3
from contextlib import contextmanager
from zhishi.config import OWNER
from .clock import stamp


class Database:
    def __init__(self, database):
        self.database = str(database)
        self.initialize()

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                  id INTEGER PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                  due TEXT NOT NULL, next_at TEXT, status TEXT NOT NULL DEFAULT 'open',
                  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, owner TEXT NOT NULL, role TEXT NOT NULL,
                  text TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                  id INTEGER PRIMARY KEY, owner TEXT NOT NULL, task_id INTEGER NOT NULL,
                  version INTEGER NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(task_id,version));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY, response TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('model','reminder-fast')")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('mode','rules')")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
            if 'channel' not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN channel TEXT NOT NULL DEFAULT 'demo'")
            if 'detail' not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE outbox SET state='unknown',detail='服务重启前发送未确认，不自动重发。' WHERE state='sending'")
            if not db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
                self.message(db, "assistant", "我是你的工作助理。这里是本地试点，微信尚未连接。\n试试：10 秒后提醒我提交项目周报。\n也可以发送：我的待办、完成 1、10 分钟后提醒 1。")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def message(db, role, text):
        db.execute("INSERT INTO messages(owner,role,text,created_at) VALUES (?,?,?,?)",
                   (OWNER, role, text, stamp()))

    def settings(self, db):
        return {r["key"]: r["value"] for r in db.execute("SELECT * FROM settings")}
