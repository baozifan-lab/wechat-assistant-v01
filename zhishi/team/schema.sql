CREATE TABLE team_schema (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE team_users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','manager','employee')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL);
CREATE TABLE team_sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES team_users(id),
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX team_sessions_user ON team_sessions(user_id);
CREATE INDEX team_sessions_expires ON team_sessions(expires_at);
CREATE TABLE team_login_limits (scope TEXT PRIMARY KEY, failures INTEGER NOT NULL, until_at TEXT NOT NULL);
CREATE TABLE team_projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 owner_id INTEGER NOT NULL REFERENCES team_users(id), created_at TEXT NOT NULL);
CREATE TABLE team_members (
 project_id TEXT NOT NULL REFERENCES team_projects(id), user_id INTEGER NOT NULL REFERENCES team_users(id),
 role TEXT NOT NULL CHECK(role IN ('manager','member','viewer')), PRIMARY KEY(project_id,user_id));
CREATE TABLE team_flows (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES team_projects(id), title TEXT NOT NULL,
 creator_id INTEGER NOT NULL REFERENCES team_users(id), prepare_id INTEGER NOT NULL REFERENCES team_users(id),
 review_id INTEGER NOT NULL REFERENCES team_users(id), approver_id INTEGER NOT NULL REFERENCES team_users(id),
 state TEXT NOT NULL CHECK(state IN ('preparing','reviewing','awaiting_approval','completed','cancelled')),
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE team_tasks (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES team_projects(id), title TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', creator_id INTEGER NOT NULL REFERENCES team_users(id),
 assignee_id INTEGER NOT NULL REFERENCES team_users(id), reviewer_id INTEGER NOT NULL REFERENCES team_users(id),
 state TEXT NOT NULL CHECK(state IN ('open','in_progress','awaiting_review','completed','cancelled')),
 version INTEGER NOT NULL DEFAULT 1, result_round INTEGER NOT NULL DEFAULT 1, due_at TEXT, remind_at TEXT,
 requires_artifact INTEGER NOT NULL DEFAULT 1 CHECK(requires_artifact IN (0,1)),
 flow_id TEXT REFERENCES team_flows(id), stage INTEGER CHECK(stage IN (1,2)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(flow_id,stage));
CREATE INDEX team_tasks_assignee ON team_tasks(assignee_id,state);
CREATE TABLE team_artifacts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id), name TEXT NOT NULL,
 storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('input','output')), uploaded_by INTEGER NOT NULL REFERENCES team_users(id),
 task_version INTEGER NOT NULL, result_round INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE team_inputs (
 task_id TEXT NOT NULL REFERENCES team_tasks(id), artifact_id TEXT NOT NULL REFERENCES team_artifacts(id),
 sha256 TEXT NOT NULL, PRIMARY KEY(task_id,artifact_id));
CREATE TABLE team_task_events (
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id),
 actor_id INTEGER REFERENCES team_users(id), kind TEXT NOT NULL, version INTEGER NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE team_messages (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES team_users(id),
 task_id TEXT REFERENCES team_tasks(id), text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE team_audit (
 id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES team_users(id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE team_requests (
 user_id INTEGER NOT NULL REFERENCES team_users(id), request_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(user_id,request_id));
CREATE TABLE team_bindings (
 user_id INTEGER PRIMARY KEY REFERENCES team_users(id), account_id TEXT NOT NULL, peer_id TEXT NOT NULL,
 generation TEXT NOT NULL, push_enabled INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, UNIQUE(account_id,peer_id));
CREATE TABLE team_binding_codes (
 code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES team_users(id), expires_at TEXT NOT NULL,
 push_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE team_inbound (
 scope TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE team_notifications (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES team_users(id),
 task_id TEXT REFERENCES team_tasks(id), task_version INTEGER,
 text TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','sending','delivered_local','submitted_unverified','unknown','blocked','cancelled')),
 binding_json TEXT, detail TEXT NOT NULL DEFAULT '', read_at TEXT,
 delivery_token TEXT, lease_until TEXT, created_at TEXT NOT NULL);
CREATE INDEX team_notifications_pending ON team_notifications(state,created_at);
CREATE TABLE team_jobs (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES team_tasks(id),
 requested_by INTEGER NOT NULL REFERENCES team_users(id), task_version INTEGER NOT NULL,
 skill TEXT NOT NULL, inputs_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','stale','cancelled')),
 lease_token TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX team_jobs_pending ON team_jobs(state,created_at);
CREATE TABLE team_imports (source_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES team_users(id),
 imported_at TEXT NOT NULL, counts_json TEXT NOT NULL);
