"""All business times are explicitly in Asia/Shanghai (UTC+8)."""
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

def now():
    return datetime.now(CN)

def stamp(value=None):
    return (value or now()).isoformat(timespec="seconds")

def deadline(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("请输入有效日期和时间。") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=CN)
    return result.astimezone(CN)

timestamp = stamp
