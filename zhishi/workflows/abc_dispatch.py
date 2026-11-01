"""Single-owner ABC dispatch simulation; confirmations are never auto-generated."""
import csv
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import threading
from datetime import timedelta

from zhishi.config import excel_node
from zhishi.core.clock import now, stamp
from .documents import DocumentPlan
from .excel import ExcelExecutor
from .files import digest, encoded, scoped


LABELS = {'scheduled':'等待定时触发','a_waiting':'已派给 A，等待完成确认',
          'a_processing':'正在生成模拟 A 成果','b_waiting':'已交接 B，等待完成确认',
          'b_processing':'正在生成模拟 B 成果','completed':'A、B 已完成，查看 C 汇报状态',
          'failed':'处理失败','blocked':'成果变化，已停止交接'}


class ABCDispatch:
    def __init__(self, pilot, root):
        self.store, self.transport, self.root = pilot.store, pilot.transport, root
        self.output = root/'outputs'/'abc-dispatch'
        self.executor = ExcelExecutor(root,self.output)
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self.notice_lock = threading.Lock()
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS abc_runs (
                  uid TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                  state TEXT NOT NULL, scheduled_at TEXT NOT NULL, route_json TEXT NOT NULL,
                  plan_json TEXT NOT NULL, access_token TEXT NOT NULL,
                  a_report_json TEXT NOT NULL DEFAULT '{}', a_assets_json TEXT NOT NULL DEFAULT '[]',
                  a_digest TEXT, b_report_json TEXT NOT NULL DEFAULT '{}',
                  b_assets_json TEXT NOT NULL DEFAULT '[]', b_digest TEXT,
                  a_confirmed_at TEXT, b_confirmed_at TEXT, a_finished_at TEXT, b_finished_at TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS abc_events (
                  id INTEGER PRIMARY KEY, run_uid TEXT NOT NULL, kind TEXT NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(run_uid,kind));
                CREATE TABLE IF NOT EXISTS abc_notices (
                  id INTEGER PRIMARY KEY, run_uid TEXT NOT NULL, kind TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
                  UNIQUE(run_uid,kind));
            ''')
            # Processing only creates derived files. Keep the actual user's confirmation.
            db.execute("UPDATE abc_notices SET state='unknown',detail='重启前发送未确认，不自动重发。' WHERE state='sending'")

    def row(self, code):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM abc_runs WHERE code=?',(code,)).fetchone()
        if not row:
            raise ValueError('找不到这轮 ABC 模拟。')
        return dict(row)

    @staticmethod
    def event(db, run, kind):
        db.execute('INSERT OR IGNORE INTO abc_events(run_uid,kind,created_at) VALUES (?,?,?)',
                   (run['uid'],kind,stamp()))

    @staticmethod
    def enqueue(db, run, kind):
        db.execute('INSERT OR IGNORE INTO abc_notices(run_uid,kind) VALUES (?,?)',(run['uid'],kind))

    def start(self, delay=30):
        with self.lock,self.store.connect() as db:
            active = db.execute("SELECT code FROM abc_runs WHERE state NOT IN ('completed','failed','blocked') LIMIT 1").fetchone()
            if active:
                return f"已有 ABC 模拟 {active['code']}，请先完成当前任务。"
            route = self.transport.route() if self.transport and self.transport.enabled else None
            if not route:
                raise ValueError('本人微信尚未绑定，无法模拟 A、B、C。')
            plan = DocumentPlan(self.root,self.root/'projects'/'excel-demo').plan()
            plan['title'] = 'ABC 模拟：A 整合，B 复核汇总，C 接收成果'
            plan['recipients_snapshot'] = [{'name':'模拟 B','id':'sim-b'}]
            plan['summary_processor_sha256'] = digest(self.root/'workflows'/'department_summary.mjs')
            uid = secrets.token_hex(12)
            code = 'ABC-'+stamp()[:10].replace('-','')+'-'+uid[:6].upper()
            due = stamp(now()+timedelta(seconds=delay))
            db.execute('''INSERT INTO abc_runs(uid,code,state,scheduled_at,route_json,plan_json,
                       access_token,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                       (uid,code,'scheduled',due,encoded(route),encoded(plan),secrets.token_urlsafe(24),stamp(),stamp()))
            self.event(db,{'uid':uid},'scheduled')
        return f'{code} V1 已安排，{delay} 秒后模拟定时触发。A、B、C 都映射到本人微信；A、B 的完成确认由你发送，C 只接收最终汇报。'

    def verified(self, run, stage):
        assets = json.loads(run[stage.lower()+'_assets_json'])
        if not assets:
            raise ValueError('该角色尚无成果。')
        for item in assets:
            path = scoped(self.output,item['relative_path'])
            if not path.is_file() or digest(path)!=item['sha256']:
                raise ValueError('交接成果已经变化。')
        return assets

    def fingerprint(self, run, stage):
        payload = {'uid':run['uid'],'version':run['version'],'stage':stage,
                   'route':json.loads(run['route_json']),'assets':self.verified(run,stage)}
        if stage=='B':
            if self.fingerprint(run,'A')!=run['a_digest']:
                raise ValueError('A 的成果与交接记录不一致。')
            payload['upstream'] = run['a_digest']
        return hashlib.sha256(encoded(payload).encode()).hexdigest()

    def complete(self, code, version, stage):
        if stage not in ('A','B'):
            raise ValueError('只有 A、B 需要完成确认，C 仅接收汇报。')
        with self.lock,self.store.connect() as db:
            run = self.row(code)
            if version!=run['version']:
                raise ValueError('版本不一致，未执行确认。')
            if run[stage.lower()+'_confirmed_at']:
                return '该角色的完成确认已记录，不重复交接或汇报。'
            if run['state']!=stage.lower()+'_waiting':
                raise ValueError('当前未派发该角色的任务，不能提前确认完成。')
            if not self.transport or not self.transport.enabled or self.transport.route()!=json.loads(run['route_json']):
                raise ValueError('本轮微信绑定已变化，不能继续确认。')
            if stage=='B':
                try:
                    if self.fingerprint(run,'A')!=run['a_digest']:
                        raise ValueError('A 的成果与交接记录不一致。')
                except ValueError as error:
                    db.execute("UPDATE abc_runs SET state='blocked',error=?,updated_at=? WHERE uid=?",(str(error),stamp(),run['uid']))
                    self.enqueue(db,run,'failure')
                    return 'A 的交接文件发生变化，已停止处理，未向 C 汇报成功。'
            db.execute(f"UPDATE abc_runs SET state=?,{stage.lower()}_confirmed_at=?,updated_at=? WHERE uid=?",
                       (stage.lower()+'_processing',stamp(),stamp(),run['uid']))
            self.event(db,run,stage+'_confirmed')
            db.execute("UPDATE abc_notices SET state='cancelled',detail='已收到该角色确认，取消待发派单。' WHERE run_uid=? AND kind=? AND state='pending'",(run['uid'],stage+'_task'))
        return f'{code} V{version}：已收到模拟 {stage} 的完成确认，正在生成并校验本阶段示例成果。'+('通过后自动派给 B。' if stage=='A' else '通过后自动向 C 汇报。')

    def command(self, text):
        if text in ('开始ABC模拟','开始 ABC 模拟','开始三角色模拟'):
            return self.start()
        if text in ('ABC进度','ABC 进度','三角色进度'):
            return '\n'.join(f"{r['code']} V1：{r['state_label']}" for r in self.state()['runs']) or '还没有 ABC 模拟。'
        match = re.fullmatch(r'完成\s*([ABC])\s+(ABC-\d{8}-[A-F0-9]{6})\s+V(\d+)',text,re.I)
        if match:
            return self.complete(match[2].upper(),int(match[3]),match[1].upper())
        return None

    def manifest(self, folder, names):
        return [{'name':n,'relative_path':str((folder/n).relative_to(self.output)),
                 'sha256':digest(folder/n),'bytes':(folder/n).stat().st_size} for n in names]

    def prepare_a(self, run):
        report,assets = self.executor.execute(run)
        folder = self.output/run['code']/'V1'
        (folder/'完成说明.md').write_text(
            f"# 模拟 A 完成说明\n\n{run['code']} V1\n\n本人已发送 A 完成口令，固定技能生成模拟成果；并非人工上传成果。\n"
            f"有效 {report['output_rows']} 行，合计 {report['total_amount']:,.2f}。详细异常见附件。\n\n下一步由模拟 B 复核并汇总。\n",encoding='utf-8')
        assets = self.manifest(folder,[a['name'] for a in assets])
        return report,assets

    def prepare_b(self, run):
        if self.fingerprint(run,'A')!=run['a_digest']:
            raise ValueError('A 的交接成果发生变化。')
        plan = json.loads(run['plan_json'])
        if digest(self.root/'workflows'/'department_summary.mjs')!=plan['summary_processor_sha256']:
            raise ValueError('B 的固定技能已变化。')
        folder = self.output/run['code']/'V1'/'B成果'
        snapshot = folder/'输入快照'
        snapshot.mkdir(parents=True,exist_ok=True)
        assets = {a['name']:a for a in self.verified(run,'A')}
        for name in ('整合结果_示例.xlsx','检查结果.json'):
            shutil.copyfile(scoped(self.output,assets[name]['relative_path']),snapshot/name)
            if digest(snapshot/name)!=assets[name]['sha256']:
                raise ValueError('B 的输入快照校验失败。')
        spec = {'input':str(snapshot/'整合结果_示例.xlsx'),'check':str(snapshot/'检查结果.json'),
                'output':str(folder),'code':run['code'],'version':1}
        spec_file = folder/'执行规格.json'
        spec_file.write_text(encoded(spec),encoding='utf-8')
        result = subprocess.run([str(excel_node()),str(self.root/'workflows'/'department_summary.mjs'),str(spec_file)],
                                cwd=self.root,capture_output=True,encoding='utf-8',errors='replace',timeout=120,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'运行记录.txt').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode:
            raise ValueError('B 的部门汇总技能执行失败。')
        if self.fingerprint(self.row(run['code']),'A')!=run['a_digest']:
            raise ValueError('处理期间 A 的成果变化，未向 C 汇报。')
        report = json.loads((folder/'部门汇总.json').read_text('utf-8'))
        upstream = json.loads(run['a_report_json'])
        if report['rows']!=upstream['output_rows'] or round(report['total_amount']*100)!=round(upstream['total_amount']*100):
            raise ValueError('B 的汇总与 A 不一致。')
        with (folder/'部门汇总.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer = csv.writer(f);writer.writerow(['部门','有效记录','金额合计'])
            for group in report['departments']:
                name = group['department']
                writer.writerow(["'"+name if name.startswith(('=','+','-','@')) else name,group['rows'],f"{group['total_amount']:.2f}"])
        (folder/'部门汇总.md').write_text(
            f"# 模拟 B 汇总成果\n\n来源：{run['code']} V1，A 的已校验交接文件。\n\nB 的完成口令已由本人发送，固定技能已生成示例结果。\n"
            f"共 {report['rows']} 行，合计 {report['total_amount']:,.2f}。\n\n下一步向模拟领导 C 汇报，C 无需确认。\n",encoding='utf-8')
        return report,self.manifest(folder,['部门汇总.json','部门汇总.csv','部门汇总.md'])

    def tick(self):
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            with self.lock,self.store.connect() as db:
                due = db.execute("SELECT * FROM abc_runs WHERE state='scheduled' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1",(stamp(),)).fetchone()
                if due:
                    db.execute("UPDATE abc_runs SET state='a_waiting',updated_at=? WHERE uid=?",(stamp(),due['uid']))
                    self.event(db,due,'A_assigned');self.enqueue(db,due,'A_task')
                row = db.execute("SELECT * FROM abc_runs WHERE state IN ('a_processing','b_processing') ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return
            run = dict(row);stage = 'A' if run['state']=='a_processing' else 'B'
            try:
                report,assets = (self.prepare_a if stage=='A' else self.prepare_b)(run)
                candidate = run | {stage.lower()+'_assets_json':encoded(assets)}
                fingerprint = self.fingerprint(candidate,stage)
                with self.lock,self.store.connect() as db:
                    if stage=='A':
                        db.execute("UPDATE abc_runs SET state='b_waiting',a_report_json=?,a_assets_json=?,a_digest=?,a_finished_at=?,updated_at=? WHERE uid=?",
                                   (encoded(report),encoded(assets),fingerprint,stamp(),stamp(),run['uid']))
                        self.event(db,run,'A_completed');self.event(db,run,'B_assigned');self.enqueue(db,run,'B_task')
                    else:
                        db.execute("UPDATE abc_runs SET state='completed',b_report_json=?,b_assets_json=?,b_digest=?,b_finished_at=?,updated_at=? WHERE uid=?",
                                   (encoded(report),encoded(assets),fingerprint,stamp(),stamp(),run['uid']))
                        self.event(db,run,'B_completed');self.enqueue(db,run,'C_report')
            except Exception as error:
                with self.store.connect() as db:
                    db.execute("UPDATE abc_runs SET state='failed',error=?,updated_at=? WHERE uid=?",(str(error)[:400],stamp(),run['uid']))
                    self.enqueue(db,run,'failure')
        finally:
            self.worker_lock.release()

    def package(self, run, kind):
        code = f"{run['code']} V1"
        intro = '【三角色模拟：A、B、领导 C 均为你本人】\n'
        if kind=='A_task':
            return (intro+f"【派给 A｜报表整合】\n{code}\n定时时间已到，任务现已派给 A。请整合两份示例部门报表，去重并隔离异常。\n"
                    f"本次用完成口令触发固定技能生成模拟 A 成果。回复：完成A {code}\n收到并校验成功后才派给 B；C 尚未收到完成汇报。",[])
        if kind=='B_task':
            if self.fingerprint(run,'A')!=run['a_digest']:
                raise ValueError('A 的交接文件变化。')
            r = json.loads(run['a_report_json'])
            media = [scoped(self.output,a['relative_path']) for a in self.verified(run,'A') if a['name'] in ('整合结果_示例.xlsx','预览.png','异常清单.csv')]
            return (intro+f"【派给 B｜复核与部门汇总】\n{code}\n已收到 A 完成确认，模拟成果校验通过，现交接给 B。\n"
                    f"输入 {r['input_rows']} 行，有效 {r['output_rows']} 行，去重 {r['duplicate_rows']} 行，隔离 {r['invalid_rows']} 行，合计 {r['total_amount']:,.2f}。\n"
                    f"请检查附件。回复：完成B {code}\n本次口令触发固定技能生成模拟 B 汇总，校验后再向 C 汇报。",media)
        if kind=='C_report':
            if self.fingerprint(run,'B')!=run['b_digest']:
                raise ValueError('最终成果发生变化。')
            a,b = json.loads(run['a_report_json']),json.loads(run['b_report_json'])
            groups = '\n'.join(f"{g['department']}：{g['rows']} 行，{g['total_amount']:,.2f}" for g in b['departments'])
            media = [scoped(self.output,x['relative_path']) for x in self.verified(run,'A') if x['name']=='整合结果_示例.xlsx']
            media += [scoped(self.output,x['relative_path']) for x in self.verified(run,'B') if x['name']=='部门汇总.csv']
            return (intro+f"【领导 C｜最终完成汇报】\n{code}\nA 已确认完成，整合成果校验通过；B 已确认完成，部门汇总校验通过。\n"
                    f"{groups}\n有效 {b['rows']} 行，合计 {b['total_amount']:,.2f}。原始异常 {a['invalid_rows']} 行已隔离，去重 {a['duplicate_rows']} 行。\n"
                    f"附件：最终 Excel、部门汇总。C 只接收汇报，无需确认。\n仅本人三角色模拟，未联系其他人员。",media)
        return intro+f"【流程异常】\n{code}\n{run['error']}\n没有向 C 汇报任务成功。",[]

    def notify_tick(self):
        if not self.notice_lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT n.*,r.code FROM abc_notices n JOIN abc_runs r ON r.uid=n.run_uid WHERE n.state='pending' ORDER BY n.id LIMIT 1").fetchone()
                if not row or db.execute("UPDATE abc_notices SET state='sending' WHERE id=? AND state='pending'",(row['id'],)).rowcount!=1:
                    return
            try:
                run = self.row(row['code']);route = json.loads(run['route_json'])
                required = {'A_task':'a_waiting','B_task':'b_waiting','C_report':'completed'}.get(row['kind'])
                if required and run['state']!=required:
                    state,detail = 'cancelled','阶段已变化，未发送过期通知。'
                elif not self.transport or not self.transport.enabled or self.transport.route()!=route:
                    state,detail = 'blocked','绑定已变化或不可用，没有发送给新会话。'
                else:
                    text,media = self.package(run,row['kind'])
                    state,detail = self.transport.send_package(text,media,expected_route=route)
            except ValueError:
                state,detail = 'blocked','成果校验未通过，未发送。'
            except Exception:
                state,detail = 'unknown','提交结果未知，不自动重发。'
            with self.store.connect() as db:
                db.execute('UPDATE abc_notices SET state=?,detail=? WHERE id=?',(state,detail,row['id']))
                if row['kind']=='C_report' and state=='submitted_unverified':
                    self.event(db,{'uid':row['run_uid']},'C_report_submitted')
        finally:
            self.notice_lock.release()

    def state(self):
        with self.store.connect() as db:
            runs = []
            for row in db.execute('SELECT * FROM abc_runs ORDER BY created_at DESC,rowid DESC LIMIT 10'):
                run = dict(row)
                runs.append({k:run[k] for k in ('uid','code','version','state','scheduled_at','created_at','updated_at','error','access_token','a_confirmed_at','b_confirmed_at','a_finished_at','b_finished_at')} | {
                    'state_label':LABELS[run['state']], 'a_report':json.loads(run['a_report_json']),
                    'b_report':json.loads(run['b_report_json']), 'a_assets':json.loads(run['a_assets_json']),
                    'b_assets':json.loads(run['b_assets_json']),
                    'events':[dict(e) for e in db.execute('SELECT kind,created_at FROM abc_events WHERE run_uid=? ORDER BY id',(run['uid'],))],
                    'notices':[dict(n) for n in db.execute('SELECT kind,state,detail FROM abc_notices WHERE run_uid=? ORDER BY id',(run['uid'],))]})
        return {'demo':True,'model_used':False,'roles':'A、B、C 均为本人微信','runs':runs}

    def asset(self, uid, stage, name, token):
        if stage not in ('A','B'):
            raise ValueError('角色无效。')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM abc_runs WHERE uid=?',(uid,)).fetchone()
        if not row or not secrets.compare_digest(row['access_token'],token):
            raise ValueError('文件访问凭证无效。')
        for item in self.verified(dict(row),stage):
            if item['name']==name:
                return scoped(self.output,item['relative_path'])
        raise ValueError('找不到该文件。')
