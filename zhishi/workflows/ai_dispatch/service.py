"""Durable state machine around AI intent, registered skills and role-specific notices."""
import json
import re
import secrets
import threading
from pathlib import Path

from zhishi.core.clock import now, stamp, deadline
from zhishi.models.workflow import WorkflowModel
from zhishi.models.intents import IntentRouter
from .plans import ROLES, load, due_slot
from .skills import ProductSkills
from .project import read_brief
from .intents import explicit_intent
from .messages import header, stage_message, acknowledgement, task_question
from .selections import channel_key, choice_index, choice_prompt

LABELS = {
    'manager_wait': '等待负责人确认', 'employee1_wait': '等待员工1接单或请求协助',
    'employee1_work': '正在协助员工1', 'employee1_review': '等待员工1核对成果',
    'employee2_wait': '等待员工2接单或请求协助', 'employee2_work': '正在协助员工2',
    'employee2_review': '等待员工2核对成果', 'final_check': '最终校验中',
    'report_pending': '校验通过，等待汇报提交', 'complete': '校验完成，汇报已提交', 'blocked': '受阻，等待处理', 'cancelled': '已关闭',
}
WAITING = {s for s in LABELS if s.endswith(('_wait', '_review'))}
ROLE_NAMES = {'manager': '负责人', 'employee1': '员工1', 'employee2': '员工2'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def unpack(row):
    value = dict(row)
    for key in ('task', 'routes', 'evidence'):
        value[key] = json.loads(value.pop(key + '_json'))
    return value


def expected_role(state):
    return 'manager' if state == 'manager_wait' else state.split('_')[0]


def run_label(run):
    if run['state']=='employee1_wait' and run['evidence'].get('employee1_acknowledged'):
        return '员工1已接单，等待文件准备'
    return LABELS[run['state']]


class AIDispatch:
    def __init__(self, pilot, root, *, model=None, skills=None):
        self.pilot, self.store = pilot, pilot.store
        self.plan_path = Path(root) / 'projects' / 'ai-dispatch' / 'tasks.json'
        self.model = model or WorkflowModel(Path(root) / 'data' / 'workflow-model.json')
        self.skills = skills or ProductSkills(pilot.product_demo)
        self.lock = threading.RLock()
        self.worker_lock, self.send_lock = threading.Lock(), threading.Lock()
        self.config_error = ''
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ai_dispatch_runs (
                    code TEXT PRIMARY KEY, task_id TEXT NOT NULL, slot TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    task_json TEXT NOT NULL, routes_json TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL,
                    UNIQUE(task_id, slot));
                CREATE TABLE IF NOT EXISTS ai_dispatch_inbox (
                    request_id TEXT PRIMARY KEY, code TEXT NOT NULL, role TEXT NOT NULL,
                    revision INTEGER NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    result TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_dispatch_notices (
                    id INTEGER PRIMARY KEY, code TEXT NOT NULL, revision INTEGER NOT NULL,
                    role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(code, revision, kind));
                CREATE TABLE IF NOT EXISTS ai_dispatch_events (
                    id INTEGER PRIMARY KEY, code TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_dispatch_selections (
                    channel_key TEXT PRIMARY KEY, options_json TEXT NOT NULL, pending_text TEXT NOT NULL,
                    request_id TEXT NOT NULL, created TEXT NOT NULL);
            ''')
            # One sequence shared by both streams also orders events in the same second.
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ai_conversation_order (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                  source_id TEXT NOT NULL, at TEXT NOT NULL, estimated INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(source,source_id));
                CREATE TRIGGER IF NOT EXISTS ai_notice_order AFTER INSERT ON ai_dispatch_notices BEGIN
                  INSERT INTO ai_conversation_order(source,source_id,at) VALUES ('notice',NEW.id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END;
                CREATE TRIGGER IF NOT EXISTS ai_reply_order AFTER INSERT ON ai_dispatch_inbox BEGIN
                  INSERT INTO ai_conversation_order(source,source_id,at) VALUES ('reply',NEW.request_id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END;
            ''')
            # Old notices did not store time. Reconstruct stage order and explicitly mark it.
            for run in db.execute('SELECT code,created FROM ai_dispatch_runs ORDER BY created'):
                history = []
                for n in db.execute('SELECT * FROM ai_dispatch_notices WHERE code=? ORDER BY id', (run['code'],)):
                    history.append((n['revision'], 0 if n['kind'] in LABELS else 2, str(n['id']), 'notice', run['created']))
                for r in db.execute('SELECT rowid,* FROM ai_dispatch_inbox WHERE code=? ORDER BY rowid', (run['code'],)):
                    history.append((r['revision'], 1, r['request_id'], 'reply', r['created']))
                for _, _, ident, source, at in sorted(history, key=lambda x: (x[0], x[1])):
                    db.execute('INSERT OR IGNORE INTO ai_conversation_order(source,source_id,at,estimated) VALUES (?,?,?,1)', (source, ident, at))
            db.execute("UPDATE ai_dispatch_notices SET state='unknown',detail='重启前发送结果未知，不自动重发。' WHERE state='sending'")
            db.execute("UPDATE ai_dispatch_inbox SET state='failed',result='AI 理解期间重启，请重新回复。' WHERE state='processing'")
            interrupted = db.execute("SELECT * FROM ai_dispatch_runs WHERE state IN ('employee1_work','employee2_work','final_check')").fetchall()
            for row in interrupted:
                self._block(db, unpack(row), '技能执行期间重启，需核查执行记录后重新发起。')

    def _event(self, db, code, kind, detail):
        db.execute('INSERT INTO ai_dispatch_events(code,kind,detail,created) VALUES (?,?,?,?)',
                   (code, kind, detail[:1500], stamp()))

    def _notice(self, db, run, role, kind, text):
        intro = header(role)
        name = run.get('task', {}).get('display_name')
        if name:
            intro = f'【{name}｜{ROLE_NAMES[role]}】\n'
            examples = {'manager_wait':'确认', 'employee1_wait':'收到', 'employee1_review':'核对过了',
                        'employee2_wait':'收到', 'employee2_review':'核对过了'}
            if run['state'] in examples:
                text += f'\n\n回复时请带上任务名，例如：{name} {examples[run["state"]]}。'
        db.execute('INSERT OR IGNORE INTO ai_dispatch_notices(code,revision,role,kind,text) VALUES (?,?,?,?,?)',
                   (run['code'], run['revision'], role, kind, intro + text))

    def _move(self, db, run, state, text=None, role=None):
        db.execute("UPDATE ai_dispatch_notices SET state='cancelled',detail='阶段已推进，旧待发说明已取消。' WHERE code=? AND revision<=? AND state='pending'",
                   (run['code'],run['revision']))
        run = dict(run, state=state, revision=run['revision'] + 1)
        db.execute('UPDATE ai_dispatch_runs SET state=?,revision=?,evidence_json=?,updated=? WHERE code=?',
                   (state, run['revision'], encoded(run['evidence']), stamp(), run['code']))
        self._event(db, run['code'], state, text or LABELS[state])
        if text:
            self._notice(db, run, role or expected_role(state), state, text)
        return run

    def _block(self, db, run, reason, *, reporting_role=None):
        db.execute('UPDATE ai_dispatch_notices SET state=\'cancelled\' WHERE code=? AND state=\'pending\'', (run['code'],))
        guidance = ('\n如需更正文件，请在工作台打开本任务，选择“上传文件更正任务”，'
                    '选择需要替换的输入文件并上传修正版。旧文件保留，重新核对后继续。')
        text = '任务受阻：' + reason + '。尚未汇报工作完成。'
        run = self._move(db, run, 'blocked', text + (guidance if reporting_role else ''), 'manager')
        if reporting_role in ('employee1', 'employee2'):
            self._notice(db, run, reporting_role, 'correction_guidance',
                         '已记录问题，任务暂不继续。如需更正文件，请把修正版交给工作台使用者，'
                         '由其在本任务中上传；旧文件会保留，重新核对后再继续。')
        db.execute('UPDATE ai_dispatch_runs SET error=? WHERE code=?', (reason[:500], run['code']))

    def row(self, code):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ai_dispatch_runs WHERE code=?', (code,)).fetchone()
        if not row:
            raise ValueError('找不到 AI 流程编号。')
        return unpack(row)

    def _channel(self, name):
        if name in ('primary', 'employee2', 'local'):
            return {'primary': self.pilot.transport, 'employee2': self.pilot.employee2_transport}.get(name)
        return self.pilot.accounts.transport(self.pilot.accounts.row(name))

    def _routes(self, task):
        routes = {}
        for role, name in task['roles'].items():
            if name == 'local':
                routes[role] = None
                continue
            channel = self._channel(name)
            if not channel or not channel.enabled or not channel.route():
                raise ValueError(f'{ROLE_NAMES[role]}的微信通道尚未配置。')
            routes[role] = channel.route()
        return routes

    def start(self, ident, slot, display_name=None, *, roles=None, staff_names=None):
        if not isinstance(slot, str) or not 8 <= len(slot) <= 100:
            raise ValueError('缺少有效的启动请求编号。')
        task = next((t for t in load(self.plan_path) if t['id'] == ident), None)
        if not task or not task['enabled']:
            raise ValueError('这项 AI 任务尚未启用；API、时间和技能待配置。')
        if not self.model.settings()['ready']:
            raise ValueError('工作流 AI API 尚未配置，未联系任何角色。')
        if roles is not None:
            if set(roles) != set(ROLE_NAMES):
                raise ValueError('请指定完整的员工分工。')
            task = dict(task, roles=roles, staff_names=staff_names or {})
        routes = self._routes(task)
        brief = read_brief(self.plan_path, task)
        if brief:
            task = dict(task, project_brief=brief)
        if display_name and not re.fullmatch(r'商品核查[1-9]\d*', display_name):
            raise ValueError('商品核查编号格式不正确。')
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT code FROM ai_dispatch_runs WHERE task_id=? AND slot=?', (ident, slot)).fetchone()
            if saved:
                return saved['code']
            active = db.execute("SELECT code FROM ai_dispatch_runs WHERE task_id=? AND state NOT IN ('complete','blocked','cancelled')", (ident,)).fetchone()
            if active and not display_name:
                raise ValueError(f'已有未结束流程 {active["code"]}，请先处理当前流程。')
            if display_name:
                for item in db.execute('SELECT task_json FROM ai_dispatch_runs'):
                    if json.loads(item['task_json']).get('display_name') == display_name:
                        raise ValueError('商品核查编号已使用，不能重复使用。')
                task = dict(task, display_name=display_name)
            code = 'AIW-' + now().strftime('%Y%m%d') + '-' + secrets.token_hex(4).upper()
            run = {'code': code, 'state': 'manager_wait', 'revision': 0, 'evidence': {}, 'task': task}
            db.execute('INSERT INTO ai_dispatch_runs(code,task_id,slot,state,task_json,routes_json,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                       (code, ident, slot, run['state'], encoded(task), encoded(routes), stamp(), stamp()))
            self._notice(db, run, 'manager', 'manager_wait', stage_message('manager_wait', task, {}))
            self._event(db, code, 'created', '已读取任务清单并创建流程，等待负责人确认。')
        return code

    def cancel(self, code, expected_state, expected_revision):
        if not self.worker_lock.acquire(blocking=False):
            raise ValueError('任务正在处理回复或执行，请稍后再关闭。')
        try:
            if not self.send_lock.acquire(blocking=False):
                raise ValueError('正在提交微信通知，请稍后再关闭。')
            try:
                with self.lock, self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute('SELECT * FROM ai_dispatch_runs WHERE code=?', (code,)).fetchone()
                    if not row:
                        raise ValueError('找不到任务。')
                    if row['state'] == 'cancelled':
                        return '任务已关闭，后续流程不会继续。'
                    if row['state'] in ('complete',) or row['state'] != expected_state or row['revision'] != expected_revision:
                        raise ValueError('任务状态已变化，请刷新后查看。')
                    if db.execute("SELECT 1 FROM ai_dispatch_notices WHERE code=? AND state='sending'", (code,)).fetchone():
                        raise ValueError('任务通知正在发送，请稍后再关闭。')
                    db.execute("UPDATE ai_dispatch_notices SET state='cancelled',detail='任务已关闭，未发送。' WHERE code=? AND state='pending'", (code,))
                    db.execute("UPDATE ai_dispatch_inbox SET state='cancelled',result='任务已关闭，回复未处理。' WHERE code=? AND state='pending'", (code,))
                    db.execute("UPDATE ai_dispatch_runs SET state='cancelled',revision=revision+1,updated=? WHERE code=?", (stamp(),code))
                    self._event(db, code, 'cancelled', '用户在工作台关闭任务，待发送通知和待处理回复已取消。')
                return '任务已关闭，后续流程不会继续。已发出的消息无法撤回。'
            finally:
                self.send_lock.release()
        finally:
            self.worker_lock.release()

    def schedule_tick(self, current=None):
        try:
            tasks = load(self.plan_path)
            self.config_error = ''
            for task in tasks:
                slot = due_slot(task, current or now()) if task['enabled'] else None
                if slot:
                    try:
                        self.start(task['id'], slot)
                    except ValueError as exc:
                        self.config_error = str(exc)
        except ValueError as exc:
            self.config_error = str(exc)

    def connect_wechat(self, code):
        """Move an untouched local pilot to the existing two-account role mapping."""
        roles = {'manager': 'primary', 'employee1': 'primary', 'employee2': 'employee2'}
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.row(code)
            # Repeating this operation must never requeue an in-flight/unknown send.
            if run['task']['roles'] == roles:
                return '本流程已经使用绑定微信，不会重复发送负责人确认消息。'
            if run['state'] != 'manager_wait' or set(run['task']['roles'].values()) != {'local'}:
                raise ValueError('只有尚在等待负责人确认的本地流程可以切换微信。')
            if db.execute("SELECT 1 FROM ai_dispatch_inbox WHERE code=? AND state IN ('pending','processing','done') LIMIT 1", (code,)).fetchone():
                raise ValueError('本流程已有待处理或已处理的回复，请先核对阶段，未切换通道。')
            if db.execute("SELECT 1 FROM ai_dispatch_notices WHERE code=? AND state='sending' LIMIT 1", (code,)).fetchone():
                raise ValueError('当前仍在生成通知，请稍后再切换。')
            task = dict(run['task'], roles=roles, allow_shared_routes=True)
            routes = self._routes(task)
            for name in ('primary', 'employee2'):
                channel = self._channel(name)
                if hasattr(channel, 'gateway_listening') and not channel.gateway_listening():
                    raise ValueError('微信接收网关未运行，暂未切换通道。')
            db.execute("UPDATE ai_dispatch_notices SET state='cancelled',detail='切换绑定微信，原待处理本地通知已取消。' WHERE code=? AND state='pending'", (code,))
            db.execute('UPDATE ai_dispatch_runs SET task_json=?,routes_json=? WHERE code=?', (encoded(task), encoded(routes), code))
            run['task'], run['routes'] = task, routes
            self._event(db, code, 'channels_changed', '从本地模拟切换为现有绑定：负责人和员工1使用主微信，员工2使用第二微信。')
            self._move(db, run, 'manager_wait', stage_message('manager_wait', task, run['evidence']))
        return '已切换现有绑定微信，负责人确认消息已排队；实际发送状态以通知记录为准。'

    def submit(self, code, role, text, request_id, *, channel='local'):
        if role not in ROLES or not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError('请提供有效角色和 1–2000 字的回复。')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('缺少有效的回复请求编号。')
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT code,role FROM ai_dispatch_inbox WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['code'] != code or previous['role'] != role:
                    raise ValueError('回复编号已被其他流程使用。')
                return '这条回复已接收，不会重复推进。'
            run = self.row(code)
            if run['state'] not in WAITING or expected_role(run['state']) != role:
                raise ValueError('当前阶段不在等待该角色回复。')
            if run['task']['roles'][role] != channel:
                raise ValueError('回复必须来自该角色绑定的通道。')
            if channel != 'local':
                transport = self._channel(channel)
                if not transport or not transport.enabled or transport.route() != run['routes'][role]:
                    raise ValueError('微信绑定已变化或停用，未接受旧流程回复。')
            if db.execute("SELECT 1 FROM ai_dispatch_inbox WHERE code=? AND role=? AND revision=? AND text=? AND state IN ('pending','processing') LIMIT 1",
                          (code,role,run['revision'],text.strip())).fetchone():
                return '收到，这条回复已经在处理，暂时不用重复发，我处理好后会告诉你。'
            notice = db.execute('SELECT state FROM ai_dispatch_notices WHERE code=? AND revision=? AND kind=?',
                                (code, run['revision'], run['state'])).fetchone()
            if not notice or notice['state'] not in ('demo_delivered', 'submitted_unverified'):
                raise ValueError('本阶段通知尚未提交或发送结果未知，请先核对发送状态。')
            db.execute('INSERT INTO ai_dispatch_inbox(request_id,code,role,revision,text,created) VALUES (?,?,?,?,?,?)',
                       (request_id, code, role, run['revision'], text.strip(), stamp()))
            if channel != 'local':
                db.execute('DELETE FROM ai_dispatch_selections WHERE channel_key=?',(channel_key(channel,run['routes'][role]),))
        return '收到，我按当前这件工作处理你的回复。'

    def command(self, text, wechat_role, request_id):
        named = re.match(r'^(商品核查[1-9]\d*)[\s：:,，]*(.*)$', text, re.S)
        if named:
            with self.store.connect() as db:
                rows = [unpack(row) for row in db.execute('SELECT * FROM ai_dispatch_runs')
                        if json.loads(row['task_json']).get('display_name') == named.group(1)]
            if len(rows) != 1:
                return '没有找到对应的商品核查任务。'
            run = rows[0]
            if run['state'] not in WAITING:
                return f'{named.group(1)}：{LABELS[run["state"]]}，当前不需要确认。'
            if not named.group(2).strip():
                return f'{named.group(1)}：{LABELS[run["state"]]}。请补充你的回复。'
            if wechat_role is None:
                raise ValueError('请使用对应角色的微信回复。')
            return self.submit(run['code'], expected_role(run['state']), named.group(2).strip(), request_id, channel=wechat_role)
        # The fixed-width code can touch Chinese text; spacing is not part of identity.
        match = re.match(r'^(AIW-\d{8}-[A-F0-9]{8})(?![A-F0-9])[\s：:,，]*(.*)$', text, re.S | re.I)
        if match:
            run = self.row(match[1].upper())
            if wechat_role is None:
                raise ValueError('请在 AI 工作流页面选择模拟角色回复，或使用对应微信会话。')
            if not match[2].strip():
                return f'商品排查：{LABELS[run["state"]]}。请直接说明你的回复。'
            if run['state'] not in WAITING:
                return f'商品排查：{LABELS[run["state"]]}，当前不需要重复确认。'
            role = expected_role(run['state'])
            if task_question(match[2]):
                if run['task']['roles'][role] != wechat_role:
                    return f'商品排查：{LABELS[run["state"]]}，请等待对应同事处理。'
                return stage_message(run['state'], run['task'], run['evidence'])
            return self.submit(run['code'], role, match[2].strip(), request_id, channel=wechat_role)
        if text.upper().startswith('AIW-'):
            return '这段编号没有识别出来。可以直接回复正文，我会按当前工作处理；同时有几件工作时，会请你选一下。'
        # Existing reminder commands retain their original meaning during a workflow.
        if wechat_role is None or IntentRouter.explicit(text) is not None:
            return None
        channel = self._channel(wechat_role)
        if not channel or not channel.enabled:
            return None
        route = channel.route()
        if not route:
            return None
        selection = self._resolve_selection(text, wechat_role, route)
        if selection is not None:
            return selection
        if choice_index(text,9) is not None:
            return '当前没有待选择的任务列表，我没有把这个序号当成新的确认。请直接说一下你要处理的工作。'
        candidates = []
        related = []
        finished = []
        with self.store.connect() as db:
            for row in db.execute('SELECT * FROM ai_dispatch_runs ORDER BY created DESC'):
                if row['state'] in ('complete','blocked','cancelled'):
                    if explicit_intent('employee1_review', text):
                        previous = unpack(row)
                        if any(name == wechat_role and previous['routes'][role] == route for role,name in previous['task']['roles'].items()):
                            finished.append(previous)
                    continue
                run = unpack(row)
                if any(name == wechat_role and run['routes'][role] == route for role,name in run['task']['roles'].items()):
                    related.append(run)
                if run['state'] not in WAITING:
                    continue
                role = expected_role(run['state'])
                if run['task']['roles'][role] == wechat_role and run['routes'][role] == route:
                    candidates.append((run,role))
            # Other workflow families can share the pilot's main chat. An unnumbered
            # reply must not silently select AI dispatch while another awaits review.
            others = []
            if wechat_role == 'primary':
                others = [('workflow_runs',('awaiting_review',)),
                          ('two_stage_runs',('a_review','b_review')),
                          ('abc_runs',('a_waiting','b_waiting')),
                          ('invoice_demo_runs',('manager_wait','employee1_wait')),
                          ('product_demo_runs',('manager_wait','employee1_wait'))]
            elif wechat_role == 'employee2':
                others = [('product_demo_runs',('employee2_wait',)), ('invoice_demo_runs',('employee2_wait','employee2_received'))]
            ambiguous = False
            for table,states in others:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    continue
                placeholders = ','.join('?' for _ in states)
                if db.execute('SELECT 1 FROM '+table+' WHERE state IN ('+placeholders+') LIMIT 1',states).fetchone():
                    ambiguous = True
                    break
        if len(candidates) > 1 or (candidates and ambiguous):
            options = [{'code':r['code'],'role':role,'revision':r['revision'],
                        'label':r['task'].get('display_name',r['task']['title'][:80])+'（'+r['created'][5:19].replace('T',' ')+'，'+run_label(r)+'）'}
                       for r,role in candidates[:9]]
            with self.lock,self.store.connect() as db:
                db.execute('INSERT OR REPLACE INTO ai_dispatch_selections VALUES (?,?,?,?,?)',
                           (channel_key(wechat_role,route),encoded(options),text,request_id,stamp()))
            return choice_prompt(options,ambiguous)
        if len(candidates) == 1:
            run,role = candidates[0]
            if task_question(text):
                return stage_message(run['state'], run['task'], run['evidence'])
            with self.store.connect() as db:
                db.execute('DELETE FROM ai_dispatch_selections WHERE channel_key=?',(channel_key(wechat_role,route),))
            return self.submit(run['code'], role, text, request_id, channel=wechat_role)
        if len(related) == 1:
            run = related[0]
            return f'商品排查：{LABELS[run["state"]]}。当前会话无需重复确认，项目尚未全部完成。'
        if not ambiguous and finished:
            run = finished[0]
            return f'商品排查：{LABELS[run["state"]]}。当前没有待你确认的阶段，无需重复回复。'
        return None

    def _resolve_selection(self, text, channel, route):
        key = channel_key(channel,route)
        with self.lock:
            with self.store.connect() as db:
                row = db.execute('SELECT * FROM ai_dispatch_selections WHERE channel_key=?',(key,)).fetchone()
            if not row:
                return None
            options = json.loads(row['options_json'])
            index = choice_index(text,len(options))
            cancel = text.strip().rstrip('。！!') in {'不是','都不是','不是这件','不是这些','取消选择','先不选'}
            expired = (now()-deadline(row['created'])).total_seconds()>600
            if cancel or expired:
                with self.store.connect() as db:
                    db.execute('DELETE FROM ai_dispatch_selections WHERE channel_key=?',(key,))
                if cancel:
                    return '好的，已取消这次选择，任务状态没有改变。请说一下你指的是哪件工作。'
                if index is not None:
                    return '刚才的选择已过期，我先不使用旧回复。请重新说明你要处理的工作，我会列出当前选项。'
                return None
            if index is None:
                return None
            if index >= len(options):
                return '这里没有这一项，请按刚才列出的选项说第几个，或者说“不是这些”。'
            option = options[index]
            run = self.row(option['code'])
            if run['revision']!=option['revision'] or run['state'] not in WAITING or expected_role(run['state'])!=option['role']:
                with self.store.connect() as db:
                    db.execute('DELETE FROM ai_dispatch_selections WHERE channel_key=?',(key,))
                return '这件工作的进度已经变化，我没有把旧回复用于新阶段。请看最新通知，再告诉我你的意思。'
            reply = self.submit(run['code'],option['role'],row['pending_text'],row['request_id'],channel=channel)
            with self.store.connect() as db:
                db.execute('DELETE FROM ai_dispatch_selections WHERE channel_key=?',(key,))
                self._event(db,run['code'],'task_selected','用户选择第'+str(index+1)+'项，已将原回复归入对应任务。')
            return reply

    def retry_reply(self, code, request_id):
        """Retry only the latest failed, authenticated reply in its original stage."""
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.row(code)
            item = db.execute('SELECT * FROM ai_dispatch_inbox WHERE code=? AND request_id=?', (code,request_id)).fetchone()
            if not item:
                raise ValueError('找不到这条流程回复。')
            if item['state'] in ('pending','processing','done'):
                return '这条回复已在处理或已处理，不会重复推进。'
            if run['state'] not in WAITING or run['revision'] != item['revision'] or expected_role(run['state']) != item['role']:
                raise ValueError('原回复所属阶段已变化，不能重试旧回复。')
            latest = db.execute('SELECT request_id FROM ai_dispatch_inbox WHERE code=? AND role=? ORDER BY rowid DESC LIMIT 1', (code,item['role'])).fetchone()
            if latest['request_id'] != request_id:
                raise ValueError('已有更新的回复，不能重试更早的用户意图。')
            channel = run['task']['roles'][item['role']]
            if channel != 'local':
                transport = self._channel(channel)
                if not transport or not transport.enabled or transport.route() != run['routes'][item['role']]:
                    raise ValueError('微信绑定已变化，不能重试旧会话的回复。')
            db.execute("UPDATE ai_dispatch_inbox SET state='pending',result='' WHERE request_id=?", (request_id,))
            self._event(db, code, 'reply_retry', '重新理解当前阶段最新的失败回复；沿用原身份和回复编号，不新建流程。')
        return '原回复已重新排队，按当前阶段识别并通过相应校验后才会推进。'

    def _interpret_one(self, item):
        run = self.row(item['code'])
        if run['revision'] != item['revision'] or run['state'] not in WAITING:
            raise ValueError('流程阶段已变化，这条旧回复未推进流程，请查看当前通知。')
        # Existing runs created before project briefs were added get one explicit
        # snapshot before their next interpretation. Later file edits do not alter it.
        if not run['task'].get('project_brief'):
            brief = read_brief(self.plan_path, run['task'])
            if brief:
                run['task'] = dict(run['task'], project_brief=brief)
                with self.lock, self.store.connect() as db:
                    changed = db.execute('UPDATE ai_dispatch_runs SET task_json=? WHERE code=? AND revision=?',
                                         (encoded(run['task']), run['code'], run['revision'])).rowcount
                    if changed != 1:
                        raise ValueError('加载项目说明期间流程已变化，请查看当前通知。')
                    self._event(db, run['code'], 'project_brief', f'已载入项目说明 {brief["name"]}，SHA256：{brief["sha256"]}')
        with self.store.connect() as db:
            notice = db.execute('SELECT text FROM ai_dispatch_notices WHERE code=? AND revision=? AND kind=?',
                                (run['code'], run['revision'], run['state'])).fetchone()
            history = db.execute('SELECT text,state FROM ai_dispatch_inbox WHERE code=? AND role=? AND rowid < '
                                 '(SELECT rowid FROM ai_dispatch_inbox WHERE request_id=?) ORDER BY rowid DESC LIMIT 4',
                                 (run['code'], item['role'], item['request_id'])).fetchall()
        context = {'task': run['task']['title'], 'stage': run_label(run), 'role': ROLE_NAMES[item['role']],
                   'workflow_code': run['code'], 'project_brief': run['task'].get('project_brief'),
                   'stage_notification': notice['text'] if notice else '',
                   'recent_role_replies': [dict(r) for r in reversed(history)],
                   'confirmed_stages': [r for r in ('employee1','employee2') if run['evidence'].get(r+'_confirmed')],
                   'role_acknowledged': bool(run['evidence'].get(item['role']+'_acknowledged')),
                   'handoff_summary': run['evidence'].get('employee1', {}).get('summary', ''),
                   'skill_result': run['evidence'].get(item['role'], {}).get('summary', '本阶段技能尚未执行')}
        decision = explicit_intent(run['state'], item['text'])
        source = 'rule' if decision else 'model'
        if decision is None:
            decision = WorkflowModel.validate(self.model.interpret(context, item['text']))
        with self.lock, self.store.connect() as db:
            latest = self.row(run['code'])
            if latest['revision'] != run['revision']:
                raise ValueError('AI 理解期间流程已变化，本次判断未生效。')
            channel = run['task']['roles'][item['role']]
            if channel != 'local' and self._channel(channel).route() != run['routes'][item['role']]:
                raise ValueError('AI 理解期间微信绑定已变化，本次判断未生效。')
            self._event(db, run['code'], 'rule_decision' if source == 'rule' else 'ai_decision', encoded(decision))
            action = decision['action'] if decision['confidence'] >= 0.85 else 'unclear'
            if action == 'acknowledge' or (run['state']=='employee1_wait' and action=='accept'):
                run['evidence'][item['role']+'_acknowledged'] = True
                db.execute('UPDATE ai_dispatch_runs SET evidence_json=?,updated=? WHERE code=?',
                           (encoded(run['evidence']),stamp(),run['code']))
                self._event(db, run['code'], 'acknowledged', ROLE_NAMES[item['role']]+'已收到本阶段安排，等待准备或核对。')
                self._notice(db, run, item['role'], 'acknowledge-'+item['request_id'], acknowledgement(run['state']))
            elif action == 'blocked':
                self._block(db, run, ROLE_NAMES[item['role']] + '反馈需要停止或处理问题：' + item['text'][:200],
                            reporting_role=item['role'])
            elif run['state'] == 'manager_wait' and action == 'accept':
                self._move(db, run, 'employee1_wait', stage_message('employee1_wait',run['task'],run['evidence']))
            elif run['state'].endswith('_wait') and item['role'] != 'manager' and action in ('accept', 'help', 'complete'):
                self._move(db, run, item['role'] + '_work')
            elif run['state'].endswith('_review') and action == 'complete':
                self.skills.validate(run, item['role'])
                run['evidence'][item['role'] + '_confirmed'] = True
                if item['role'] == 'employee1':
                    self._move(db, run, 'employee2_wait', stage_message('employee2_wait',run['task'],run['evidence']))
                else:
                    self._move(db, run, 'final_check')
            else:
                if item['role'] == 'manager':
                    clarification = '当前等待你确认是否启动商品排查。确认启动可回复“同意，开始吧”；有疑问可说明具体问题。'
                elif run['state'].endswith('_wait'):
                    clarification = ('还没有进入检查。我需要确认一下：文件已经准备好了，还是你需要先准备？导好后说“好了”即可。'
                                     if item['role']=='employee1' else '当前等你接手商品比对。直接说“收到”或“开始吧”，我就运行比对并把结果给你核对。')
                else:
                    clarification = '我还不确定你是否核对通过。看过结果后说“没问题”或“可以继续”就行；发现问题直接告诉我哪一项，我先不交接。'
                self._notice(db, run, item['role'], 'clarify-' + item['request_id'], clarification)
            db.execute("UPDATE ai_dispatch_inbox SET state='done',result=? WHERE request_id=?", (encoded(decision), item['request_id']))

    def work_tick(self):
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                item = db.execute("SELECT * FROM ai_dispatch_inbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
                if item:
                    db.execute("UPDATE ai_dispatch_inbox SET state='processing' WHERE request_id=?", (item['request_id'],))
            if item:
                try:
                    self._interpret_one(dict(item))
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else 'AI 判断暂时失败，请重新回复。'
                    with self.store.connect() as db:
                        db.execute("UPDATE ai_dispatch_inbox SET state='failed',result=? WHERE request_id=?", (reason[:500], item['request_id']))
                        self._event(db, item['code'], 'reply_failed', reason)
                        run = self.row(item['code'])
                        if run['state'] in WAITING:
                            self._notice(db, run, item['role'], 'error-' + item['request_id'], reason[:300])
                return
            with self.lock, self.store.connect() as db:
                row = db.execute("SELECT * FROM ai_dispatch_runs WHERE state IN ('employee1_work','employee2_work','final_check') ORDER BY created LIMIT 1").fetchone()
            if not row:
                return
            run = unpack(row)
            try:
                if run['state'] == 'final_check':
                    if not all(run['evidence'].get(r + '_confirmed') for r in ('employee1', 'employee2')):
                        raise ValueError('缺少两名员工对本版成果的确认。')
                    result = self.skills.verify(run)
                    if result.get('passed') is not True:
                        raise ValueError('最终校验未通过。')
                    run['evidence']['final'] = result
                    with self.lock, self.store.connect() as db:
                        self._move(db, run, 'report_pending', stage_message('report_pending',run['task'],run['evidence']), 'manager')
                else:
                    role = expected_role(run['state'])
                    run['evidence'][role] = self.skills.execute(role, run)
                    with self.lock, self.store.connect() as db:
                        self._move(db, run, role + '_review', stage_message(role+'_review',run['task'],run['evidence']))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else '技能执行或校验失败，请检查输入与执行记录。'
                with self.lock, self.store.connect() as db:
                    self._block(db, run, reason)
        finally:
            self.worker_lock.release()

    def notify_tick(self):
        if not self.send_lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                item = db.execute("SELECT * FROM ai_dispatch_notices WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
                if not item:
                    return
                db.execute("UPDATE ai_dispatch_notices SET state='sending' WHERE id=?", (item['id'],))
            run = self.row(item['code'])
            try:
                # Recheck the approved inputs/results just before review and final reports.
                if item['kind'] == 'report_pending':
                    if self.skills.verify(run).get('passed') is not True:
                        raise ValueError('发送前最终校验未通过。')
                elif item['kind'].endswith('_review'):
                    self.skills.validate(run, item['role'])
                name = run['task']['roles'][item['role']]
                if name == 'local':
                    state, detail = 'demo_delivered', '消息保留在本地工作台，未联系真实人员。'
                else:
                    media = self.skills.attachments(run) if item['kind'] in ('employee2_review', 'report_pending') else []
                    state, detail = self._channel(name).send_package(item['text'],
                                                                  media, expected_route=run['routes'][item['role']])
            except (ValueError, FileNotFoundError) as exc:
                state, detail = 'blocked', str(exc)[:300]
                with self.lock, self.store.connect() as db:
                    self._block(db, run, detail)
            except Exception:
                state, detail = 'unknown', '发送结果未知，不自动重发。'
            with self.lock, self.store.connect() as db:
                db.execute('UPDATE ai_dispatch_notices SET state=?,detail=? WHERE id=?', (state, detail, item['id']))
                if item['kind'] == 'report_pending' and state in ('demo_delivered', 'submitted_unverified'):
                    self._move(db, run, 'complete')
        finally:
            self.send_lock.release()

    def state(self, *, limit=30):
        try:
            tasks = load(self.plan_path)
            config_error = self.config_error
        except ValueError as exc:
            tasks, config_error = [], str(exc)
        with self.store.connect() as db:
            runs = []
            query = 'SELECT * FROM ai_dispatch_runs ORDER BY rowid DESC'
            rows = db.execute(query if limit is None else query + ' LIMIT ?', () if limit is None else (limit,))
            for row in rows:
                run = unpack(row)
                # Bindings stay server-side. The workbench only sees role names.
                run.pop('routes')
                run['display_name'] = run['task'].get('display_name') or '商品排查 · ' + run['created'][5:16].replace('T',' ')
                run['state_label'] = run_label(run)
                run['notices'] = [dict(n) for n in db.execute("SELECT n.*,o.sequence,o.at,o.estimated FROM ai_dispatch_notices n JOIN ai_conversation_order o ON o.source='notice' AND o.source_id=CAST(n.id AS TEXT) WHERE n.code=? ORDER BY n.id", (run['code'],))]
                run['events'] = [dict(e) for e in db.execute('SELECT * FROM ai_dispatch_events WHERE code=? ORDER BY id', (run['code'],))]
                run['replies'] = [dict(r) for r in db.execute("SELECT r.request_id,r.role,r.text,r.revision,r.state,r.result,r.created,o.sequence,o.at,o.estimated FROM ai_dispatch_inbox r JOIN ai_conversation_order o ON o.source='reply' AND o.source_id=r.request_id WHERE r.code=? ORDER BY r.rowid", (run['code'],))]
                runs.append(run)
        return {'model': self.model.settings(), 'tasks': tasks, 'runs': runs, 'config_error': config_error}

    def message_preview(self):
        tasks = load(self.plan_path)
        if not tasks:
            return {'preview':True,'messages':[]}
        task = tasks[0]
        evidence = {'employee1':{'summary':'检查后会在这里显示实际输入行数和待核查记录数。'},
                    'employee2':{'summary':'比对完成后会在这里显示实际匹配数量和结果摘要。'}}
        stages = [('manager_wait','manager'),('employee1_wait','employee1'),('employee1_review','employee1'),
                  ('employee2_wait','employee2'),('employee2_review','employee2'),('report_pending','manager')]
        return {'preview':True,'messages':[{'stage':state,'label':LABELS[state],'role':role,
                                            'text':header(role)+stage_message(state,task,evidence)} for state,role in stages]}
