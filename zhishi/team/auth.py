"""Accounts, revocable sessions and login throttling with operation-scoped connections."""
from datetime import timedelta
import secrets
import sqlite3

from zhishi.core.clock import now, stamp
from .errors import TeamError, require
from .security import digest, integer, password_hash, password_matches, text, username

PUBLIC_USER = 'id,username,display_name,role,active,created_at'
DUMMY_HASH = 'pbkdf2_sha256$600000$' + '00' * 16 + '$' + '00' * 32


def actor(db, user_id):
    user = db.execute(f'SELECT {PUBLIC_USER} FROM team_users WHERE id=? AND active=1', (user_id,)).fetchone()
    require(user is not None, '请重新登录。', 401, 'unauthorized')
    return dict(user)


def admin(db, user_id):
    user = actor(db, user_id)
    require(user['role'] == 'admin', '需要管理员权限。')
    return user


class AuthService:
    def __init__(self, store):
        self.store = store

    def _insert(self, db, data, hashed):
        name = username(data.get('username'))
        display = text(data.get('display_name'), '显示名称', 100)
        role = data.get('role', 'employee')
        require(role in ('admin', 'manager', 'employee'), '角色无效。', 400)
        try:
            cursor = db.execute('INSERT INTO team_users(username,password_hash,display_name,role,created_at) VALUES (?,?,?,?,?)',
                                (name, hashed, display, role, stamp()))
        except sqlite3.IntegrityError:
            raise TeamError('用户名已存在。', 409, 'conflict') from None
        return dict(db.execute(f'SELECT {PUBLIC_USER} FROM team_users WHERE id=?', (cursor.lastrowid,)).fetchone())

    def bootstrap(self, name, password, display='管理员'):
        hashed = password_hash(password)
        with self.store.connect(write=True) as db:
            require(not db.execute('SELECT 1 FROM team_users LIMIT 1').fetchone(), '管理员已经初始化。', 409)
            user = self._insert(db, {'username': name, 'display_name': display, 'role': 'admin'}, hashed)
            self.store.audit(db, user['id'], 'bootstrap', 'user', user['id'])
            return user

    def create_user(self, user_id, data):
        # Hash outside the write transaction, after checking authorization.
        with self.store.connect() as db:
            admin(db, user_id)
        hashed = password_hash(data.get('password'))
        with self.store.connect(write=True) as db:
            admin(db, user_id)
            user = self._insert(db, data, hashed)
            self.store.audit(db, user_id, 'create_user', 'user', user['id'], {'role': user['role']})
            return user

    def users(self, user_id):
        with self.store.connect() as db:
            admin(db, user_id)
            return [dict(row) for row in db.execute(f'SELECT {PUBLIC_USER} FROM team_users ORDER BY id')]

    def update_user(self, user_id, target, data):
        integer(target)
        with self.store.connect(write=True) as db:
            admin(db, user_id)
            old = db.execute('SELECT * FROM team_users WHERE id=?', (target,)).fetchone()
            require(old is not None, '用户不存在。', 404)
            role, active = data.get('role', old['role']), data.get('active', bool(old['active']))
            require(role in ('admin', 'manager', 'employee') and type(active) is bool, '角色或启用状态无效。', 400)
            if old['active'] and old['role'] == 'admin' and (not active or role != 'admin'):
                require(db.execute("SELECT COUNT(*) FROM team_users WHERE active=1 AND role='admin'").fetchone()[0] > 1,
                        '不能停用或降级最后一名管理员。', 409)
            display = text(data.get('display_name', old['display_name']), '显示名称', 100)
            db.execute('UPDATE team_users SET role=?,active=?,display_name=? WHERE id=?', (role, int(active), display, target))
            if not active or role != old['role']:
                db.execute('DELETE FROM team_sessions WHERE user_id=?', (target,))
                db.execute('DELETE FROM team_binding_codes WHERE user_id=?', (target,))
            self.store.audit(db, user_id, 'update_user', 'user', target, {'role': role, 'active': active})
            return dict(db.execute(f'SELECT {PUBLIC_USER} FROM team_users WHERE id=?', (target,)).fetchone())

    def login(self, name, password, address='local'):
        require(isinstance(name, str) and len(name) <= 100, '账号或密码不正确。', 401)
        name = name.lower()
        scopes = [(digest('username:' + name), 5), (digest('address:' + address), 30)]
        with self.store.connect() as db:
            for scope, limit in scopes:
                entry = db.execute('SELECT * FROM team_login_limits WHERE scope=?', (scope,)).fetchone()
                require(not entry or entry['until_at'] <= stamp() or entry['failures'] < limit,
                        '登录尝试过于频繁，请15分钟后重试。', 429, 'rate_limited')
            row = db.execute('SELECT * FROM team_users WHERE username=?', (name,)).fetchone()
        valid = password_matches(password, row['password_hash'] if row else DUMMY_HASH)
        result = None
        with self.store.connect(write=True) as db:
            # Recheck after password verification: disabling/changing passwords wins races.
            current = db.execute('SELECT * FROM team_users WHERE username=?', (name,)).fetchone()
            for scope, limit in scopes:
                entry = db.execute('SELECT * FROM team_login_limits WHERE scope=?', (scope,)).fetchone()
                require(not entry or entry['until_at'] <= stamp() or entry['failures'] < limit,
                        '登录尝试过于频繁，请15分钟后重试。', 429, 'rate_limited')
            if valid and current and current['active'] and row and current['password_hash'] == row['password_hash']:
                token = secrets.token_urlsafe(32)
                db.execute('DELETE FROM team_sessions WHERE expires_at<=?', (stamp(),))
                db.execute('INSERT INTO team_sessions VALUES (?,?,?,?)',
                           (digest(token), current['id'], stamp(now() + timedelta(hours=24)), stamp()))
                for scope, _ in scopes:
                    db.execute('DELETE FROM team_login_limits WHERE scope=?', (scope,))
                self.store.audit(db, current['id'], 'login', 'user', current['id'])
                result = {'user': actor(db, current['id']), 'access_token': token,
                          'expires_at': stamp(now() + timedelta(hours=24))}
            else:
                for scope, _ in scopes:
                    db.execute('INSERT INTO team_login_limits VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET '
                               'failures=CASE WHEN until_at<=? THEN 1 ELSE failures+1 END, '
                               'until_at=CASE WHEN until_at<=? THEN excluded.until_at ELSE until_at END',
                               (scope, 1, stamp(now() + timedelta(minutes=15)), stamp(), stamp()))
                self.store.audit(db, None, 'login_failed', 'authentication', 'login')
        require(result is not None, '账号或密码不正确。', 401, 'unauthorized')
        return result

    def verify(self, token):
        require(isinstance(token, str) and 20 <= len(token) <= 200, '请登录。', 401, 'unauthorized')
        with self.store.connect() as db:
            row = db.execute('SELECT user_id FROM team_sessions WHERE token_hash=? AND expires_at>?', (digest(token), stamp())).fetchone()
            require(row is not None, '登录已失效。', 401, 'unauthorized')
            return actor(db, row['user_id'])

    def logout(self, token):
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT user_id FROM team_sessions WHERE token_hash=?', (digest(token),)).fetchone()
            db.execute('DELETE FROM team_sessions WHERE token_hash=?', (digest(token),))
            if row:
                self.store.audit(db, row['user_id'], 'logout', 'user', row['user_id'])

    def change_password(self, user_id, old, new, *, target=None):
        target = user_id if target is None else integer(target)
        with self.store.connect() as db:
            actor(db, user_id)
            if target != user_id:
                admin(db, user_id)
            row = db.execute('SELECT password_hash FROM team_users WHERE id=?', (target,)).fetchone()
            require(row is not None, '用户不存在。', 404)
        require(target != user_id or password_matches(old, row['password_hash']), '原密码不正确。', 403)
        hashed = password_hash(new)
        with self.store.connect(write=True) as db:
            actor(db, user_id)
            if target != user_id:
                admin(db, user_id)
            current = db.execute('SELECT password_hash FROM team_users WHERE id=?', (target,)).fetchone()
            require(current and current[0] == row['password_hash'], '密码已变化，请重新操作。', 409)
            db.execute('UPDATE team_users SET password_hash=? WHERE id=?', (hashed, target))
            db.execute('DELETE FROM team_sessions WHERE user_id=?', (target,))
            self.store.audit(db, user_id, 'reset_password' if target != user_id else 'change_password', 'user', target)
        return {'ok': True, 'reauthenticate': True}
