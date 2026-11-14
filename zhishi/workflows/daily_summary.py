"""Read-only daily task overview for the pilot's bound conversations."""
import json
from zhishi.config import OWNER
from zhishi.core.clock import now, deadline
from zhishi.workflows.ai_dispatch.service import LABELS
from zhishi.workflows.ai_dispatch.plans import load


INVOICE_LABELS = {
    'manager_wait': '等待负责人确认', 'export_pending': '等待取数和导出',
    'employee1_wait': '等待员工1核对', 'employee2_wait': '等待员工2接收',
    'employee2_received': '等待员工2核对', 'failed': '取数或导出失败，需处理',
    'complete': '已完成',
}
PRODUCT_LABELS = {'manager_wait':'等待负责人确认', 'employee1_wait':'等待员工1准备',
                  'processing':'正在核查', 'employee2_wait':'等待员工2核对',
                  'failed':'核查失败，需处理', 'complete':'已完成'}


def local_day(value):
    if not value:
        return None
    try:
        return deadline(value).date()
    except (ValueError, TypeError):
        return None


def daily_summary(pilot, wechat_role=None):
    today = now().date()
    employee2 = wechat_role == 'employee2'
    transport = pilot.employee2_transport if employee2 else pilot.transport
    route = transport.route() if transport and hasattr(transport, 'route') else None
    pending, done, unknown_time = [], [], []
    tables = set()
    with pilot.connect() as db:
        tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def append(name, state, created, changed, *, failed=False, complete=False, warning=False):
            if complete:
                if local_day(changed) == today:
                    detail = '已完成，汇报发送状态需核对' if warning else '已完成'
                    done.append(f'{name}：{detail}')
            else:
                label = state + ('；通知发送状态需核对' if warning else '')
                carry = '（此前未完成）' if local_day(created) and local_day(created) < today else ''
                pending.append(f'{name}：{label}{carry}')

        if not employee2:
            for row in db.execute('SELECT * FROM tasks WHERE owner=? ORDER BY due,id', (OWNER,)):
                due = local_day(row['due'])
                created = local_day(row['created_at'])
                if row['status'] == 'open' and (due and due <= today or created == today):
                    note = '已到期，未完成' if due and due < today else ('今日待办' if due == today else f'待办，截止 {due}')
                    pending.append(f'待办#{row["id"]} {row["title"]}：{note}')
                elif row['status'] == 'done':
                    completed = local_day(row['completed_at'])
                    if completed == today:
                        done.append(f'待办#{row["id"]} {row["title"]}：已完成')
                    elif completed is None and (due == today or created == today):
                        unknown_time.append(f'待办#{row["id"]} {row["title"]}：已完成，完成日期未记录')

        if 'invoice_demo_runs' in tables:
            for row in db.execute('SELECT * FROM invoice_demo_runs ORDER BY rowid DESC'):
                d = json.loads(row['detail'])
                if row['state'] == 'cancelled':
                    continue
                if employee2 and (not route or d.get('employee2') != route):
                    continue
                state = row['state']
                latest = db.execute("SELECT state FROM invoice_demo_notices WHERE code=? AND kind NOT LIKE '%old%' ORDER BY id DESC LIMIT 1", (row['code'],)).fetchone()
                warn = bool(latest and latest['state'] in ('unknown','blocked'))
                created = row['code'][3:7]+'-'+row['code'][7:9]+'-'+row['code'][9:11]+'T00:00:00+08:00' if row['code'].startswith('FP-') else None
                append(d.get('display_name') or '发票任务（历史）', INVOICE_LABELS.get(state,state), created,
                       row['updated'], complete=state=='complete', warning=warn)

        if 'ai_dispatch_runs' in tables:
            for row in db.execute('SELECT * FROM ai_dispatch_runs ORDER BY rowid DESC'):
                if row['state'] == 'cancelled':
                    continue
                task, routes = json.loads(row['task_json']), json.loads(row['routes_json'])
                if employee2 and (not route or not any(task['roles'].get(role)=='employee2' and routes.get(role)==route for role in task['roles'])):
                    continue
                state = row['state']
                notice = db.execute("SELECT state FROM ai_dispatch_notices WHERE code=? AND revision=? AND state IN ('unknown','blocked') LIMIT 1", (row['code'], row['revision'])).fetchone()
                name = task.get('display_name') or f"商品排查（{row['created'][:10]}）"
                append(name, LABELS.get(state,state), row['created'], row['updated'],
                       complete=state=='complete', warning=bool(notice))

        if 'product_demo_runs' in tables:
            for row in db.execute('SELECT * FROM product_demo_runs ORDER BY rowid DESC'):
                data = json.loads(row['detail'])
                if employee2 and (not route or data.get('employee2_route') != route):
                    continue
                state = row['state']
                notice = db.execute("SELECT state FROM product_demo_notices WHERE code=? ORDER BY id DESC LIMIT 1", (row['code'],)).fetchone()
                append('商品排查演示 '+row['code'], PRODUCT_LABELS.get(state,state), None, row['updated'],
                       complete=state=='complete', warning=bool(notice and notice['state'] in ('unknown','blocked')))

        if not employee2 and getattr(pilot, 'workbench', None):
            # Legacy workflows have their own history and state labels.
            for item in pilot.workbench._legacy():
                if item['status'] == 'cancelled':
                    continue
                append(item['display_name'], item['state_label'], item['created_at'], item['updated_at'],
                       complete=item['status']=='done', warning=item['status']=='blocked')

        if not employee2 and pilot.ai_dispatch:
            try:
                for task in load(pilot.ai_dispatch.plan_path):
                    schedule = task['schedule']
                    if not task['enabled'] or schedule['kind'] == 'manual':
                        continue
                    if schedule['kind'] == 'once':
                        target = deadline(schedule['at'])
                        if target.date() != today:
                            continue
                        slot = target.isoformat()
                        when = target.strftime('%H:%M')
                    else:
                        slot, when = today.isoformat(), schedule['at']
                    if not db.execute('SELECT 1 FROM ai_dispatch_runs WHERE task_id=? AND slot=?', (task['id'],slot)).fetchone():
                        pending.append(f"{task['title']}：今日 {when} 计划启动，尚未创建流程")
            except ValueError:
                pending.append('定时任务配置无法读取，请检查设置')

    lines = [f'【{today:%m月%d日}任务清单】', '范围：今天相关事项及此前未完成事项。']
    if employee2:
        lines.append('仅显示与员工2微信绑定的协作任务。')
    for label, items in (('未完成或需处理', pending), ('今日已完成', done), ('完成日期未记录', unknown_time)):
        lines.append(f'\n{label}（{len(items)}项）')
        shown = 0
        for item in items[:20]:
            line = '• ' + (item[:117] + '…' if len(item) > 118 else item)
            if sum(len(part) for part in lines) + len(line) > 1550:
                break
            lines.append(line)
            shown += 1
        if len(items) > shown:
            lines.append(f'还有 {len(items)-shown} 项未在此消息中展开。')
        if not items:
            lines.append('无')
    return '\n'.join(lines)
