"""Project authorization, optimistic task transitions and a fixed two-step workflow."""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from zhishi.core.clock import deadline, now, stamp
from .auth import actor, admin
from .errors import TeamError, require
from .security import integer, text


def ident(prefix):
    return prefix + secrets.token_hex(12)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class TeamService:
    def __init__(self, store, artifact_root):
        self.store = store
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def mutate(self, user_id, request_id, operation, payload, callback, *, guard=None):
        require(isinstance(request_id, str) and 8 <= len(request_id) <= 100,
                '缺少有效request_id（8至100个字符）。', 400)
        fingerprint = hashlib.sha256(encoded([operation, payload]).encode()).hexdigest()
        with self.store.connect(write=True) as db:
            user = actor(db, user_id)
            if guard:
                guard(db, user)
            saved = db.execute('SELECT * FROM team_requests WHERE user_id=? AND request_id=?', (user_id, request_id)).fetchone()
            if saved:
                require(saved['fingerprint'] == fingerprint, '该请求编号已用于其他操作。', 409, 'idempotency_conflict')
                return json.loads(saved['response'])
            result = callback(db, user)
            db.execute('INSERT INTO team_requests VALUES (?,?,?,?)', (user_id, request_id, fingerprint, encoded(result)))
            return result

    def project(self, db, project_id, user, *, manage=False, write=False):
        require(isinstance(project_id, str) and len(project_id) <= 100, '项目编号无效。', 400)
        row = db.execute('SELECT * FROM team_projects WHERE id=?', (project_id,)).fetchone()
        require(row is not None, '项目不存在。', 404)
        member = db.execute('SELECT role FROM team_members WHERE project_id=? AND user_id=?', (project_id, user['id'])).fetchone()
        require(user['role'] == 'admin' or member is not None)
        if manage:
            require(user['role'] == 'admin' or member['role'] == 'manager', '需要项目管理权限。')
        if write:
            require(user['role'] == 'admin' or member['role'] != 'viewer', '当前项目为只读权限。')
        return dict(row)

    def member(self, db, project_id, user_id, *, write=True):
        integer(user_id, '员工编号')
        row = db.execute('SELECT m.role,u.active FROM team_members m JOIN team_users u ON u.id=m.user_id '
                         'WHERE m.project_id=? AND m.user_id=?', (project_id, user_id)).fetchone()
        require(row and row['active'] and (not write or row['role'] != 'viewer'), '目标员工未获准参与此项目。', 400)

    def can_read(self, db, task, user):
        if user['role'] == 'admin':
            return True
        member = db.execute('SELECT role FROM team_members WHERE project_id=? AND user_id=?', (task['project_id'], user['id'])).fetchone()
        if not member:
            return False
        if member['role'] == 'manager' or user['id'] in (task['creator_id'], task['assignee_id'], task['reviewer_id']):
            return True
        if task['flow_id']:
            flow = db.execute('SELECT * FROM team_flows WHERE id=?', (task['flow_id'],)).fetchone()
            return user['id'] in (flow['creator_id'], flow['prepare_id'], flow['review_id'], flow['approver_id'])
        return False

    def task(self, db, task_id, user):
        require(isinstance(task_id, str) and len(task_id) <= 100, '任务编号无效。', 400)
        row = db.execute('SELECT * FROM team_tasks WHERE id=?', (task_id,)).fetchone()
        require(row is not None, '任务不存在。', 404)
        require(self.can_read(db, row, user))
        return dict(row)

    @staticmethod
    def version(row, data):
        require(type(data.get('expected_version')) is int and row['version'] == data['expected_version'],
                '版本已变化，请刷新后重新操作。', 409, 'version_conflict')

    def event(self, db, task, user_id, kind, detail=''):
        db.execute('INSERT INTO team_task_events(task_id,actor_id,kind,version,detail,created_at) VALUES (?,?,?,?,?,?)',
                   (task['id'], user_id, kind, task['version'], detail, stamp()))
        self.store.audit(db, user_id, kind, 'task', task['id'], {'version': task['version']})

    def notify(self, db, user_id, message, task=None):
        recipient = db.execute('SELECT id,role,active FROM team_users WHERE id=?', (user_id,)).fetchone()
        if not recipient or not recipient['active'] or (task and not self.can_read(db, task, recipient)):
            return None
        binding = db.execute('SELECT * FROM team_bindings WHERE user_id=? AND push_enabled=1', (user_id,)).fetchone()
        notification_id = ident('n_')
        db.execute('INSERT INTO team_notifications(id,user_id,task_id,task_version,text,state,binding_json,created_at) '
                   'VALUES (?,?,?,?,?,?,?,?)', (notification_id, user_id, task['id'] if task else None,
                   task['version'] if task else None, message, 'pending' if binding else 'delivered_local',
                   encoded(dict(binding)) if binding else None, stamp()))
        return notification_id

    def bump(self, db, task, user_id, state, kind, *, detail='', fields=None):
        changed = dict(task, state=state, version=task['version'] + 1, updated_at=stamp(), **(fields or {}))
        allowed = {'state', 'version', 'updated_at', 'assignee_id', 'reviewer_id', 'result_round', 'remind_at'}
        values = {k: v for k, v in changed.items() if k in allowed}
        assignments = ','.join(k + '=?' for k in values)
        cursor = db.execute(f'UPDATE team_tasks SET {assignments} WHERE id=? AND version=?',
                            (*values.values(), task['id'], task['version']))
        require(cursor.rowcount == 1, '任务版本已变化。', 409)
        db.execute("UPDATE team_notifications SET state='cancelled',detail='任务版本已变化。' WHERE task_id=? AND state='pending'",
                   (task['id'],))
        db.execute("UPDATE team_jobs SET state='stale',error='任务版本已变化。',updated_at=? WHERE task_id=? AND state='queued' AND task_version<>?",
                   (stamp(), task['id'], changed['version']))
        self.event(db, changed, user_id, kind, detail)
        return changed

    def create_project(self, user_id, data):
        def operation(db, user):
            require(user['role'] in ('admin', 'manager'), '需要管理员或经理账号。')
            project_id = ident('p_')
            name = text(data.get('name'), '项目名称', 150)
            description = data.get('description', '')
            require(isinstance(description, str) and len(description) <= 4000, '项目说明过长。', 400)
            db.execute('INSERT INTO team_projects VALUES (?,?,?,?,?)', (project_id, name, description, user_id, stamp()))
            db.execute('INSERT INTO team_members VALUES (?,?,?)', (project_id, user_id, 'manager'))
            self.store.audit(db, user_id, 'create_project', 'project', project_id)
            return dict(db.execute('SELECT * FROM team_projects WHERE id=?', (project_id,)).fetchone())
        return self.mutate(user_id, data.get('request_id'), 'create_project', data, operation)

    def projects(self, user_id):
        with self.store.connect() as db:
            user = actor(db, user_id)
            rows = db.execute('SELECT * FROM team_projects ORDER BY created_at') if user['role'] == 'admin' else db.execute(
                'SELECT p.* FROM team_projects p JOIN team_members m ON m.project_id=p.id WHERE m.user_id=? ORDER BY p.created_at', (user_id,))
            return [dict(row) for row in rows]

    def project_detail(self, user_id, project_id):
        with self.store.connect() as db:
            user = actor(db, user_id)
            result = self.project(db, project_id, user)
            result['members'] = [dict(row) for row in db.execute('SELECT m.user_id,m.role,u.username,u.display_name,u.active '
                'FROM team_members m JOIN team_users u ON u.id=m.user_id WHERE project_id=? ORDER BY user_id', (project_id,))]
            return result

    def set_member(self, user_id, project_id, target, data):
        def operation(db, user):
            project = self.project(db, project_id, user, manage=True)
            integer(target)
            role = data.get('role')
            require(role in ('manager', 'member', 'viewer', 'remove'), '成员角色无效。', 400)
            require(target != project['owner_id'] or role == 'manager', '项目创建者必须保留管理权限。', 409)
            require(db.execute('SELECT 1 FROM team_users WHERE id=? AND active=1', (target,)).fetchone(), '用户不存在或已停用。', 400)
            if role in ('viewer', 'remove'):
                active = db.execute("SELECT 1 FROM team_tasks WHERE project_id=? AND state NOT IN ('completed','cancelled') "
                                    'AND (assignee_id=? OR reviewer_id=?)', (project_id, target, target)).fetchone()
                flow = db.execute("SELECT 1 FROM team_flows WHERE project_id=? AND state NOT IN ('completed','cancelled') "
                                  'AND (prepare_id=? OR review_id=? OR approver_id=? OR creator_id=?)', (project_id, target, target, target, target)).fetchone()
                require(not active and not flow, '员工仍负责进行中的工作，请先交接或取消。', 409)
            if role == 'remove':
                db.execute('DELETE FROM team_members WHERE project_id=? AND user_id=?', (project_id, target))
            else:
                db.execute('INSERT INTO team_members VALUES (?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role',
                           (project_id, target, role))
            self.store.audit(db, user_id, 'set_project_member', 'project', project_id, {'user_id': target, 'role': role})
            return {'ok': True}
        return self.mutate(user_id, data.get('request_id'), 'member:' + project_id + ':' + str(target), data, operation)

    def insert_task(self, db, user, data, *, flow_id=None, stage=None):
        project_id = data.get('project_id')
        self.project(db, project_id, user, write=True)
        assignee, reviewer = data.get('assignee_id', user['id']), data.get('reviewer_id', user['id'])
        self.member(db, project_id, assignee)
        self.member(db, project_id, reviewer)
        if assignee != user['id']:
            self.project(db, project_id, user, manage=True)
        title = text(data.get('title'), '任务名称', 300)
        description = data.get('description', '')
        require(isinstance(description, str) and len(description) <= 4000, '任务说明过长。', 400)
        due = data.get('due_at')
        if due:
            due = deadline(due)
            require(due > now(), '截止时间须在未来。', 400)
            due = stamp(due)
        required = data.get('requires_artifact', True)
        require(type(required) is bool, 'requires_artifact须为布尔值。', 400)
        task_id = ident('t_')
        db.execute('INSERT INTO team_tasks(id,project_id,title,description,creator_id,assignee_id,reviewer_id,state,due_at,remind_at,'
                   'requires_artifact,flow_id,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (task_id, project_id, title, description, user['id'], assignee, reviewer, 'open', due, due,
                    int(required), flow_id, stage, stamp(), stamp()))
        task = dict(db.execute('SELECT * FROM team_tasks WHERE id=?', (task_id,)).fetchone())
        self.event(db, task, user['id'], 'create_task')
        self.notify(db, assignee, f'新任务：{title}。请在任务页接单。', task)
        return task

    def create_task(self, user_id, data):
        return self.mutate(user_id, data.get('request_id'), 'create_task', data, lambda db, u: self.insert_task(db, u, data))

    def tasks(self, user_id, project_id=None):
        with self.store.connect() as db:
            user = actor(db, user_id)
            if project_id:
                self.project(db, project_id, user)
            rows = db.execute('SELECT * FROM team_tasks' + (' WHERE project_id=?' if project_id else '') + ' ORDER BY created_at DESC',
                              (project_id,) if project_id else ())
            return [dict(row) for row in rows if self.can_read(db, row, user)]

    def detail(self, user_id, task_id):
        with self.store.connect() as db:
            user = actor(db, user_id)
            task = self.task(db, task_id, user)
            task['events'] = [dict(row) for row in db.execute('SELECT * FROM team_task_events WHERE task_id=? ORDER BY id', (task_id,))]
            task['artifacts'] = [self.artifact_info(row) for row in db.execute('SELECT * FROM team_artifacts WHERE task_id=? ORDER BY created_at', (task_id,))]
            task['inputs'] = [self.artifact_info(row) for row in db.execute('SELECT a.* FROM team_inputs i JOIN team_artifacts a ON a.id=i.artifact_id WHERE i.task_id=?', (task_id,))]
            task['jobs'] = [dict(row) for row in db.execute('SELECT id,skill,state,error,result_json,task_version,created_at FROM team_jobs WHERE task_id=? ORDER BY created_at', (task_id,))]
            return task

    @staticmethod
    def artifact_info(row):
        return {key: row[key] for key in ('id', 'task_id', 'name', 'sha256', 'bytes', 'kind', 'task_version', 'result_round', 'created_at')}

    def file(self, row):
        path = (self.artifact_root / row['storage_key']).resolve()
        require(path.is_relative_to(self.artifact_root) and path.is_file(), '成果文件不可用。', 409)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], '成果内容已变化，拒绝验收或下载。', 409)
        return path

    def read_file(self, row):
        path = (self.artifact_root / row['storage_key']).resolve()
        require(path.is_relative_to(self.artifact_root) and path.is_file(), '成果文件不可用。', 409)
        content = path.read_bytes()
        require(hashlib.sha256(content).hexdigest() == row['sha256'], '成果内容已变化。', 409)
        return content

    def outputs(self, db, task):
        rows = db.execute("SELECT * FROM team_artifacts WHERE task_id=? AND kind='output' AND result_round=? ORDER BY created_at",
                          (task['id'], task['result_round'])).fetchall()
        require(rows or not task['requires_artifact'], '尚未提交本轮成果文件。', 409)
        for row in rows:
            self.file(row)
        for row in db.execute("SELECT * FROM team_artifacts WHERE task_id=? AND kind='input'", (task['id'],)):
            self.file(row)
        for row in db.execute('SELECT a.*,i.sha256 AS expected_sha FROM team_inputs i JOIN team_artifacts a ON a.id=i.artifact_id WHERE i.task_id=?', (task['id'],)):
            require(row['sha256'] == row['expected_sha'], '上游输入版本不一致。', 409)
            self.file(row)
        return rows

    def task_action(self, user_id, task_id, action, data, *, binding_guard=None):
        def guard(db, user):
            if binding_guard:
                current = db.execute('SELECT user_id,account_id,peer_id,generation FROM team_bindings WHERE user_id=?', (user_id,)).fetchone()
                require(current and dict(current) == binding_guard, '微信绑定已变化，请通过当前绑定重新操作。', 403)
        def operation(db, user):
            task = self.task(db, task_id, user)
            self.version(task, data)
            self.project(db, task['project_id'], user, write=True)
            require(task['state'] not in ('completed', 'cancelled'), '任务已结束。', 409)
            if action == 'accept':
                require(user_id == task['assignee_id'], '只有执行人可以接单。')
                require(task['state'] == 'open', '当前阶段不能接单。', 409)
                return self.bump(db, task, user_id, 'in_progress', 'accept_task')
            if action == 'submit':
                require(user_id == task['assignee_id'], '只有执行人可以提交成果。')
                require(task['state'] == 'in_progress', '请先接单，再提交成果。', 409)
                self.outputs(db, task)
                task = self.bump(db, task, user_id, 'awaiting_review', 'submit_task')
                self.notify(db, task['reviewer_id'], '请核对任务本版成果：' + task['title'], task)
                return task
            if action in ('approve', 'reject'):
                require(user_id == task['reviewer_id'] or user['role'] == 'admin', '只有验收人可以确认本版成果。')
                require(task['state'] == 'awaiting_review', '当前没有待验收成果。', 409)
                if action == 'reject':
                    reason = text(data.get('reason'), '返工原因', 1000)
                    task = self.bump(db, task, user_id, 'in_progress', 'reject_task', detail=reason,
                                     fields={'result_round': task['result_round'] + 1})
                    self.notify(db, task['assignee_id'], '任务需要返工：' + reason, task)
                    return task
                self.outputs(db, task)
                task = self.bump(db, task, user_id, 'completed', 'approve_task', fields={'remind_at': None})
                self.notify(db, task['creator_id'], '任务成果已通过验收：' + task['title'], task)
                if task['flow_id']:
                    self.advance_flow(db, task, user_id)
                return task
            if action == 'assign':
                require(not task['flow_id'], '固定流程的负责人不能中途静默替换；请取消后重建流程。', 409)
                self.project(db, task['project_id'], user, manage=True)
                require(task['state'] in ('open', 'in_progress'), '等待验收时不能更换执行人。', 409)
                target = data.get('assignee_id')
                self.member(db, task['project_id'], target)
                task = self.bump(db, task, user_id, 'open', 'assign_task', fields={'assignee_id': target,
                                 'result_round': task['result_round'] + 1})
                self.notify(db, target, '任务已交给你：' + task['title'], task)
                return task
            if action == 'snooze':
                require(user_id == task['assignee_id'], '只有执行人可以稍后提醒。')
                minutes = integer(data.get('minutes'), '分钟数')
                require(minutes <= 525600, '最多延后365天。', 400)
                from datetime import timedelta
                return self.bump(db, task, user_id, task['state'], 'snooze_task', fields={'remind_at': stamp(now() + timedelta(minutes=minutes))})
            if action == 'cancel':
                require(not task['flow_id'], '请通过流程取消入口结束固定流程。', 409)
                if user_id != task['creator_id']:
                    self.project(db, task['project_id'], user, manage=True)
                return self.bump(db, task, user_id, 'cancelled', 'cancel_task', fields={'remind_at': None})
            raise TeamError('不支持这个任务操作。')
        return self.mutate(user_id, data.get('request_id'), f'task:{task_id}:{action}', data, operation, guard=guard)

    def upload(self, user_id, task_id, data, content):
        require(isinstance(content, bytes) and 0 < len(content) <= 10_000_000, '文件须在1字节至10MB之间。', 400)
        name = text(data.get('name'), '文件名', 120)
        require(name == Path(name).name and not any(c in name for c in '/\\:\r\n<>"|?*') and
                not any(ord(c) < 32 for c in name) and not name.startswith('.'), '文件名无效。', 400)
        require(Path(name).suffix.lower() in ('.xlsx', '.csv', '.json', '.txt', '.md', '.png', '.pdf'), '文件类型不支持。', 400)
        kind = data.get('kind', 'output')
        require(kind in ('input', 'output'), '成果类型无效。', 400)
        created = []
        checksum = hashlib.sha256(content).hexdigest()
        payload = {k: v for k, v in data.items() if k != 'content_base64'}
        payload['content_sha256'] = checksum
        def operation(db, user):
            task = self.task(db, task_id, user)
            self.version(task, data)
            require(user_id == task['assignee_id'] and task['state'] == 'in_progress', '只有正在执行的负责人可以上传。')
            self.project(db, task['project_id'], user, write=True)
            artifact_id, key = ident('a_'), secrets.token_hex(24)
            path = self.artifact_root / key
            with path.open('xb') as stream:
                stream.write(content)
            created.append(path)
            task = self.bump(db, task, user_id, task['state'], 'upload_artifact')
            db.execute('INSERT INTO team_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (artifact_id, task_id, name, key, checksum, len(content), kind, user_id,
                        task['version'], task['result_round'], stamp()))
            row = db.execute('SELECT * FROM team_artifacts WHERE id=?', (artifact_id,)).fetchone()
            return {'artifact': self.artifact_info(row), 'task_version': task['version']}
        try:
            return self.mutate(user_id, data.get('request_id'), 'upload:' + task_id, payload, operation)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def download(self, user_id, artifact_id):
        with self.store.connect(write=True) as db:
            user = actor(db, user_id)
            row = db.execute('SELECT * FROM team_artifacts WHERE id=?', (artifact_id,)).fetchone()
            require(row is not None, '文件不存在。', 404)
            task = db.execute('SELECT * FROM team_tasks WHERE id=?', (row['task_id'],)).fetchone()
            allowed = self.can_read(db, task, user)
            if not allowed:
                allowed = any(self.can_read(db, item, user) for item in db.execute(
                    'SELECT t.* FROM team_tasks t JOIN team_inputs i ON i.task_id=t.id WHERE i.artifact_id=?', (artifact_id,)))
            require(allowed)
            content = self.read_file(row)
            self.store.audit(db, user_id, 'download_artifact', 'artifact', artifact_id)
            return row['name'], content

    def start_flow(self, user_id, data):
        def operation(db, user):
            project_id = data.get('project_id')
            self.project(db, project_id, user, manage=True)
            a, b, c = (data.get(k) for k in ('prepare_id', 'review_id', 'approver_id'))
            for target in (a, b, c):
                self.member(db, project_id, target)
            require(a != b and b != c, '相邻执行和验收阶段必须由不同员工负责。', 400)
            require(len({a, b, c}) == 3 or data.get('allow_shared_roles') is True,
                    '两人兼任最终审批时须显式声明allow_shared_roles。', 400)
            title, flow_id = text(data.get('title'), '流程名称', 200), ident('f_')
            db.execute('INSERT INTO team_flows VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (flow_id, project_id, title, user_id, a, b, c, 'preparing', 1, stamp(), stamp()))
            task = self.insert_task(db, user, {'project_id': project_id, 'title': title + '：整理',
                                    'assignee_id': a, 'reviewer_id': b, 'requires_artifact': True}, flow_id=flow_id, stage=1)
            self.store.audit(db, user_id, 'start_flow', 'flow', flow_id)
            return {'id': flow_id, 'state': 'preparing', 'version': 1, 'task': task}
        return self.mutate(user_id, data.get('request_id'), 'start_flow', data, operation)

    def advance_flow(self, db, task, approver):
        flow = dict(db.execute('SELECT * FROM team_flows WHERE id=?', (task['flow_id'],)).fetchone())
        if task['stage'] == 1:
            require(flow['state'] == 'preparing', '流程阶段不一致。', 409)
            creator = actor(db, flow['creator_id'])
            downstream = self.insert_task(db, creator, {'project_id': flow['project_id'], 'title': flow['title'] + '：复核',
                                          'assignee_id': flow['review_id'], 'reviewer_id': flow['approver_id']},
                                          flow_id=flow['id'], stage=2)
            for artifact in self.outputs(db, task):
                db.execute('INSERT INTO team_inputs VALUES (?,?,?)', (downstream['id'], artifact['id'], artifact['sha256']))
            state = 'reviewing'
        else:
            require(flow['state'] == 'reviewing', '流程阶段不一致。', 409)
            state = 'awaiting_approval'
            self.notify(db, flow['approver_id'], '全部成果已验收，请在流程页作最终批准：' + flow['title'], task)
        db.execute('UPDATE team_flows SET state=?,version=version+1,updated_at=? WHERE id=?', (state, stamp(), flow['id']))
        self.store.audit(db, approver, 'handoff_flow', 'flow', flow['id'], {'state': state, 'source_version': task['version']})

    def flow(self, db, flow_id, user):
        require(isinstance(flow_id, str) and len(flow_id) <= 100, '流程编号无效。', 400)
        row = db.execute('SELECT * FROM team_flows WHERE id=?', (flow_id,)).fetchone()
        require(row is not None, '流程不存在。', 404)
        self.project(db, row['project_id'], user)
        member = db.execute('SELECT role FROM team_members WHERE project_id=? AND user_id=?', (row['project_id'], user['id'])).fetchone()
        require(user['role'] == 'admin' or (member and member['role'] == 'manager') or user['id'] in
                (row['creator_id'], row['prepare_id'], row['review_id'], row['approver_id']))
        return dict(row)

    def flow_detail(self, user_id, flow_id):
        with self.store.connect() as db:
            user = actor(db, user_id)
            flow = self.flow(db, flow_id, user)
            flow['tasks'] = [dict(t) for t in db.execute('SELECT * FROM team_tasks WHERE flow_id=? ORDER BY stage', (flow_id,))]
            return flow

    def flow_action(self, user_id, flow_id, action, data):
        def operation(db, user):
            flow = self.flow(db, flow_id, user)
            self.version(flow, data)
            require(flow['state'] not in ('completed', 'cancelled'), '流程已结束。', 409)
            # Cancel the previous stage's pending prompts before creating any new
            # rework notice, otherwise a final rejection cancels its own handoff.
            db.execute("UPDATE team_notifications SET state='cancelled',detail='流程状态已变化。' WHERE state='pending' "
                       'AND task_id IN (SELECT id FROM team_tasks WHERE flow_id=?)', (flow_id,))
            if action in ('approve', 'reject'):
                require(user_id == flow['approver_id'], '只有最终审批人可以批准或退回。')
                require(flow['state'] == 'awaiting_approval', '尚未进入最终审批。', 409)
                tasks = db.execute('SELECT * FROM team_tasks WHERE flow_id=? ORDER BY stage', (flow_id,)).fetchall()
                require(len(tasks) == 2 and all(t['state'] == 'completed' for t in tasks), '缺少已验收阶段。', 409)
                for task in tasks:
                    self.outputs(db, task)
                if action == 'approve':
                    state = 'completed'
                    self.notify(db, flow['creator_id'], '流程已最终批准：' + flow['title'])
                else:
                    reason = text(data.get('reason'), '退回原因', 1000)
                    task = self.bump(db, dict(tasks[1]), user_id, 'in_progress', 'reopen_flow_review',
                                     detail=reason, fields={'result_round': tasks[1]['result_round'] + 1})
                    self.notify(db, task['assignee_id'], '最终审批退回：' + reason, task)
                    state = 'reviewing'
            elif action == 'cancel':
                if user_id != flow['creator_id']:
                    self.project(db, flow['project_id'], user, manage=True)
                for task in db.execute("SELECT * FROM team_tasks WHERE flow_id=? AND state NOT IN ('completed','cancelled')", (flow_id,)).fetchall():
                    self.bump(db, dict(task), user_id, 'cancelled', 'cancel_flow_task', fields={'remind_at': None})
                state = 'cancelled'
            else:
                raise TeamError('流程操作无效。')
            db.execute('UPDATE team_flows SET state=?,version=version+1,updated_at=? WHERE id=?', (state, stamp(), flow_id))
            self.store.audit(db, user_id, action + '_flow', 'flow', flow_id, {'version': flow['version'] + 1})
            return {'id': flow_id, 'state': state, 'version': flow['version'] + 1}
        return self.mutate(user_id, data.get('request_id'), f'flow:{flow_id}:{action}', data, operation)

    def notifications(self, user_id):
        with self.store.connect() as db:
            actor(db, user_id)
            return [dict(row) for row in db.execute('SELECT id,task_id,task_version,text,state,detail,read_at,created_at '
                                                  'FROM team_notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 200', (user_id,))]

    def read_notification(self, user_id, notification_id):
        with self.store.connect(write=True) as db:
            actor(db, user_id)
            require(db.execute('SELECT 1 FROM team_notifications WHERE id=? AND user_id=?', (notification_id, user_id)).fetchone(), '通知不存在。', 404)
            db.execute('UPDATE team_notifications SET read_at=COALESCE(read_at,?) WHERE id=?', (stamp(), notification_id))
            return {'ok': True}

    def audit_logs(self, user_id, limit=100):
        with self.store.connect() as db:
            admin(db, user_id)
            return [dict(row) for row in db.execute('SELECT * FROM team_audit ORDER BY id DESC LIMIT ?', (min(max(int(limit), 1), 500),))]
