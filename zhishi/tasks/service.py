"""Create and update tasks; preserve deadlines and ownership checks."""
from datetime import timedelta
from zhishi.config import OWNER
from zhishi.core.clock import deadline, now, stamp


class TaskService:
    def __init__(self, store):
        self.store = store

    def create(self, db, title, due):
        title = title.strip()
        if not title or len(title) > 300:
            raise ValueError("事项名称须为 1–300 个字符。")
        due = deadline(due)
        if due <= now():
            raise ValueError("提醒时间已经过去，请选择未来时间。")
        cursor = db.execute("INSERT INTO tasks(owner,title,due,next_at,created_at) VALUES (?,?,?,?,?)",
                            (OWNER, title, stamp(due), stamp(due), stamp()))
        ident = cursor.lastrowid
        reply = f"已记录 #{ident} {title}\n北京时间 {due:%m月%d日 %H:%M:%S} 提醒你。"
        self.store.message(db, "assistant", reply)
        return reply

    def change(self, db, ident, action, minutes=10):
        task = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (ident, OWNER)).fetchone()
        if not task:
            raise ValueError("找不到这条事项，请先发送“我的待办”。")
        if task["status"] != "open":
            raise ValueError("这条事项已经完成或取消。")
        db.execute("UPDATE outbox SET state='cancelled' WHERE task_id=? AND state='pending'", (ident,))
        if action in ("done", "cancel"):
            state = "done" if action == "done" else "cancelled"
            db.execute("UPDATE tasks SET status=?,next_at=NULL,version=version+1 WHERE id=? AND owner=?",
                       (state, ident, OWNER))
            reply = f"#{ident} 已{'完成' if action == 'done' else '取消'}，后续提醒已停止。"
        elif action == "snooze":
            minutes = int(minutes)
            if not 1 <= minutes <= 60 * 24 * 365:
                raise ValueError("稍后提醒的时间须在 1 分钟至 365 天之间。")
            target = now() + timedelta(minutes=minutes)
            db.execute("UPDATE tasks SET next_at=?,version=version+1 WHERE id=? AND owner=?",
                       (stamp(target), ident, OWNER))
            reply = f"#{ident} 改为北京时间 {target:%m月%d日 %H:%M} 再提醒；原截止时间保留。"
        else:
            raise ValueError("不支持这个操作。")
        self.store.message(db, "assistant", reply)
        return reply
