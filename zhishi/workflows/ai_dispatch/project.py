"""Explicit project brief input, snapshotted with each workflow run."""
import hashlib
from pathlib import Path


def read_brief(plan_path, task):
    base = Path(plan_path).parent.resolve()
    name = task.get('project_document', '项目说明.md')
    if not isinstance(name, str) or not name:
        raise ValueError('项目说明文件名无效。')
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() != '.md':
        raise ValueError('项目说明必须是当前项目目录中的 Markdown 文件。')
    if not path.exists() and 'project_document' not in task:
        return None
    try:
        if path.stat().st_size > 24000:
            raise ValueError('项目说明超过 24 KB，请精简后重新开始。')
        raw = path.read_bytes()
        text = raw.decode('utf-8').strip()
    except (OSError, UnicodeError):
        raise ValueError('无法读取项目说明，请检查指定文件。') from None
    if not text:
        raise ValueError('项目说明内容为空。')
    return {'name': path.name, 'text': text, 'sha256': hashlib.sha256(raw).hexdigest()}
