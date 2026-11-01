"""Local two-role experiment: merge -> explicit A approval -> summary -> B approval.

Roles are simulated by one local user. This does not implement employee authentication.
Optional owner notifications pin both roles to the same explicitly bound WeChat route.
"""
import hashlib
import csv
import json
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from zhishi.config import excel_node
from zhishi.core.clock import timestamp
from .documents import DocumentPlan
from .excel import ExcelExecutor
from .files import digest, encoded, scoped
from .two_stage_notifications import TwoStageNotifications


LABELS = {
    'a_queued':'第一段等待执行', 'a_running':'A 正在整合', 'a_review':'等待模拟 A 确认',
    'b_queued':'已交接，B 等待执行', 'b_running':'B 正在汇总', 'b_review':'等待模拟 B 确认',
    'completed':'两段式验证完成', 'failed':'执行失败', 'blocked':'文件发生变化，流程已阻止',
}


class TwoStageDemo:
    def __init__(self, pilot, root):
        self.store = pilot.store
        self.root = Path(root).resolve()
        self.output = self.root/'outputs'/'two-stage'
        self.executor = ExcelExecutor(self.root, self.output)
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS two_stage_runs (
                  uid TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, version INTEGER NOT NULL,
                  state TEXT NOT NULL, plan_json TEXT NOT NULL, access_token TEXT NOT NULL,
                  a_report_json TEXT NOT NULL DEFAULT '{}', a_assets_json TEXT NOT NULL DEFAULT '[]',
                  a_approved_digest TEXT, b_report_json TEXT NOT NULL DEFAULT '{}',
                  b_assets_json TEXT NOT NULL DEFAULT '[]', b_approved_digest TEXT,
                  b_attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS two_stage_events (
                  id TEXT PRIMARY KEY, run_uid TEXT NOT NULL, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL, consumed_at TEXT,
                  UNIQUE(run_uid,kind));
            ''')
            # Both steps only create local derived files; interrupted work can safely rerun.
            db.execute("UPDATE two_stage_runs SET state='a_queued' WHERE state='a_running'")
            db.execute("UPDATE two_stage_runs SET state='b_queued' WHERE state='b_running'")
            columns = {r['name'] for r in db.execute('PRAGMA table_info(two_stage_runs)')}
            if 'notice_enabled' not in columns:
                db.execute('ALTER TABLE two_stage_runs ADD COLUMN notice_enabled INTEGER NOT NULL DEFAULT 0')
            if 'notice_route_json' not in columns:
                db.execute("ALTER TABLE two_stage_runs ADD COLUMN notice_route_json TEXT NOT NULL DEFAULT '{}'")
        self.notifications = TwoStageNotifications(self,pilot.transport)

    def row(self, code):
        with self.store.connect() as db:
            value = db.execute('SELECT * FROM two_stage_runs WHERE code=?',(code,)).fetchone()
        if not value:
            raise ValueError('找不到这次两段式验证。')
        return dict(value)

    def start(self, *, wechat=False):
        with self.lock, self.store.connect() as db:
            active = db.execute("SELECT code,notice_enabled FROM two_stage_runs WHERE state NOT IN ('completed','failed','blocked') ORDER BY created_at DESC LIMIT 1").fetchone()
            if active:
                if bool(active['notice_enabled'])!=wechat:
                    raise ValueError('已有其他通道的两段式验证未完成，请先完成当前验证。')
                return f"已有两段式验证 {active['code']}，请先完成或查看当前进度。"
            route = {}
            if wechat:
                transport = self.notifications.transport
                route = transport.route() if transport and transport.enabled else None
                if not route:
                    raise ValueError('本人微信尚未绑定，无法将 A、B 映射到本人会话。')
            plan = DocumentPlan(self.root,self.root/'projects'/'excel-demo').plan()
            plan['title'] = '两段式验证：A 整合报表'
            plan['recipients_snapshot'] = [{'id':'sim-b','name':'模拟 B','channel':'local_demo','folder':'模拟B'}]
            plan['summary_processor_sha256'] = digest(self.root/'workflows'/'department_summary.mjs')
            uid = secrets.token_hex(12)
            code = 'PAIR-'+timestamp()[:10].replace('-','')+'-'+uid[:6].upper()
            db.execute('INSERT INTO two_stage_runs(uid,code,version,state,plan_json,access_token,created_at,updated_at,notice_enabled,notice_route_json) VALUES (?,?,1,?,?,?,?,?,?,?)',
                       (uid,code,'a_queued',encoded(plan),secrets.token_urlsafe(24),timestamp(),timestamp(),int(wechat),encoded(route)))
            self.store.message(db,'assistant',f'{code} V1：第一段开始。B 必须等待你以模拟 A 身份确认本版结果。')
        channel = 'A、B 的验收材料都发到本人已绑定微信，未向其他联系人分发。' if wechat else '本次仅在本地验证，不发送给真实联系人。'
        return f'{code} V1 已开始。A 整合 Excel，确认后自动交接给 B；'+channel

    def verified_assets(self, run, stage):
        assets = json.loads(run[stage.lower()+'_assets_json'])
        if not assets:
            raise ValueError('该阶段尚未产生文件。')
        for item in assets:
            file = scoped(self.output,item['relative_path'])
            if not file.is_file() or digest(file)!=item['sha256']:
                raise ValueError('已验收材料发生变化，请重新发起验证。')
        return assets

    def approval_digest(self, run, stage):
        payload = {'uid':run['uid'],'version':run['version'],'stage':stage,
                   'assets':self.verified_assets(run,stage),'target':'sim-b' if stage=='A' else 'local-completion'}
        if run['notice_enabled']:
            payload['role_binding'] = json.loads(run['notice_route_json'])
        if stage=='B':
            if self.approval_digest(run,'A')!=run['a_approved_digest']:
                raise ValueError('第一段成果与交接版本不一致。')
            payload['upstream_digest'] = run['a_approved_digest']
        return hashlib.sha256(encoded(payload).encode()).hexdigest()

    def approve(self, code, version, stage, role):
        if stage not in ('A','B') or role!=stage:
            raise ValueError('请使用对应的模拟角色确认该阶段。这不是企业身份认证。')
        with self.lock,self.store.connect() as db:
            run = self.row(code)
            if int(version)!=run['version']:
                raise ValueError('版本不一致，未执行确认。')
            if run[stage.lower()+'_approved_digest']:
                return '本阶段已经确认，不会重复交接或重复生成任务。'
            if run['state']!=stage.lower()+'_review':
                raise ValueError('该阶段尚未完成，当前不能确认。')
            try:
                approved = self.approval_digest(run,stage)
            except ValueError as error:
                db.execute("UPDATE two_stage_runs SET state='blocked',error=?,updated_at=? WHERE uid=?",(str(error),timestamp(),run['uid']))
                return '文件已经变化，本次没有交接或完成确认。请重新发起验证。'
            payload = {'code':code,'version':int(version),'from':'sim-'+stage.lower(),
                       'to':'sim-b' if stage=='A' else 'local-completion','approved_digest':approved,
                       'assets':json.loads(run[stage.lower()+'_assets_json']),
                       'approval_actor':'本人（模拟 '+stage+'）'}
            next_state = 'b_queued' if stage=='A' else 'completed'
            column = stage.lower()+'_approved_digest'
            db.execute(f'UPDATE two_stage_runs SET state=?,{column}=?,updated_at=? WHERE uid=?',
                       (next_state,approved,timestamp(),run['uid']))
            db.execute('INSERT INTO two_stage_events(id,run_uid,kind,payload_json,state,created_at,consumed_at) VALUES (?,?,?,?,?,?,?)',
                       (secrets.token_hex(12),run['uid'],stage+'_approved',encoded(payload),
                        'pending' if stage=='A' else 'recorded',timestamp(),None))
            db.execute("UPDATE two_stage_notices SET state='cancelled',detail='该阶段已确认，不再发送过期验收通知。' WHERE run_uid=? AND kind=? AND state='pending'",(run['uid'],stage.lower()+'_review'))
            if stage=='B':
                self.notifications.enqueue(db,run,'completed')
            reply = f'{code} V{version}：A 已确认，交接已入队。B 将自动读取本版 Excel 并生成部门汇总。' if stage=='A' else f'{code} V{version}：B 已确认，两段式验证完成。未向其他联系人分发。'
            self.store.message(db,'assistant',reply)
            return reply

    def command(self, text):
        import re
        if text=='开始两段式验证':
            return self.start()
        if text=='开始微信两段式验证':
            return self.start(wechat=True)
        if text in ('两段式进度','两段式验证进度'):
            runs = self.state()['runs']
            return '\n'.join(f"{r['code']} V{r['version']}：{r['state_label']}" for r in runs) or '还没有两段式验证，发送“开始两段式验证”。'
        match = re.fullmatch(r'确认(第一段|第二段)\s+(PAIR-\d{8}-[A-F0-9]{6})\s+V(\d+)',text,re.I)
        if match:
            stage = 'A' if match[1]=='第一段' else 'B'
            return self.approve(match[2].upper(),int(match[3]),stage,stage)
        return None

    def manifest(self, folder, names):
        return [{'name':name,'relative_path':str((folder/name).relative_to(self.output)),
                 'sha256':digest(folder/name),'bytes':(folder/name).stat().st_size} for name in names]

    def execute_a(self, run):
        report, assets = self.executor.execute(run)
        folder = self.output/run['code']/('V'+str(run['version']))
        (folder/'完成说明.md').write_text(
            f"# 第一段：模拟 A 整合报表\n\n仅示例数据。输入 {report['input_rows']} 行，保留 {report['output_rows']} 行，去重 {report['duplicate_rows']} 行，隔离 {report['invalid_rows']} 行，合计 {report['total_amount']:,.2f}。\n\n"
            f"核对 Excel 与异常清单后，回复：确认第一段 {run['code']} V{run['version']}\n\n确认后自动交接给模拟 B。微信模式下两个角色均为本人；未向其他联系人分发。\n",encoding='utf-8')
        assets = self.manifest(folder,[a['name'] for a in assets])
        with self.store.connect() as db:
            db.execute("UPDATE two_stage_runs SET state='a_review',a_report_json=?,a_assets_json=?,updated_at=? WHERE uid=? AND state='a_running'",
                       (encoded(report),encoded(assets),timestamp(),run['uid']))
            self.notifications.enqueue(db,run,'a_review')
            self.store.message(db,'assistant',f"{run['code']} V1：A 已完成，等待第一段确认；B 尚未开始。")

    def execute_b(self, run):
        if self.approval_digest(run,'A')!=run['a_approved_digest']:
            raise ValueError('上游成果与 A 确认的版本不同，B 未执行。')
        plan = json.loads(run['plan_json'])
        if digest(self.root/'workflows'/'department_summary.mjs')!=plan['summary_processor_sha256']:
            raise ValueError('第二段技能已变化，请重新发起验证。')
        with self.store.connect() as db:
            event = db.execute("SELECT * FROM two_stage_events WHERE run_uid=? AND kind='A_approved'",(run['uid'],)).fetchone()
        if not event or json.loads(event['payload_json'])['approved_digest']!=run['a_approved_digest']:
            raise ValueError('缺少有效交接事件。')
        folder = self.output/run['code']/('V'+str(run['version']))/'第二段'
        snapshot = folder/'输入快照'
        snapshot.mkdir(parents=True,exist_ok=True)
        items = {a['name']:a for a in self.verified_assets(run,'A')}
        for name in ['整合结果_示例.xlsx','检查结果.json']:
            item = items[name]
            shutil.copyfile(scoped(self.output,item['relative_path']),snapshot/name)
            if digest(snapshot/name)!=item['sha256']:
                raise ValueError('B 收到的输入与交接摘要不一致。')
        handoff = json.loads(event['payload_json']) | {'event_id':event['id'],'skill':'department.summary.v1'}
        (folder/'交接单.json').write_text(encoded(handoff),encoding='utf-8')
        spec = {'input':str(snapshot/'整合结果_示例.xlsx'),'check':str(snapshot/'检查结果.json'),
                'output':str(folder),'code':run['code'],'version':run['version']}
        spec_file = folder/'执行规格.json'
        spec_file.write_text(encoded(spec),encoding='utf-8')
        result = subprocess.run([str(excel_node()),str(self.root/'workflows'/'department_summary.mjs'),str(spec_file)],
                                cwd=self.root,capture_output=True,encoding='utf-8',errors='replace',timeout=120,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'运行记录.txt').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode:
            raise ValueError('B 的汇总技能执行失败，请查看运行记录。')
        # Validate the upstream again before publishing B's derived result.
        if self.approval_digest(self.row(run['code']),'A')!=run['a_approved_digest']:
            raise ValueError('B 执行期间上游材料变化，结果未发布。')
        report = json.loads((folder/'部门汇总.json').read_text('utf-8'))
        upstream = json.loads(run['a_report_json'])
        if report['rows']!=upstream['output_rows'] or round(report['total_amount']*100)!=round(upstream['total_amount']*100):
            raise ValueError('B 的行数或金额与 A 的交接成果不一致。')
        with (folder/'部门汇总.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['部门','有效记录','金额合计'])
            for group in report['departments']:
                name = group['department']
                if name.startswith(('=','+','-','@')):
                    name = "'"+name
                writer.writerow([name,group['rows'],f"{group['total_amount']:.2f}"])
        assets = self.manifest(folder,['部门汇总.md','部门汇总.json','交接单.json','部门汇总.csv'])
        with self.store.connect() as db:
            db.execute("UPDATE two_stage_runs SET state='b_review',b_report_json=?,b_assets_json=?,updated_at=? WHERE uid=? AND state='b_running'",
                       (encoded(report),encoded(assets),timestamp(),run['uid']))
            self.notifications.enqueue(db,run,'b_review')
            db.execute("UPDATE two_stage_events SET state='consumed',consumed_at=? WHERE id=?",(timestamp(),event['id']))
            self.store.message(db,'assistant',f"{run['code']} V1：B 已接收 A 的本版 Excel，部门汇总已完成，等待第二段确认。")

    def tick(self):
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM two_stage_runs WHERE state IN ('a_queued','b_queued') ORDER BY created_at LIMIT 1").fetchone()
                if not row:
                    return
                running = row['state'].replace('_queued','_running')
                claimed = db.execute("UPDATE two_stage_runs SET state=?,updated_at=?,b_attempts=b_attempts+? WHERE uid=? AND state=?",
                                     (running,timestamp(),int(running=='b_running'),row['uid'],row['state'])).rowcount
            if claimed!=1:
                return
            try:
                (self.execute_a if running=='a_running' else self.execute_b)(dict(row))
            except Exception as error:
                with self.store.connect() as db:
                    db.execute("UPDATE two_stage_runs SET state='failed',error=?,updated_at=? WHERE uid=?",(str(error)[:500],timestamp(),row['uid']))
                    self.notifications.enqueue(db,row,'failure')
        finally:
            self.worker_lock.release()

    def state(self):
        with self.store.connect() as db:
            runs = []
            for row in db.execute('SELECT * FROM two_stage_runs ORDER BY created_at DESC,rowid DESC LIMIT 10'):
                run = dict(row)
                runs.append({k:run[k] for k in ['uid','code','version','state','access_token','created_at','updated_at','error','b_attempts','notice_enabled']} | {
                    'state_label':LABELS[run['state']], 'a_report':json.loads(run['a_report_json']),
                    'b_report':json.loads(run['b_report_json']), 'a_assets':json.loads(run['a_assets_json']),
                    'b_assets':json.loads(run['b_assets_json']),
                    'events':[dict(e) for e in db.execute('SELECT kind,state,created_at,consumed_at FROM two_stage_events WHERE run_uid=?',(run['uid'],))],
                    'notices':[dict(n) for n in db.execute('SELECT kind,state,detail FROM two_stage_notices WHERE run_uid=? ORDER BY id',(run['uid'],))],
                    'role_binding':'A、B 均为本人已绑定微信' if run['notice_enabled'] else '本机模拟 A、B'})
            return {'demo':True,'roles_simulated':True,'model_used':False,'runs':runs}

    def asset(self, uid, stage, name, token):
        if stage not in ('A','B'):
            raise ValueError('阶段无效。')
        with self.store.connect() as db:
            value = db.execute('SELECT * FROM two_stage_runs WHERE uid=?',(uid,)).fetchone()
        if not value or not secrets.compare_digest(value['access_token'],token):
            raise ValueError('访问凭证无效。')
        run = dict(value)
        for item in self.verified_assets(run,stage):
            if item['name']==name:
                return scoped(self.output,item['relative_path'])
        raise ValueError('找不到该阶段的文件。')
