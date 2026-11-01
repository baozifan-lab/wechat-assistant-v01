"""Model provider adapter using a compatible Chat Completions endpoint."""
import json
import os
import urllib.error
import urllib.request
from zhishi.config import model_aliases
from zhishi.core.clock import stamp


class ModelClient:
    def extract(self, text, model):
        base = os.environ.get("MODEL_BASE_URL", "").rstrip("/")
        key = os.environ.get("MODEL_API_KEY", "")
        if not base:
            raise ValueError("尚未设置模型服务地址，请切回规则模式，或配置 MODEL_BASE_URL。")
        allowed = model_aliases()
        if model not in allowed:
            raise ValueError("所选模型不在服务端允许列表中。")
        instruction = (
            "你是待办意图提取器，只返回一个 JSON 对象，不要 Markdown。当前北京时间：" + stamp() +
            '。可用动作：{"action":"create","title":"事项","due":"带时区ISO时间"}；'
            '{"action":"list"}；{"action":"done","id":整数}；'
            '{"action":"cancel","id":整数}；{"action":"snooze","id":整数,"minutes":整数}；'
            '{"action":"clarify","question":"澄清问题"}。'
            "没有明确事项编号的完成、取消、延期必须澄清；禁止猜测事项编号；时间不明确必须澄清。"
        )
        data = json.dumps({"model": model, "messages": [
            {"role": "system", "content": instruction}, {"role": "user", "content": text}],
            "temperature": 0, "max_tokens": 450}).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(base + "/chat/completions", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read(1000000))
            value = json.loads(payload["choices"][0]["message"]["content"])
        except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError):
            raise ValueError("模型暂时未返回有效结果，未修改待办。可用明确指令重试。") from None
        if not isinstance(value, dict):
            raise ValueError("模型返回格式不正确，未修改待办。")
        return value
