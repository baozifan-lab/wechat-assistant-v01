"""Workflow tables and recovery of interrupted work and uncertain sends."""

def initialize(store):
    with store.connect() as db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS workflow_runs (
            uid TEXT PRIMARY KEY, code TEXT NOT NULL, version INTEGER NOT NULL,
            task_key TEXT NOT NULL, work_date TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
            approved_at TEXT, approved_digest TEXT, review_token TEXT NOT NULL,
            plan_json TEXT NOT NULL, report_json TEXT NOT NULL DEFAULT '{}',
            assets_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
            UNIQUE(code,version));
          CREATE TABLE IF NOT EXISTS workflow_notices (
            id INTEGER PRIMARY KEY, run_uid TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
            UNIQUE(run_uid,kind));
          CREATE TABLE IF NOT EXISTS workflow_deliveries (
            run_uid TEXT NOT NULL, recipient_id TEXT NOT NULL, state TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_uid,recipient_id));
        ''')
        db.execute("UPDATE workflow_runs SET state='failed',error='执行过程中服务重启，请重新制作新版本。' WHERE state='running'")
        db.execute("UPDATE workflow_notices SET state='unknown',detail='重启前发送结果未确认，不自动重复。' WHERE state='sending'")
