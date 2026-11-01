"""Validate and persist the selected model alias without exposing credentials."""
import os
from zhishi.config import model_aliases

def configure(store, lock, data):
    mode = data.get("mode")
    model = data.get("model")
    aliases = model_aliases()
    if mode not in ("rules", "model") or model not in aliases:
        raise ValueError("无效的模式或模型。")
    if mode == "model" and not os.environ.get("MODEL_BASE_URL"):
        raise ValueError("模型尚未连接。请先设置 MODEL_BASE_URL 和密钥，再重启服务。")
    with lock, store.connect() as db:
        db.execute("UPDATE settings SET value=? WHERE key='mode'", (mode,))
        db.execute("UPDATE settings SET value=? WHERE key='model'", (model,))
