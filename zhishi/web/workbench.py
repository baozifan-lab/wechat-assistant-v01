"""Present existing business services through the four-page workbench contract."""
import json
import re
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlencode

from zhishi.workflows.ai_dispatch.service import WAITING, ROLE_NAMES, expected_role
from zhishi.workflows.daily_summary import INVOICE_LABELS


DELIVERY_LABELS = {
    'pending': '等待发送', 'sending': '正在提交', 'demo_delivered': '本地模拟消息',
    'submitted_unverified': '已提交微信，送达未确认', 'unknown': '发送结果未知',
    'blocked': '发送受阻', 'cancelled': '已取消', 'delivered_local': '已投递本地收件箱',
}


class Workbench:
    def __init__(self, pilot, root):
        self.pilot, self.root = pilot, Path(root)

    def _channel(self, name, transport):
        directory = self.root / 'data'
        file = directory / ('wechat-login.json' if name == 'primary' else 'employee2-wechat/wechat-login.json')
        try:
            login = json.loads(file.read_text('utf-8'))
            if not isinstance(login, dict):
                raise ValueError('invalid login state')
        except (OSError, ValueError):
            login = {'state': 'not_started', 'detail': '尚未开始连接。'}
        status = transport.status() if transport else {'enabled': False, 'bound': False}
        connection = status.get('connection_state')
        if connection and login.get('state') not in ('starting', 'waiting_scan', 'waiting_confirm', 'refreshing'):
            login = {**login, 'detail': status.get('connection_detail', login.get('detail'))}
        # Account IDs, session routes and bridge credentials remain server-side.
        return {'id': name, 'name': '主微信' if name == 'primary' else '员工2微信',
                'available': transport is not None,
                'bound': bool(status.get('bound')), 'enabled': bool(status.get('enabled')),
                'online': bool(status.get('bound')) and bool(status.get('enabled'))
                          and status.get('gateway_ready', status.get('gateway_listening')) is True,
                'connection_state': connection,
                'login': {key: login.get(key) for key in ('state', 'detail', 'qr_version')},
                'qr_url': '/wechat-login.png' if name == 'primary' else '/wechat2-qr.png'}

    @staticmethod
    def _reminder(task):
        state = task['status']
        return {'id': f'reminder:{task["id"]}', 'kind': 'reminder', 'kind_label': '待办提醒',
                'title': task['title'], 'state': state,
                'state_label': {'open': '进行中', 'done': '已完成', 'cancelled': '已取消'}[state],
                'status': 'done' if state == 'done' else 'cancelled' if state == 'cancelled' else 'open',
                'created_at': task['created_at'], 'updated_at': task['created_at'],
                'due': task['due'], 'next_at': task['next_at'], 'owner': '本人',
                'summary': '到点提醒，完成或取消后停止后续提醒。',
                'actions': ['done', 'snooze', 'cancel'] if state == 'open' else []}

    @staticmethod
    def _ai(run):
        waiting = run['state'] in WAITING
        role = expected_role(run['state']) if waiting else None
        local = waiting and run['task']['roles'][role] == 'local'
        latest = run['replies'][-1] if run['replies'] else None
        can_retry = bool(latest and latest['state'] == 'failed' and latest['revision'] == run['revision'])
        attention = run['state'] == 'blocked' or can_retry or any(
            n['state'] in ('unknown', 'blocked') and n['revision'] == run['revision'] for n in run['notices'])
        status = 'cancelled' if run['state'] == 'cancelled' else 'done' if run['state'] == 'complete' else 'blocked' if attention else 'open'
        actions = (['reply'] if local else []) + (['retry'] if can_retry else []) + (['close'] if status not in ('done','cancelled') else [])
        summary = run['evidence'].get('employee2', run['evidence'].get('employee1', {})).get('summary', '')
        messages = [{'role': 'assistant', 'text': n['text'], 'status': DELIVERY_LABELS.get(n['state'], n['state']),
                     'sequence': n['sequence'], 'at': n['at'], 'estimated': n['estimated'],
                     'detail': n['detail']} for n in run['notices'] if n['state'] != 'cancelled']
        messages += [{'role': 'user', 'text': ROLE_NAMES.get(r['role'], r['role']) + '：' + r['text'],
                      'status': {'pending': '等待理解', 'processing': '正在理解', 'done': '已处理',
                                 'failed': '处理失败'}.get(r['state'], r['state']),
                      'detail': r['result'] if r['state'] == 'failed' else '已按当前任务处理。' if r['state'] == 'done' else '',
                      'at': r['at'], 'sequence': r['sequence'], 'estimated': r['estimated']} for r in run['replies']]
        return {'id': 'ai:' + run['code'], 'kind': 'ai', 'kind_label': 'AI 协作',
                'title': run['task']['title'], 'display_name': run['display_name'],
                'state': run['state'], 'state_label': run['state_label'], 'status': status,
                'revision': run['revision'],
                'code': run['code'], 'messages': sorted(messages, key=lambda m: m['sequence']),
                'created_at': run['created'], 'updated_at': run['updated'], 'due': None, 'next_at': None,
                'owner': ROLE_NAMES.get(role, '系统'), 'summary': summary or run['state_label'],
                'error': run['error'], 'actions': actions,
                'reply_role': role, 'reply_channel': run['task']['roles'].get(role),
                'reply_hint': ('本地模拟' + ROLE_NAMES.get(role, '') + '：可直接回复“收到”“好了”“没问题”。'
                               if local else '请由' + ROLE_NAMES[role] + '在绑定微信中回复。' if waiting
                               else '当前阶段由系统处理或已结束，暂不接受回复。'),
                'notices': messages[:len([n for n in run['notices'] if n['state'] != 'cancelled'])],
                'replies': messages[len([n for n in run['notices'] if n['state'] != 'cancelled']):],
                'events': run['events'], 'project_brief': run['task'].get('project_brief'),
                'download': '/api/ai-dispatch/asset?' + urlencode({'code': run['code']})
                            if run['evidence'].get('employee2') else None,
                'retry_request_id': latest['request_id'] if can_retry else None,
                'channel_label': '本地模拟' if set(run['task']['roles'].values()) == {'local'} else '；'.join(
                    ROLE_NAMES[role] + ' → ' + run['task'].get('staff_names', {}).get(role, {'primary': '主微信', 'employee2': '员工2微信', 'local': '本地模拟'}.get(channel, '所选员工'))
                    for role, channel in run['task']['roles'].items())}

    def _legacy(self):
        """Expose historical workflows without requiring their retired pages."""
        result = []
        services = [('work', self.pilot.workflow, 'workflow_runs', '文档工作流'),
                    ('two', self.pilot.two_stage, 'two_stage_runs', '两段式验证'),
                    ('abc', self.pilot.abc, 'abc_runs', '三角色模拟')]
        for kind, service, table, label in services:
            if service is None:
                continue
            with self.pilot.connect() as db:
                rows = [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY created_at DESC')]
            for row in rows:
                state = row['state']
                notice_table, notice_key = ('workflow_notices', 'run_uid') if kind == 'work' else (table.replace('_runs', '_notices'), 'run_uid')
                with self.pilot.connect() as db:
                    notices = [dict(n) for n in db.execute(f'SELECT kind,state,detail FROM {notice_table} WHERE {notice_key}=? ORDER BY id', (row['uid'],))]
                complete = state in ('delivered', 'completed')
                blocked = state in ('failed', 'blocked', 'rejected') or any(n['state'] in ('unknown', 'blocked') for n in notices)
                if kind == 'abc' and state == 'completed':
                    complete = any(n['kind'] == 'C_report' and n['state'] == 'submitted_unverified' for n in notices)
                cancelled = state in ('superseded','cancelled')
                if kind == 'work':
                    plan = json.loads(row['plan_json'])
                    title, state_label = plan['title'], service.label(state)
                    reports = [json.loads(row['report_json'])]
                    actions = ['approve', 'reject'] if state == 'awaiting_review' else []
                    files = [(None, a) for a in json.loads(row['assets_json'])]
                    token, endpoint = row['review_token'], '/api/workflow/asset'
                else:
                    from zhishi.workflows.two_stage import LABELS as TWO_LABELS
                    from zhishi.workflows.abc_dispatch import LABELS as ABC_LABELS
                    title, state_label = label, (TWO_LABELS if kind == 'two' else ABC_LABELS).get(state, state)
                    reports = [json.loads(row['a_report_json']), json.loads(row['b_report_json'])]
                    actions = ['approve'] if state in ('a_review', 'b_review', 'a_waiting', 'b_waiting') else []
                    files = [(stage, a) for stage in ('A', 'B') for a in json.loads(row[stage.lower() + '_assets_json'])]
                    token, endpoint = row['access_token'], '/api/two-stage/asset' if kind == 'two' else '/api/abc/asset'
                if not cancelled and state not in ('delivered','completed'):
                    actions.append('close')
                assets = []
                for stage, asset in files:
                    query = {'uid': row['uid'], 'name': asset['name'], 'access': token}
                    if stage:
                        query['stage'] = stage
                    assets.append({'name': asset['name'], 'url': endpoint + '?' + urlencode(query)})
                result.append({'id': kind + ':' + row['uid'], 'kind': kind, 'kind_label': label,
                               'title': title, 'display_name': f'{title} · {row["code"]} V{row["version"]}',
                               'state': state, 'state_label': state_label,
                               'status': 'cancelled' if cancelled else 'blocked' if blocked else 'done' if complete else 'open',
                               'created_at': row['created_at'], 'updated_at': row.get('updated_at') or row['created_at'],
                               'due': row.get('scheduled_at'), 'next_at': row.get('scheduled_at') if state == 'scheduled' else None,
                               'owner': '本人模拟角色', 'summary': state_label, 'error': row['error'],
                               'actions': actions, 'reports': reports, 'assets': assets, 'notices': notices,
                               'code': row['code'], 'version': row['version']})
        return result

    def _invoices(self):
        if not self.pilot.invoice_demo:
            return []
        result = []
        with self.pilot.connect() as db:
            for row in db.execute('SELECT * FROM invoice_demo_runs ORDER BY rowid DESC'):
                d = json.loads(row['detail'])
                notices = [dict(n) for n in db.execute('SELECT kind,state,detail FROM invoice_demo_notices WHERE code=? ORDER BY id', (row['code'],))]
                state = row['state']
                current = {'manager_wait':'manager','export_pending':'employee1_start','employee1_wait':'employee1_review',
                           'employee2_wait':'employee2_review','employee2_received':'employee2_review',
                           'complete':'manager_done','failed':'failure'}.get(state)
                notice = next((n for n in reversed(notices) if n['kind'] == current), None)
                warning = bool(notice and notice['state'] in ('unknown','blocked'))
                status = 'cancelled' if state == 'cancelled' else 'blocked' if state == 'failed' or warning else 'done' if state == 'complete' else 'open'
                label = '已关闭' if state == 'cancelled' else INVOICE_LABELS.get(state, state)
                if state == 'complete' and notice and notice['state'] in ('pending','sending'):
                    status, label = 'open', '核对完成，等待向负责人汇报'
                owner = {'manager_wait':'负责人','export_pending':'系统','employee1_wait':'员工1',
                         'employee2_wait':'员工2','employee2_received':'员工2','complete':'系统','failed':'系统'}.get(state,'系统')
                title = d.get('display_name') or ('发票测试 · '+row['code'][3:11])
                start = row['code'][3:11]
                created = f'{start[:4]}-{start[4:6]}-{start[6:8]}T00:00:00+08:00' if re.fullmatch(r'\d{8}', start) else row['updated']
                result.append({'id':'invoice:'+row['code'],'kind':'invoice','kind_label':'发票测试',
                    'title':title,'display_name':title,'state':state,'state_label':label,'status':status,
                    'created_at':created,'updated_at':row['updated'],'due':None,'next_at':None,'owner':owner,
                    'summary':f"模拟日期：{d.get('start','未知')} 至 {d.get('end','未知')}（含首尾）" + (f"；{d['count']} 条样本" if 'count' in d else ''),
                    'error':'取数或导出失败，请检查数据库。' if state=='failed' else '',
                    'actions':['close'] if status not in ('done','cancelled') and state != 'complete' else [],'reports':[],'assets':[],'notices':notices,
                    'download':'/api/console/invoice/asset?'+urlencode({'code':row['code']}) if d.get('file') and d.get('sha256') else None,
                    'reply_hint':f'请由{owner}在绑定微信中处理；日期为模拟数据。' if state not in ('failed','complete','cancelled') else '',
                    'extraction':d.get('extraction'), 'comparison':d.get('extraction_comparison'),
                    'code':row['code'], 'file_revision':d.get('review_revision',1),
                    'messages':[dict(m) for m in db.execute('SELECT role,text,at FROM invoice_conversations WHERE code=? ORDER BY id',(row['code'],))]})
        return result

    def start_invoice(self, request_id):
        with self.pilot.connect() as db:
            numbers = [int(m.group(1)) for row in db.execute('SELECT detail FROM invoice_demo_runs')
                       if (m := re.fullmatch(r'发票([1-9]\d*)', json.loads(row['detail']).get('display_name','')))]
        return self.pilot.chat(f'开始发票{max(numbers, default=0)+1}', request_id)

    def state(self):
        base = self.pilot.state()
        ai = self.pilot.ai_dispatch.state(limit=None) if self.pilot.ai_dispatch else {'tasks': [], 'runs': [], 'model': {}, 'config_error': ''}
        items = [self._reminder(t) for t in base['tasks']] + [self._ai(r) for r in ai['runs']] + self._invoices() + self._legacy()
        if getattr(self.pilot, 'work_orders', None):
            items += self.pilot.work_orders.items()
        for item in items:
            item.setdefault('code', item['id'].split(':',1)[1])
            if item['kind'] in ('ai', 'invoice') and getattr(self.pilot, 'corrections', None):
                item['file_versions'] = self.pilot.corrections.history(item['id'])
                item['can_replace'] = item['status'] != 'cancelled' and (item['kind'] == 'ai' or bool(item.get('download')))
        items.sort(key=lambda t: (t['updated_at'], t['id']), reverse=True)
        counts = {key: sum(t['status'] == key for t in items) for key in ('open', 'done', 'blocked', 'cancelled')}
        counts['scheduled'] = sum(bool(t.get('next_at')) and t['status'] == 'open' for t in items)
        templates = []
        for task in ai['tasks']:
            active = any(r['task_id'] == task['id'] and r['state'] not in ('complete', 'blocked','cancelled') for r in ai['runs'])
            can_name = task['id'] == 'product-investigation'
            reason = '已有进行中的流程' if active and not can_name else '任务尚未启用' if not task['enabled'] else '请先配置 AI 接口' if not ai['model'].get('ready') else ''
            numbers = [int(m.group(1)) for run in ai['runs'] if (m := re.fullmatch(r'商品核查([1-9]\d*)', run['task'].get('display_name','')))]
            templates.append({'id': task['id'], 'title': task['title'], 'schedule': task['schedule'],
                              'can_start': not reason, 'start_hint': reason,
                              'next_display_name': f'商品核查{max(numbers, default=0)+1}' if can_name else None,
                              'channel_label': '本地模拟' if set(task['roles'].values()) == {'local'} else '绑定微信协作'})
        if self.pilot.invoice_demo:
            invoice_active = any(item['kind']=='invoice' and item['state'] not in ('complete','failed','cancelled') for item in items)
            templates.append({'id':'invoice-demo','title':'开发票（模拟日期测试）','schedule':{'kind':'manual'},
                              'can_start':not invoice_active,'start_hint':'已有进行中的发票任务' if invoice_active else '',
                              'channel_label':'负责人和员工1使用主微信；员工2使用第二微信'})
        channels = [self._channel('primary', self.pilot.transport), self._channel('employee2', self.pilot.employee2_transport)]
        employees = [{'id':r['id'],'name':r['name']} for r in self.pilot.accounts.rows()] if getattr(self.pilot, 'accounts', None) else []
        return {'now': base['now'], 'dashboard': counts, 'tasks': items, 'templates': templates, 'employees': employees,
                'messages': base['messages'], 'outbox': base['outbox'],
                'settings': {'workflow_model': ai['model'], 'reminder': base['settings'],
                             'channels': channels, 'task_config_error': ai['config_error']}}

    def close_task(self, data):
        ident, state, request_id = data.get('id'), data.get('state'), data.get('request_id')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('缺少有效的操作请求编号。')
        if not isinstance(ident, str) or not isinstance(state, str):
            raise ValueError('请选择有效任务。')
        with self.pilot.request_lock:
            if ident.startswith('ai:') and self.pilot.ai_dispatch:
                revision = data.get('revision')
                if type(revision) is not int:
                    raise ValueError('任务版本缺失，请刷新后重试。')
                reply = self.pilot.ai_dispatch.cancel(ident[3:], state, revision)
            elif ident.startswith('invoice:') and self.pilot.invoice_demo:
                reply = self.pilot.invoice_demo.cancel(ident[8:], state)
            elif ident.split(':',1)[0] in ('work','two','abc'):
                reply = self._close_legacy(ident, state)
            else:
                raise ValueError('此类任务暂不支持关闭。')
        return {'reply':reply}

    def _close_legacy(self, ident, expected_state):
        kind, uid = ident.split(':',1)
        service = {'work':self.pilot.workflow,'two':self.pilot.two_stage,'abc':self.pilot.abc}[kind]
        if not service or not uid:
            raise ValueError('找不到任务。')
        table, notices = {'work':('workflow_runs','workflow_notices'), 'two':('two_stage_runs','two_stage_notices'),
                          'abc':('abc_runs','abc_notices')}[kind]
        locks = ([service.tick_lock] if kind=='work' else
                 [service.worker_lock, service.notifications.lock] if kind=='two' else
                 [service.worker_lock, service.notice_lock])
        with ExitStack() as stack:
            for lock in locks:
                if not lock.acquire(blocking=False):
                    raise ValueError('任务正在执行或发送通知，请稍后再关闭。')
                stack.callback(lock.release)
            with service.lock, self.pilot.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute(f'SELECT state FROM {table} WHERE uid=?', (uid,)).fetchone()
                if not row:
                    raise ValueError('找不到任务。')
                if row['state']=='cancelled':
                    return '任务已关闭，后续流程不会继续。'
                if row['state'] in ('delivered','completed','superseded') or row['state'] != expected_state:
                    raise ValueError('任务状态已变化，请刷新后查看。')
                if db.execute(f"SELECT 1 FROM {notices} WHERE run_uid=? AND state='sending'", (uid,)).fetchone():
                    raise ValueError('任务通知正在发送，请稍后再关闭。')
                db.execute(f"UPDATE {notices} SET state='cancelled',detail='任务已关闭，未发送。' WHERE run_uid=? AND state='pending'", (uid,))
                if kind=='two':
                    db.execute("UPDATE two_stage_events SET state='cancelled' WHERE run_uid=? AND state='pending'",(uid,))
                if kind=='work':
                    db.execute("UPDATE workflow_deliveries SET state='cancelled',detail='任务已关闭，未投递。' WHERE run_uid=? AND state='pending'",(uid,))
                    db.execute("UPDATE workflow_runs SET state='cancelled' WHERE uid=?",(uid,))
                else:
                    from zhishi.core.clock import stamp
                    db.execute(f"UPDATE {table} SET state='cancelled',updated_at=? WHERE uid=?",(stamp(),uid))
        return '任务已关闭，后续流程不会继续。已发出的消息无法撤回。'

    def reminder_action(self, data):
        """Structured buttons never depend on a model understanding a UI command."""
        request_id, action = data.get('request_id'), data.get('action')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('缺少有效的操作请求编号。')
        if action not in ('create', 'done', 'cancel', 'snooze'):
            raise ValueError('不支持这个待办操作。')
        fingerprint = json.dumps({k: v for k, v in data.items() if k != 'request_id'}, ensure_ascii=False, sort_keys=True)
        with self.pilot.request_lock, self.pilot.lock, self.pilot.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT response FROM requests WHERE id=?', (request_id,)).fetchone()
            if saved:
                result = json.loads(saved['response'])
                if result.get('_action') != fingerprint:
                    raise ValueError('操作请求编号已用于其他内容，请重新操作。')
                return {'reply': result['reply']}
            if action == 'create':
                title = data.get('title')
                if not isinstance(title, str):
                    raise ValueError('请填写任务内容。')
                reply = self.pilot.create(db, title, data.get('due'))
            else:
                ident = data.get('id')
                if type(ident) is not int:
                    raise ValueError('请选择有效待办。')
                minutes = data.get('minutes', 10)
                if type(minutes) is not int:
                    raise ValueError('请输入整数分钟。')
                reply = self.pilot.change(db, ident, action, minutes)
            result = {'reply': reply, '_action': fingerprint}
            db.execute('INSERT INTO requests VALUES (?,?)', (request_id, json.dumps(result, ensure_ascii=False)))
        return {'reply': reply}

    def legacy_action(self, data):
        item = next((t for t in self._legacy() if t['id'] == data.get('id')), None)
        if not item or data.get('action') not in item['actions'] or data.get('state') != item['state']:
            raise ValueError('任务阶段已经变化，请刷新后查看可用操作。')
        code, version, kind = item['code'], item['version'], item['kind']
        if kind == 'work':
            command = f'{"确认发送" if data["action"] == "approve" else "要求修改"} {code} V{version}'
        elif kind == 'two':
            command = f'确认{"第一段" if item["state"] == "a_review" else "第二段"} {code} V{version}'
        else:
            command = f'完成{"A" if item["state"] == "a_waiting" else "B"} {code}'
        return self.pilot.chat(command, data.get('request_id'))

    def chat(self, data):
        target = data.get('target', 'assistant')
        if target == 'assistant':
            return self.pilot.chat(data.get('text'), data.get('request_id'))
        if not isinstance(target, str) or not target.startswith('ai:') or not self.pilot.ai_dispatch:
            raise ValueError('请选择有效的对话任务。')
        with self.pilot.ai_dispatch.lock:
            run = self.pilot.ai_dispatch.row(target[3:])
            if data.get('revision') != run['revision'] or run['state'] not in WAITING:
                raise ValueError('该流程阶段已经变化，请查看最新进度后再回复。')
            role = expected_role(run['state'])
            # submit enforces the original local/WeChat binding and notice delivery gate.
            reply = self.pilot.ai_dispatch.submit(run['code'], role, data.get('text'), data.get('request_id'))
        return {'reply': reply}
