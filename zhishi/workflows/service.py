"""Coordinate workflow versions, explicit approvals and background scheduling."""
import hashlib
import json
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from zhishi.core.clock import CN, timestamp
from zhishi.channels.local_inbox import LocalInbox
from .artifacts import WorkflowArtifacts
from .documents import DocumentPlan
from .excel import ExcelExecutor
from .files import encoded
from .notifications import WorkflowNotifications
from .schema import initialize


class Workflows:
    def __init__(self, pilot, root):
        self.pilot = getattr(pilot, "store", pilot)
        self.root = Path(root).resolve()
        self.project = self.root / "projects" / "excel-demo"
        self.output = self.root / "outputs" / "01a1963b-workflow"
        self.lock = threading.RLock()
        self.scan_count = 0
        initialize(self.pilot)
        self.documents = DocumentPlan(self.root, self.project)
        self.artifacts = WorkflowArtifacts(self.pilot, self.output)
        self.executor = ExcelExecutor(self.root, self.output)
        self.delivery = LocalInbox(self.project, self.output)
        self.notifications = WorkflowNotifications(self.artifacts, pilot.transport, self.output)

    def plan(self):
        return self.documents.plan()

    def row(self, code, version):
        return self.artifacts.row(code, version)

    def assets(self, run):
        return self.artifacts.assets(run)

    def approval_digest(self, run):
        return self.artifacts.approval_digest(run)

    def asset(self, uid, name, token):
        return self.artifacts.asset(uid, name, token)

    def notice(self, row):
        return self.notifications.notice(row)

    def start(self,force_code=None):
        plan=self.plan();fp=hashlib.sha256(encoded(plan).encode()).hexdigest()
        with self.lock,self.pilot.connect() as db:
            latest=db.execute('SELECT * FROM workflow_runs WHERE task_key=? AND work_date=? ORDER BY version DESC LIMIT 1',
                              (plan['task_id'],plan['work_date_resolved'])).fetchone()
            if force_code:
                if not latest or latest['code']!=force_code: raise ValueError('找不到今天的这项工作。')
                if latest['state'] in ('queued','running','approved','sending'): raise ValueError('这项工作仍在执行或投递，请等待当前步骤结束。')
            elif latest and latest['fingerprint']==fp:
                return f"已有今日工作 {latest['code']} V{latest['version']}，状态：{self.label(latest['state'])}。不会重复执行。"
            if latest and latest['state'] in ('queued','running','approved','sending'): raise ValueError('已有任务正在执行，请等待结果。')
            code=latest['code'] if latest else 'JOB-'+plan['work_date_resolved'].replace('-','')+'-001'
            version=latest['version']+1 if latest else 1
            if latest and latest['state']=='awaiting_review':
                db.execute("UPDATE workflow_runs SET state='superseded' WHERE uid=?",(latest['uid'],))
                db.execute("UPDATE workflow_notices SET state='cancelled' WHERE run_uid=? AND state='pending'",(latest['uid'],))
            db.execute('INSERT INTO workflow_runs(uid,code,version,task_key,work_date,fingerprint,state,created_at,review_token,plan_json) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (secrets.token_hex(12),code,version,plan['task_id'],plan['work_date_resolved'],fp,'queued',timestamp(),secrets.token_urlsafe(24),encoded(plan)))
            return f'{code} V{version} 已开始。正在读取项目文档、执行 Excel 整合；完成后会发给你验收。全部为示例数据，最终收件人为本地模拟收件箱。'

    @staticmethod
    def label(state):
        return {'queued':'等待执行','running':'执行中','failed':'执行失败','awaiting_review':'等待本人验收','approved':'已确认，等待投递','sending':'模拟投递中','delivered':'模拟投递完成','superseded':'已被新版本替代','blocked':'文件已变化，需重新制作','rejected':'要求修改'}.get(state,state)

    def approve(self,code,version):
        with self.lock,self.pilot.connect() as db:
            run=self.row(code,version)
            latest=db.execute('SELECT max(version) FROM workflow_runs WHERE code=?',(code,)).fetchone()[0]
            if int(version)!=latest or run['state']=='superseded': raise ValueError('这是旧版本，请验收最新版本。')
            if run['state'] in ('approved','sending','delivered'): return '这份结果已经确认过，不会重复投递。'
            if run['state']!='awaiting_review': raise ValueError('这份结果当前不能确认发送。')
            try: approved=self.approval_digest(run)
            except ValueError:
                db.execute("UPDATE workflow_runs SET state='blocked',error='验收文件已改变。' WHERE uid=?",(run['uid'],))
                return '验收文件已改变，本次没有发送。请重新制作并验收新版本。'
            db.execute("UPDATE workflow_runs SET state='approved',approved_at=?,approved_digest=? WHERE uid=?",(timestamp(),approved,run['uid']))
            for recipient in json.loads(run['plan_json'])['recipients_snapshot']:
                db.execute("INSERT OR IGNORE INTO workflow_deliveries VALUES (?,?,'pending','')",(run['uid'],recipient['id']))
            return f'已确认 {code} V{version}。将把本次验收的文件投递到两名模拟收件人的本地收件箱；不会发送给真实联系人。'

    def command(self,text):
        if text in ('开始今日工作','开始今日任务'): return self.start()
        if text in ('今日工作','查看今日工作','工作进度'):
            jobs=self.state()['jobs']
            return '还没有今日工作。发送“开始今日工作”运行示例。' if not jobs else '\n'.join(f"{j['code']} V{j['version']}：{j['state_label']}" for j in jobs)
        match=re.fullmatch(r'确认发送\s+(JOB-\d{8}-\d{3})\s+V(\d+)',text,re.I)
        if match:return self.approve(match[1].upper(),int(match[2]))
        match=re.fullmatch(r'重新制作\s+(JOB-\d{8}-\d{3})',text,re.I)
        if match:return self.start(match[1].upper())
        match=re.fullmatch(r'要求修改\s+(JOB-\d{8}-\d{3})\s+V(\d+)',text,re.I)
        if match:
            run=self.row(match[1].upper(),int(match[2]))
            with self.pilot.connect() as db:
                if run['state']!='awaiting_review':return '当前版本已经不在等待验收。'
                db.execute("UPDATE workflow_runs SET state='rejected' WHERE uid=?",(run['uid'],))
            return '已停止本版投递。修改项目文档或输入文件后，发送“重新制作 '+run['code']+'”。'
        if text in ('确认','没问题','确认发送'):return '请带上任务和版本确认，例如“确认发送 JOB-20261101-001 V1”，避免发送错版本。'
        return None

    def execute(self, run):
        report, assets = self.executor.execute(run)
        with self.lock,self.pilot.connect() as db:
            db.execute("UPDATE workflow_runs SET state='awaiting_review',finished_at=?,report_json=?,assets_json=? WHERE uid=? AND state='running'",(timestamp(),encoded(report),encoded(assets),run['uid']))
            db.execute("INSERT OR IGNORE INTO workflow_notices(run_uid,kind) VALUES (?,'review')",(run['uid'],))
            self.pilot.message(db,'assistant',f"{run['code']} V{run['version']} 已完成，等待你验收。输入 {report['input_rows']} 行，保留 {report['output_rows']} 行，去重 {report['duplicate_rows']} 行，隔离 {report['invalid_rows']} 行。确认后的收件人为本地模拟收件箱。")

    def deliver(self,run):
        if not run['approved_digest'] or self.approval_digest(run)!=run['approved_digest']: raise ValueError('文件或收件人快照与确认记录不一致。')
        assets=self.assets(run);plan=json.loads(run['plan_json'])
        with self.pilot.connect() as db:db.execute("UPDATE workflow_runs SET state='sending' WHERE uid=?",(run['uid'],))
        for recipient in plan['recipients_snapshot']:
            self.delivery.send(run, recipient, assets)
            with self.pilot.connect() as db:db.execute("UPDATE workflow_deliveries SET state='delivered_local',detail='已复制并核对文件摘要。' WHERE run_uid=? AND recipient_id=?",(run['uid'],recipient['id']))
        with self.pilot.connect() as db:
            db.execute("UPDATE workflow_runs SET state='delivered' WHERE uid=?",(run['uid'],))
            db.execute("INSERT OR IGNORE INTO workflow_notices(run_uid,kind) VALUES (?,'delivery')",(run['uid'],))
            self.pilot.message(db,'assistant',f"{run['code']} V{run['version']} 已投递到两名模拟收件人的本地收件箱。未联系真实人员。")

    def tick(self):
        with self.lock,self.pilot.connect() as db:
            row=db.execute("SELECT * FROM workflow_runs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:db.execute("UPDATE workflow_runs SET state='running' WHERE uid=? AND state='queued'",(row['uid'],))
        if row:
            try:self.execute(dict(row))
            except Exception as error:
                with self.pilot.connect() as db:
                    db.execute("UPDATE workflow_runs SET state='failed',error=? WHERE uid=?",(str(error)[:500],row['uid']))
                    db.execute("INSERT OR IGNORE INTO workflow_notices(run_uid,kind) VALUES (?,'failure')",(row['uid'],))
                    self.pilot.message(db,'assistant',f"{row['code']} V{row['version']} 执行失败，请查看今日工作页面。")
        with self.pilot.connect() as db:approved=db.execute("SELECT * FROM workflow_runs WHERE state IN ('approved','sending') ORDER BY created_at LIMIT 1").fetchone()
        if approved:
            try:self.deliver(dict(approved))
            except Exception as error:
                with self.pilot.connect() as db:
                    db.execute("UPDATE workflow_runs SET state='blocked',error=? WHERE uid=?",(str(error)[:500],approved['uid']))
                    db.execute("INSERT OR IGNORE INTO workflow_notices(run_uid,kind) VALUES (?,'failure')",(approved['uid'],))
        with self.pilot.connect() as db:
            notice=db.execute("SELECT n.*,r.code,r.version FROM workflow_notices n JOIN workflow_runs r ON r.uid=n.run_uid WHERE n.state='pending' AND (n.kind IN ('delivery','failure') OR r.state='awaiting_review') ORDER BY n.id LIMIT 1").fetchone()
            if notice:db.execute("UPDATE workflow_notices SET state='sending' WHERE id=? AND state='pending'",(notice['id'],))
        if notice:
            try:state,detail=self.notice(dict(notice))
            except Exception:state,detail='unknown','发送结果未确认，材料仍保留在工作台。'
            with self.pilot.connect() as db:db.execute('UPDATE workflow_notices SET state=?,detail=? WHERE id=?',(state,detail,notice['id']))
        self.scan_count+=1
        if self.scan_count%30==0:
            try:
                plan=self.plan()
                if plan.get('auto_run') and re.fullmatch(r'\d{2}:\d{2}',plan.get('run_at','')) and datetime.now(CN).strftime('%H:%M')>=plan['run_at']:self.start()
            except (OSError,ValueError,KeyError):pass

    def state(self):
        with self.pilot.connect() as db:
            rows=db.execute('SELECT * FROM workflow_runs ORDER BY created_at DESC,version DESC LIMIT 20').fetchall()
            jobs=[]
            for row in rows:
                run=dict(row);plan=json.loads(run['plan_json'])
                jobs.append({k:run[k] for k in ['uid','code','version','state','created_at','finished_at','approved_at','error','review_token']}|{
                    'state_label':self.label(run['state']),'title':plan['title'],'report':json.loads(run['report_json']),
                    'assets':json.loads(run['assets_json']),'recipients':[r['name'] for r in plan['recipients_snapshot']],
                    'notices':[dict(r) for r in db.execute('SELECT kind,state,detail FROM workflow_notices WHERE run_uid=?',(run['uid'],))],
                    'deliveries':[dict(r) for r in db.execute('SELECT recipient_id,state,detail FROM workflow_deliveries WHERE run_uid=?',(run['uid'],))]})
            return {'demo':True,'jobs':jobs,'project_document':str(self.project/'项目文档.md')}
