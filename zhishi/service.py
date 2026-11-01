"""Application coordinator for chat, tasks, model configuration and workflows."""
import json
import os
import re
import threading
from zhishi.config import OWNER, model_aliases
from zhishi.core.clock import deadline, stamp
from zhishi.core.database import Database
from zhishi.models.intents import IntentRouter
from zhishi.models.settings import configure
from zhishi.tasks.service import TaskService
from zhishi.tasks.reminders import ReminderService
from zhishi.channels.base import NotificationChannel


class Pilot:
    def __init__(self, database, transport: NotificationChannel | None = None):
        self.database = str(database)
        self.transport = transport
        self.workflow = None
        self.two_stage = None
        self.abc = None
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.store = Database(database)
        self.tasks = TaskService(self.store)
        self.intents = IntentRouter(self.store)
        self.reminders = ReminderService(self.store, transport, self.lock)

    def connect(self):
        return self.store.connect()

    message = staticmethod(Database.message)

    def settings(self, db):
        return self.store.settings(db)

    def create(self, db, title, due):
        return self.tasks.create(db, title, due)

    def change(self, db, ident, action, minutes=10):
        return self.tasks.change(db, ident, action, minutes)

    def interpret(self, text):
        return self.intents.interpret(text)

    def tick(self):
        return self.reminders.tick()

    def configure(self, data):
        return configure(self.store, self.lock, data)

    def chat(self, text, request_id):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError("消息须为 1–2000 个字符。")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError("缺少有效的请求编号。")
        text = text.strip()
        # Prevent duplicate user mutations if a browser re-sends the same request.
        with self.request_lock:
            with self.connect() as db:
                saved = db.execute("SELECT response FROM requests WHERE id=?", (request_id,)).fetchone()
                if saved:
                    return json.loads(saved[0])
            for handler in (self.workflow, self.two_stage, self.abc):
                if handler is None:
                    continue
                reply = handler.command(text)
                if reply is not None:
                    result = {"reply": reply}
                    with self.connect() as db:
                        self.message(db, "user", text)
                        self.message(db, "assistant", reply)
                        db.execute("INSERT INTO requests VALUES (?,?)", (request_id, json.dumps(result, ensure_ascii=False)))
                    return result
            intent = self.interpret(text)
            with self.lock, self.connect() as db:
                # Model-generated updates are confined to this user's real task numbers.
                action = intent.get("action")
                if action in ("done", "cancel", "snooze"):
                    ident = intent.get("id")
                    explicit = re.search(r"(?<!\d)#?" + re.escape(str(ident)) + r"(?!\d)", text)
                    if not isinstance(ident, int) or not explicit:
                        intent = {"action": "clarify", "question": "请带上事项编号，例如：完成 1。"}
                        action = "clarify"
                self.message(db, "user", text)
                if action == "create":
                    reply = self.create(db, intent.get("title", ""), intent.get("due", ""))
                elif action in ("done", "cancel", "snooze"):
                    reply = self.change(db, intent["id"], action, intent.get("minutes", 10))
                elif action == "list":
                    tasks = db.execute("SELECT * FROM tasks WHERE owner=? AND status='open' ORDER BY due,id", (OWNER,)).fetchall()
                    reply = "当前没有未完成事项。" if not tasks else "你有这些待办：\n" + "\n".join(
                        f"#{t['id']} {t['title']} · {deadline(t['due']):%m-%d %H:%M}" for t in tasks)
                    self.message(db, "assistant", reply)
                else:
                    reply = str(intent.get("question", "请补充明确的事项与时间。"))[:1000]
                    self.message(db, "assistant", reply)
                result = {"reply": reply}
                db.execute("INSERT INTO requests VALUES (?,?)", (request_id, json.dumps(result, ensure_ascii=False)))
                return result

    def state(self):
        with self.connect() as db:
            config = self.settings(db)
            config.update({"model_ready": bool(os.environ.get("MODEL_BASE_URL")),
                           "aliases": model_aliases()})
            return {"tasks": [dict(r) for r in db.execute("SELECT * FROM tasks WHERE owner=? ORDER BY status='open' DESC,due,id", (OWNER,))],
                    "messages": [dict(r) for r in db.execute("SELECT * FROM (SELECT * FROM messages WHERE owner=? ORDER BY id DESC LIMIT 100) ORDER BY id", (OWNER,))],
                    "outbox": [dict(r) for r in db.execute("SELECT * FROM outbox WHERE owner=? ORDER BY id DESC LIMIT 30", (OWNER,))],
                    "settings": config, "now": stamp(), "channel": "wechat" if self.transport and self.transport.enabled else "demo",
                    "wechat": self.transport.status() if self.transport else {"enabled":False,"bound":False},
                    "employee": "个人试点"}
