"""Durable reminder outbox; channel calls happen outside transactions."""
from zhishi.config import OWNER
from zhishi.core.clock import deadline, stamp


class ReminderService:
    def __init__(self, store, transport, lock):
        self.store = store
        self.transport = transport
        self.lock = lock

    def tick(self):
        """Atomically put due work into an outbox; an unchanged version is reminded once."""
        with self.lock, self.store.connect() as db:
            tasks = db.execute("SELECT * FROM tasks WHERE owner=? AND status='open' AND next_at IS NOT NULL AND next_at<=?",
                               (OWNER, stamp())).fetchall()
            for task in tasks:
                text = f"待办提醒 #{task['id']}\n{task['title']}\n原截止：{deadline(task['due']):%m月%d日 %H:%M}\n回复“完成 {task['id']}”或“10 分钟后提醒 {task['id']}”。"
                channel = 'wechat' if self.transport and self.transport.enabled else 'demo'
                db.execute("INSERT OR IGNORE INTO outbox(owner,task_id,version,text,state,created_at,channel) VALUES (?,?,?,?,?,?,?)",
                           (OWNER, task["id"], task["version"], text, "pending", stamp(), channel))
                db.execute("UPDATE tasks SET next_at=NULL WHERE id=? AND version=?", (task["id"], task["version"]))
            pending = db.execute("SELECT o.* FROM outbox o JOIN tasks t ON t.id=o.task_id WHERE o.state='pending' AND t.owner=? AND t.status='open' AND t.version=o.version", (OWNER,)).fetchall()
            for row in pending:
                if row['channel'] == 'wechat':
                    if self.transport and self.transport.enabled and self.transport.route():
                        db.execute("UPDATE outbox SET state='sending' WHERE id=? AND state='pending'", (row['id'],))
                    continue
                self.store.message(db, "reminder", row["text"])
                db.execute("UPDATE outbox SET state='demo_delivered' WHERE id=?", (row["id"],))
            claimed = [dict(row) for row in pending if row['channel'] == 'wechat' and self.transport
                       and self.transport.enabled and self.transport.route()]
        # Network calls happen outside the database transaction.
        for row in claimed:
            with self.lock, self.store.connect() as db:
                active = db.execute("SELECT 1 FROM tasks WHERE id=? AND owner=? AND status='open' AND version=?",
                                    (row['task_id'], OWNER, row['version'])).fetchone()
                if not active:
                    db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row['id'],))
                    continue
            state, detail = self.transport.send(row['text'])
            with self.lock, self.store.connect() as db:
                db.execute("UPDATE outbox SET state=?,detail=? WHERE id=?", (state, detail, row['id']))
                self.store.message(db, 'reminder', row['text'] + '\n\n[微信状态] ' + detail)
