"""Shared output hashing and project path containment."""
import hashlib
import json
from pathlib import Path

def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True)

def scoped(base,value):
    root=Path(base).resolve(); result=(root/value).resolve()
    if not result.is_relative_to(root): raise ValueError('路径超出当前项目范围。')
    return result
