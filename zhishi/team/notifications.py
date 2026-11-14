"""Durable delivery claims; uncertain sends require an explicit human retry."""
from datetime import timedelta
import json
import secrets

from zhishi.core.clock import now, stamp
from .auth import actor
from .errors import require


class NotificationService:
    def __init__(self, service, sender=None):
        self.service, self.store, self.sender = service, service.store, sender

    def binding_valid(self, db, row):
        user = db.execute('SELECT id,role,active FROM team_users WHERE id=?', (row['user_id'],)).fetchone()
        binding = db.execute('SELECT * FROM team_bindings WHERE user_id=?', (row['user_id'],)).fetchone()
        expected = json.loads(row['binding_json']) if row['binding_json'] else None
        if not user or not user['active'] or not expected or not binding or dict(binding) != expected:
            return False
        if row['task_id']:
            task = db.execute('SELECT * FROM team_tasks WHERE id=?', (row['task_id'],)).fetchone()
            # Delivered inbox information remains readable; stale push actions are not sent.
            if not task or task['version'] != row['task_version']:
                return False
            if not self.service.can_read(db, task, user):
                return False
        return True

    def claim(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE team_notifications SET state='unknown',detail='发送租约过期，结果未知，不自动重发。' "
                       "WHERE state='sending' AND lease_until<=?", (stamp(),))
            for row in db.execute("SELECT * FROM team_notifications WHERE state='pending' ORDER BY created_at LIMIT 50").fetchall():
                if not self.binding_valid(db, row):
                    db.execute("UPDATE team_notifications SET state='cancelled',detail='绑定、用户或任务版本已变化。' WHERE id=?", (row['id'],))
                    continue
                token = secrets.token_hex(24)
                db.execute("UPDATE team_notifications SET state='sending',delivery_token=?,lease_until=? WHERE id=? AND state='pending'",
                           (token, stamp(now() + timedelta(seconds=180)), row['id']))
                return dict(db.execute('SELECT * FROM team_notifications WHERE id=?', (row['id'],)).fetchone())

    def tick(self):
        row = self.claim()
        if not row:
            return
        with self.store.connect() as db:
            allowed = self.binding_valid(db, row)
        if not allowed:
            state, detail = 'cancelled', '发送前授权已变化。'
        elif self.sender is None:
            state, detail = 'blocked', '真实微信发送未启用，通知已保存在站内。'
        else:
            try:
                state, detail = self.sender(json.loads(row['binding_json']), row['text'])
                if state not in ('submitted_unverified', 'unknown', 'blocked'):
                    state, detail = 'unknown', '发送适配器返回了无法确认的结果。'
            except Exception:
                state, detail = 'unknown', '微信调用中断，结果未知，不自动重发。'
        with self.store.connect(write=True) as db:
            changed = db.execute("UPDATE team_notifications SET state=?,detail=? WHERE id=? AND state='sending' AND delivery_token=? AND lease_until>?",
                                 (state, detail, row['id'], row['delivery_token'], stamp()))
            if changed.rowcount:
                self.store.audit(db, None, 'notification_' + state, 'notification', row['id'])

    def retry(self, user_id, notification_id, data):
        def operation(db, user):
            row = db.execute('SELECT * FROM team_notifications WHERE id=?', (notification_id,)).fetchone()
            require(row and (row['user_id'] == user_id or user['role'] == 'admin'), '通知不存在。', 404)
            require(data.get('confirmed_not_received') is True, '先核对手机，并明确确认未收到。', 400)
            require(row['state'] in ('unknown', 'blocked'), '当前通知不允许重试。', 409)
            require(self.binding_valid(db, row), '任务阶段或微信绑定已变化，不能重发旧通知。', 409)
            db.execute("UPDATE team_notifications SET state='pending',detail='本人核对未收到，已人工重试。',delivery_token=NULL,lease_until=NULL WHERE id=?", (notification_id,))
            self.store.audit(db, user_id, 'retry_notification', 'notification', notification_id)
            return {'id': notification_id, 'state': 'pending'}
        return self.service.mutate(user_id, data.get('request_id'), 'retry_notification:' + notification_id, data, operation)

    def reminders(self):
        with self.store.connect(write=True) as db:
            tasks = db.execute("SELECT * FROM team_tasks WHERE state IN ('open','in_progress','awaiting_review') AND remind_at<=?", (stamp(),)).fetchall()
            for task in tasks:
                recipient = task['reviewer_id'] if task['state'] == 'awaiting_review' else task['assignee_id']
                user = db.execute('SELECT active FROM team_users WHERE id=?', (recipient,)).fetchone()
                if user and user['active']:
                    self.service.notify(db, recipient, '任务到期提醒：' + task['title'], task)
                    db.execute('UPDATE team_tasks SET remind_at=NULL WHERE id=? AND version=?', (task['id'], task['version']))
