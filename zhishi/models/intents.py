"""Explicit task rules take precedence over optional model interpretation."""
import re
from datetime import timedelta
from zhishi.core.clock import deadline, now, stamp
from .client import ModelClient


class IntentRouter:
    def __init__(self, store, model=None):
        self.store = store
        self.model = model or ModelClient()

    def interpret(self, text):
        """Rules cover explicit pilot commands. Optional LLM only proposes structured intent."""
        if text in ("我的待办", "待办", "今天有什么事", "今天要做什么"):
            return {"action": "list"}
        match = re.fullmatch(r"(完成|取消)\s*#?(\d+)", text)
        if match:
            return {"action": "done" if match[1] == "完成" else "cancel", "id": int(match[2])}
        match = re.fullmatch(r"(\d+)\s*分钟后提醒\s*#?(\d+)", text)
        if match:
            return {"action": "snooze", "id": int(match[2]), "minutes": int(match[1])}
        match = re.fullmatch(r"(\d+)\s*(秒|分钟|小时|天)后提醒我\s*(.+)", text)
        if match:
            seconds = int(match[1]) * {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}[match[2]]
            if not 1 <= seconds <= 31536000:
                raise ValueError("提醒间隔须在 1 秒至 365 天之间。")
            return {"action": "create", "title": match[3], "due": stamp(now() + timedelta(seconds=seconds))}
        match = re.fullmatch(r"提醒我\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)", text)
        if match:
            return {"action": "create", "title": match[3], "due": stamp(deadline(match[1] + "T" + match[2]))}
        with self.store.connect() as db:
            config = self.store.settings(db)
        if config["mode"] != "model":
            return {"action": "clarify", "question": "请告诉我明确的事项和提醒时间。\n例如：30 分钟后提醒我回复客户；或：提醒我 2026-11-03 09:00 提交方案。\n管理已有事项可发送：我的待办、完成 1、取消 1、10 分钟后提醒 1。"}
        return self.model.extract(text, config["model"])
