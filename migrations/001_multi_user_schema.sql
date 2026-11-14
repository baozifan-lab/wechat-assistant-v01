-- 知事多人协作 - 最小用户系统设计
-- 基于现有SQLite，渐进式添加表

-- 1. 用户表
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    role TEXT DEFAULT 'employee' CHECK (role IN ('admin', 'manager', 'employee')),
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_status ON users(status);

-- 2. 会话表（用于登录态管理）
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_sessions_token ON sessions(token);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- 3. 微信绑定表（用户与微信账号的映射）
CREATE TABLE IF NOT EXISTS wechat_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    wechat_account_id TEXT NOT NULL,
    wechat_peer_id TEXT NOT NULL,
    nickname TEXT,
    bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(wechat_account_id, wechat_peer_id)
);

CREATE INDEX idx_wechat_user ON wechat_bindings(user_id);
CREATE INDEX idx_wechat_peer ON wechat_bindings(wechat_peer_id);

-- 4. 修改现有tasks表，添加用户关联
-- 注意：这是迁移脚本，需要保留现有数据
ALTER TABLE tasks ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE tasks ADD COLUMN creator_id INTEGER REFERENCES users(id);

-- 为现有数据创建默认用户（迁移过程）
-- INSERT INTO users (id, username, password_hash, display_name, role)
-- VALUES (1, 'pilot', '...', '试点用户', 'admin');
-- UPDATE tasks SET user_id = 1, creator_id = 1 WHERE user_id IS NULL;

-- 5. 修改messages表，添加用户关联
ALTER TABLE messages ADD COLUMN user_id INTEGER REFERENCES users(id);

-- 6. 审计日志表（简化版，记录关键操作）
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details TEXT,
    ip_address TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_audit_user ON audit_logs(user_id, created_at);
CREATE INDEX idx_audit_entity ON audit_logs(entity_type, entity_id);

-- 7. 初始化管理员用户（密码：Admin@123）
INSERT INTO users (username, password_hash, display_name, email, role) VALUES
('admin', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5NU7VU.aQBZHu', '系统管理员', 'admin@zhishi.local', 'admin');

-- 8. 权限检查辅助视图
CREATE VIEW user_task_summary AS
SELECT
    u.id as user_id,
    u.username,
    u.display_name,
    COUNT(CASE WHEN t.status = 'open' THEN 1 END) as open_tasks,
    COUNT(CASE WHEN t.status = 'completed' THEN 1 END) as completed_tasks
FROM users u
LEFT JOIN tasks t ON t.user_id = u.id
WHERE u.status = 'active'
GROUP BY u.id, u.username, u.display_name;

-- 数据迁移说明：
-- 1. 运行此脚本创建新表
-- 2. 创建管理员用户
-- 3. 将现有 owner='pilot-owner' 的数据关联到管理员
-- 4. 逐步为新员工创建账号
-- 5. 更新业务代码使用 user_id 而非固定 owner
