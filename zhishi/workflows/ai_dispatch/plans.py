"""Read the administrator's task list; dates use Beijing time."""
import json
import re
from datetime import datetime

from zhishi.core.clock import CN, deadline

ROLES = ('manager', 'employee1', 'employee2')
SKILLS = {'employee1': 'product.inputs.v1', 'employee2': 'product.compare.v1', 'final': 'product.verify.v1'}


def load(path):
    if not path.exists():
        return []
    try:
        tasks = json.loads(path.read_text('utf-8'))['tasks']
        if not isinstance(tasks, list) or len(tasks) > 50:
            raise ValueError()
        seen = set()
        for task in tasks:
            if not isinstance(task, dict) or not isinstance(task.get('roles'), dict) or not isinstance(task.get('schedule'), dict):
                raise ValueError()
            ident = task['id']
            if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9-]{1,60}', ident) or ident in seen:
                raise ValueError()
            seen.add(ident)
            if (type(task['enabled']) is not bool or not isinstance(task['title'], str)
                    or not 1 <= len(task['title']) <= 150 or task['skills'] != SKILLS
                    or task['source'] not in ('file', 'database_products')
                    or not isinstance(task['input_directory'], str) or not task['input_directory']
                    or set(task['roles']) != set(ROLES)
                    or any(channel not in ('local', 'primary', 'employee2') for channel in task['roles'].values())):
                raise ValueError()
            channels = list(task['roles'].values())
            if 'local' in channels and set(channels) != {'local'}:
                raise ValueError('一个流程不能混用模拟角色和真实微信。')
            real = [c for c in channels if c != 'local']
            if len(set(real)) != len(real) and task.get('allow_shared_routes') is not True:
                raise ValueError('当前只有两条微信绑定，复用角色需显式声明 allow_shared_routes。')
            schedule = task['schedule']
            kind = schedule['kind']
            if kind == 'once':
                deadline(schedule['at'])
            elif kind == 'daily':
                if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['at']):
                    raise ValueError()
            elif kind != 'manual':
                raise ValueError()
        return tasks
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError('AI 任务清单格式无效，请检查 tasks.json；未创建新流程。') from exc


def due_slot(task, current):
    schedule = task['schedule']
    current = current.astimezone(CN)
    if schedule['kind'] == 'manual':
        return None
    if schedule['kind'] == 'once':
        target = deadline(schedule['at'])
        return target.isoformat() if current >= target else None
    hour, minute = map(int, schedule['at'].split(':'))
    target = datetime.combine(current.date(), datetime.min.time(), CN).replace(hour=hour, minute=minute)
    return current.date().isoformat() if current >= target else None
