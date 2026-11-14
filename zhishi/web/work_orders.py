"""Persistent staff assignments and scheduled launches from the workbench."""
import json
import re
import secrets
import threading

from zhishi.core.clock import now, stamp, deadline


class WorkOrders:
    def __init__(self, pilot):
        self.pilot = pilot
        self.lock = threading.RLock()
        with pilot.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS work_orders (
                  code TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                  title TEXT NOT NULL, due TEXT NOT NULL, template TEXT NOT NULL,
                  assignments TEXT NOT NULL, roles TEXT NOT NULL, state TEXT NOT NULL,
                  linked_id TEXT, error TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
                  created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_order_staff (
                  code TEXT NOT NULL, account_id TEXT NOT NULL, name TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', delivery TEXT NOT NULL DEFAULT 'pending',
                  route TEXT, detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(code,account_id));
                CREATE TABLE IF NOT EXISTS task_conversations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                  role TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL);
            ''')
            db.execute("UPDATE work_order_staff SET delivery='unknown',detail='服务重启前发送结果未知，请核对微信。' WHERE delivery='sending'")

    def record(self, db, ident, role, text):
        db.execute('INSERT INTO task_conversations(task_id,role,text,at) VALUES (?,?,?,?)',
                   (ident, role, text, stamp()))

    def channel(self, account_id):
        row = self.pilot.accounts.row(account_id)
        return self.pilot.accounts.transport(row), row

    def create(self, data):
        request_id = data.get('request_id')
        title, template = data.get('title', ''), data.get('template', 'custom')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('缺少有效的请求编号。')
        fingerprint = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self.pilot.connect() as db:
            saved = db.execute('SELECT code,fingerprint FROM work_orders WHERE request_id=?', (request_id,)).fetchone()
        if saved:
            if saved['fingerprint'] != fingerprint:
                raise ValueError('请求编号已用于其他内容。')
            return {'id': 'order:' + saved['code'], 'reply': '这项安排已保存。'}
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
            raise ValueError('请填写 1–300 字的任务内容。')
        due = deadline(data.get('due'))
        if due < now():
            raise ValueError('请选择未来的安排时间。')
        staff, roles = data.get('assignees', []), data.get('roles', {})
        if not isinstance(staff, list) or len(staff) > 100 or any(not isinstance(a, str) for a in staff) or len(set(staff)) != len(staff):
            raise ValueError('请选择有效员工。')
        if not isinstance(roles, dict):
            raise ValueError('请选择员工分工。')
        if template == 'custom':
            if not staff:
                raise ValueError('请至少选择一位执行员工。')
        else:
            templates = {t['id']: t for t in self.pilot.workbench.state()['templates']}
            if template not in templates:
                raise ValueError('找不到已有任务模板。')
            if set(roles) != {'manager', 'employee1', 'employee2'}:
                raise ValueError('请为负责人、员工1和员工2分别选择员工。')
            staff = list(dict.fromkeys(roles.values()))
        people = [self.pilot.accounts.row(ident) for ident in staff]
        fingerprint = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self.lock, self.pilot.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT code,fingerprint FROM work_orders WHERE request_id=?', (request_id,)).fetchone()
            if saved:
                if saved['fingerprint'] != fingerprint:
                    raise ValueError('请求编号已用于其他内容。')
                return {'id': 'order:' + saved['code'], 'reply': '这项安排已保存。'}
            code = 'TASK-' + now().strftime('%Y%m%d') + '-' + secrets.token_hex(4).upper()
            db.execute('INSERT INTO work_orders(code,request_id,fingerprint,title,due,template,assignments,roles,state,created,updated) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (code, request_id, fingerprint, title.strip(), stamp(due), template, json.dumps(staff),
                        json.dumps(roles), 'scheduled', stamp(), stamp()))
            for person in people:
                db.execute('INSERT INTO work_order_staff(code,account_id,name) VALUES (?,?,?)', (code, person['id'], person['name']))
            self.record(db, 'order:' + code, 'assistant', f'已安排：{title.strip()}。执行员工：' + '、'.join(p['name'] for p in people))
        return {'id': 'order:' + code, 'reply': '任务已安排，将在指定时间派发。'}

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.pilot.connect() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM work_orders WHERE state='scheduled' AND due<=? ORDER BY due", (stamp(),))]
            for row in rows:
                try:
                    linked = None
                    if row['template'] != 'custom':
                        people = {role: self.pilot.accounts.row(ident) for role, ident in json.loads(row['roles']).items()}
                        channels = {role: person['channel'] for role, person in people.items()}
                        names = {role: person['name'] for role, person in people.items()}
                        if row['template'] == 'invoice-demo':
                            linked = 'invoice:' + self.pilot.invoice_demo.start_assigned(row['request_id'], channels, names)
                        else:
                            linked = 'ai:' + self.pilot.ai_dispatch.start(row['template'], row['request_id'], roles=channels, staff_names=names)
                    with self.pilot.connect() as db:
                        db.execute('UPDATE work_orders SET state=?,linked_id=?,revision=revision+1,updated=? WHERE code=?',
                                   ('launched' if linked else 'open', linked, stamp(), row['code']))
                        self.record(db, 'order:' + row['code'], 'assistant', '已启动已有任务：' + linked if linked else '已到安排时间，等待员工接单。')
                except ValueError as exc:
                    with self.pilot.connect() as db:
                        db.execute("UPDATE work_orders SET state='blocked',error=?,revision=revision+1,updated=? WHERE code=?", (str(exc), stamp(), row['code']))
                        self.record(db, 'order:' + row['code'], 'assistant', '派发受阻：' + str(exc))
            with self.pilot.connect() as db:
                items = [dict(r) for r in db.execute("SELECT s.*,o.title FROM work_order_staff s JOIN work_orders o USING(code) WHERE o.state='open' AND s.delivery='pending'")]
            for item in items:
                try:
                    channel, _ = self.channel(item['account_id'])
                    if not channel or not channel.enabled or not channel.route():
                        raise ValueError('员工微信未连接，可在任务详情记录处理情况，连接后重试派单。')
                    route = channel.route()
                    with self.pilot.connect() as db:
                        db.execute("UPDATE work_order_staff SET delivery='sending',route=? WHERE code=? AND account_id=?", (json.dumps(route), item['code'], item['account_id']))
                    text = f'【任务 {item["code"]}】{item["title"]}\n请回复：{item["code"]} 收到，或 {item["code"]} 完成。'
                    delivery, detail = channel.send_package(text, [], expected_route=route)
                except ValueError as exc:
                    delivery, detail = 'blocked', str(exc)
                except Exception:
                    delivery, detail = 'unknown', '发送结果未知，请先核对微信，不自动重发。'
                with self.pilot.connect() as db:
                    db.execute('UPDATE work_order_staff SET delivery=?,detail=? WHERE code=? AND account_id=?', (delivery, detail, item['code'], item['account_id']))
        finally:
            self.lock.release()

    def update(self, data):
        ident, action, person = data.get('id', ''), data.get('action'), data.get('account_id')
        with self.lock, self.pilot.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM work_orders WHERE code=?', (ident.removeprefix('order:'),)).fetchone()
            if not row or data.get('revision') != row['revision']:
                raise ValueError('任务已变化，请刷新后操作。')
            if action == 'retry' and row['state'] == 'blocked':
                db.execute("UPDATE work_orders SET state='scheduled',error='',revision=revision+1 WHERE code=?", (row['code'],))
            elif action == 'cancel' and row['state'] in ('scheduled', 'blocked', 'open'):
                db.execute("UPDATE work_orders SET state='cancelled',revision=revision+1,updated=? WHERE code=?", (stamp(), row['code']))
            elif row['state'] == 'open' and action in ('ack', 'done', 'retry-delivery'):
                staff = db.execute('SELECT * FROM work_order_staff WHERE code=? AND account_id=?', (row['code'], person)).fetchone()
                if not staff:
                    raise ValueError('该员工未参与此任务。')
                if action == 'retry-delivery':
                    if staff['delivery'] != 'blocked':
                        raise ValueError('仅受阻且未发送的通知可以重试；发送结果未知时请先核对微信。')
                    db.execute("UPDATE work_order_staff SET delivery='pending' WHERE code=? AND account_id=?", (row['code'], person))
                else:
                    if staff['state'] == 'done':
                        raise ValueError('该员工已完成，请刷新任务。')
                    db.execute('UPDATE work_order_staff SET state=? WHERE code=? AND account_id=?', ('done' if action == 'done' else 'acknowledged', row['code'], person))
                complete = not db.execute("SELECT 1 FROM work_order_staff WHERE code=? AND state<>'done'", (row['code'],)).fetchone()
                db.execute('UPDATE work_orders SET state=?,revision=revision+1,updated=? WHERE code=?', ('done' if complete else 'open', stamp(), row['code']))
                self.record(db, ident, 'user', staff['name'] + '：' + {'ack': '已接单', 'done': '已完成', 'retry-delivery': '重新派单'}[action] + ('（管理员代记）' if not data.get('_wechat') else ''))
            else:
                raise ValueError('当前阶段不支持此操作。')
        return {'reply': '任务已更新。'}

    def command(self, text, channel):
        match = re.fullmatch(r'(TASK-\d{8}-[A-F0-9]{8})\s*(收到|完成)', text, re.I)
        if not match:
            return None
        code = match[1].upper()
        with self.pilot.connect() as db:
            row = db.execute('SELECT revision FROM work_orders WHERE code=?', (code,)).fetchone()
            staff = [dict(s) for s in db.execute('SELECT * FROM work_order_staff WHERE code=?', (code,))]
        for person in staff:
            bridge, account = self.channel(person['account_id'])
            if account['channel'] == channel and bridge and bridge.enabled and person['route'] and json.loads(person['route']) == bridge.route():
                return self.update({'id': 'order:' + code, 'action': 'ack' if match[2] == '收到' else 'done',
                                    'account_id': person['account_id'], 'revision': row['revision'], '_wechat': True})['reply']
        raise ValueError('请使用被安排员工的绑定微信处理任务。')

    def items(self):
        with self.pilot.connect() as db:
            result = []
            for row in db.execute('SELECT * FROM work_orders ORDER BY created DESC'):
                status = 'done' if row['state'] in ('done', 'launched') else 'blocked' if row['state'] == 'blocked' else 'cancelled' if row['state'] == 'cancelled' else 'open'
                staff = [dict(s) for s in db.execute('SELECT account_id,name,state,delivery,detail FROM work_order_staff WHERE code=?', (row['code'],))]
                if row['state'] == 'open' and any(p['delivery'] in ('blocked','unknown') and p['state'] != 'done' for p in staff):
                    status = 'blocked'
                result.append({'id': 'order:' + row['code'], 'code': row['code'], 'kind': 'order', 'kind_label': '员工安排',
                    'title': row['title'], 'template':row['template'], 'state': row['state'], 'state_label': {'scheduled': '等待派发', 'open': '员工处理中', 'blocked': '派发受阻', 'launched': '已启动关联任务', 'done': '全部完成', 'cancelled': '已取消'}[row['state']],
                    'status': status, 'revision': row['revision'], 'created_at': row['created'], 'updated_at': row['updated'],
                    'due': row['due'], 'next_at': row['due'] if row['state'] == 'scheduled' else None,
                    'owner': '、'.join(p['name'] for p in staff), 'staff': staff, 'linked_id': row['linked_id'],
                    'summary': '已有任务安排' if row['template'] != 'custom' else '由所选员工执行；全部员工完成后任务结束。',
                    'error': row['error'], 'actions': [],
                    'messages': [dict(m) for m in db.execute('SELECT * FROM task_conversations WHERE task_id=? ORDER BY id', ('order:' + row['code'],))]})
            return result
