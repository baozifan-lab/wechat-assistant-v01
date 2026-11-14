"""Invoice handoff demo. Dates are synthetic; source records are read only."""
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

import openpyxl
from zhishi.core.clock import now, stamp

SAMPLE_LIMIT = 140
INVOICE_SOURCE = 'PostgreSQL localdb.retail_demo.发票库'
SAMPLE_METHOD = f'按数据库内部行位置排序，取前{SAMPLE_LIMIT}条样本'
DATE_METHOD = '按样本顺序循环分配7天模拟日期，不按真实业务日期筛选'
INVOICE_QUERY = f'SELECT * FROM retail_demo."发票库" ORDER BY ctid LIMIT {SAMPLE_LIMIT}'


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_invoice_samples():
    # Use the existing local PostgreSQL credentials and force a read-only
    # transaction. Credentials are never placed in command arguments or logs.
    base = Path.home() / 'AppData' / 'Local' / 'LocalPostgreSQL'
    executable = base / 'pgsql' / 'bin' / 'psql.exe'
    env = os.environ.copy()
    try:
        env['PGPASSWORD'] = (base / 'password.txt').read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise ValueError('找不到本机 PostgreSQL 连接配置。') from exc
    env['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=15000'
    env['PGCLIENTENCODING'] = 'UTF8'
    query = INVOICE_QUERY
    try:
        result = subprocess.run([str(executable), '-X', '-h', '127.0.0.1', '-p', '5432',
            '-U', 'postgres', '-d', 'localdb', '-v', 'ON_ERROR_STOP=1', '--csv', '-f', '-'],
            input=query, capture_output=True, text=True, encoding='utf-8', env=env, timeout=25,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('数据库连接失败，请检查只读连接。') from exc
    if result.returncode:
        raise ValueError('数据库读取失败，请检查只读连接和表权限。')
    rows = list(csv.reader(io.StringIO(result.stdout.lstrip('\ufeff\r\n'))))
    rows = [row for row in rows if row]
    if len(rows) < 2 or '商品名称' not in rows[0]:
        raise ValueError('数据库未返回可用的发票样本。')
    return rows[0], rows[1:]


def export_samples(path, start, rows):
    headers, records = rows
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet('模拟日期发票样本')
    sheet.append(['模拟日期'] + headers)
    for index, record in enumerate(records):
        # Dates are evenly assigned across the seven-day window, never claimed
        # to be actual transaction dates.
        safe = ["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value
                for value in record]
        sheet.append([(start + timedelta(days=index % 7)).isoformat()] + safe)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return len(records), file_hash(path)


def extraction_details(start, end, headers):
    return {'来源': INVOICE_SOURCE, '取数方式': SAMPLE_METHOD, '模拟日期规则': DATE_METHOD,
            '模拟日期范围': f'{start} 至 {end}（含首尾）', '字段': list(headers)}


def extraction_comparison(current, previous):
    if not previous or not all(key in previous for key in current):
        return '上次取数未保存完整配置，无法比较。'
    changed = []
    for key, value in current.items():
        if previous[key] != value:
            if key == '字段':
                changed.append(f"字段：{'、'.join(previous[key])} → {'、'.join(value)}")
            else:
                changed.append(f'{key}：{previous[key]} → {value}')
    return '；'.join(changed) if changed else '与上次记录的取数配置和条件一致。'


class InvoiceDemo:
    def __init__(self, pilot, root):
        self.pilot, self.store = pilot, pilot.store
        self.output = Path(root) / 'outputs' / 'invoice-demo'
        self.lock = threading.Lock()
        with self.store.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS invoice_demo_runs
              (code TEXT PRIMARY KEY, state TEXT NOT NULL, detail TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS invoice_demo_notices
              (id INTEGER PRIMARY KEY, code TEXT NOT NULL, kind TEXT NOT NULL,
               state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '', UNIQUE(code,kind));
            ''')
            db.execute('CREATE TABLE IF NOT EXISTS invoice_conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, notice_id INTEGER UNIQUE, role TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL)')
            for n in db.execute('SELECT * FROM invoice_demo_notices ORDER BY id'):
                db.execute('INSERT OR IGNORE INTO invoice_conversations(code,notice_id,role,text,at) VALUES (?,?,?,?,?)',
                           (n['code'], n['id'], 'assistant', '历史通知：' + n['kind'] + '（旧记录未保存正文和发送时间）', ''))
            db.execute("UPDATE invoice_demo_notices SET state='unknown', detail='重启前发送结果未知，不自动重发' WHERE state='sending'")

    @staticmethod
    def enqueue(db, code, kind):
        db.execute('INSERT OR IGNORE INTO invoice_demo_notices(code,kind) VALUES (?,?)', (code, kind))
        notice = db.execute('SELECT id FROM invoice_demo_notices WHERE code=? AND kind=?', (code, kind)).fetchone()
        db.execute('INSERT OR IGNORE INTO invoice_conversations(code,notice_id,role,text,at) VALUES (?,?,?,?,?)',
                   (code, notice['id'], 'assistant', {'manager':'等待负责人确认启动', 'employee1_start':'已安排系统导出文件',
                    'employee1_review':'请员工1核对当前版本文件', 'employee2_review':'请员工2接收并核对当前版本文件',
                    'manager_done':'员工核对完成，等待汇报', 'failure':'取数或导出失败'}.get(kind, kind), stamp()))

    def channel(self, name):
        if name in ('primary', 'employee2'):
            return self.pilot.transport if name == 'primary' else self.pilot.employee2_transport
        return self.pilot.ai_dispatch._channel(name)

    def start_assigned(self, request_id, channels, names):
        with self.store.connect() as db:
            for row in db.execute('SELECT code,detail FROM invoice_demo_runs'):
                if json.loads(row['detail']).get('launch_request') == request_id:
                    return row['code']
        self.start(channels=channels, names=names, launch_request=request_id)
        with self.store.connect() as db:
            row = next((r for r in db.execute('SELECT code,detail FROM invoice_demo_runs') if json.loads(r['detail']).get('launch_request') == request_id), None)
        if row is None:
            raise ValueError('已有发票任务进行中，请结束后重试安排。')
        return row['code']

    def start(self, display_name=None, *, channels=None, names=None, launch_request=None):
        channels = channels or {'manager': 'primary', 'employee1': 'primary', 'employee2': 'employee2'}
        primary, second = self.channel(channels['manager']), self.channel(channels['employee2'])
        first = self.channel(channels['employee1'])
        if not primary or not primary.enabled or not primary.route() or not primary.gateway_listening():
            raise ValueError('主微信未连接，无法发起开发票任务。')
        if not second or not second.enabled or not second.route() or not second.gateway_listening():
            raise ValueError('员工2微信未连接，无法发起开发票任务。')
        if not first or not first.enabled or not first.route() or not first.gateway_listening():
            raise ValueError('员工1微信未连接，无法发起开发票任务。')
        base = Path.home() / 'AppData' / 'Local' / 'LocalPostgreSQL'
        if not (base / 'password.txt').is_file() or not (base / 'pgsql' / 'bin' / 'psql.exe').is_file():
            raise ValueError('PostgreSQL 连接配置不完整，不能启动任务。')
        today = now().date()
        code = 'FP-' + today.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:8].upper()
        detail = {'primary': primary.route(), 'employee2': second.route(),
                  'employee1': first.route(), 'channels': channels, 'staff_names': names or {}, 'launch_request': launch_request,
                  'start': (today - timedelta(days=6)).isoformat(), 'end': today.isoformat()}
        if display_name:
            detail['display_name'] = display_name
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            history = [json.loads(r['detail']) for r in db.execute('SELECT detail FROM invoice_demo_runs')]
            if display_name and any(item.get('display_name') == display_name for item in history):
                raise ValueError(f'任务编号 {display_name} 已使用，不能重复使用。')
            if db.execute("SELECT 1 FROM invoice_demo_runs WHERE state NOT IN ('complete','failed','cancelled')").fetchone():
                return '已有开发票任务进行中，请发送“开发票进度”。'
            if display_name:
                number = int(display_name[len('发票'):])
                previous = next((item for item in history if item.get('display_name') == f'发票{number-1}'), None)
                if number > 1 and not previous:
                    raise ValueError('找不到上一编号的发票任务，无法确定顺延日期。')
                if previous:
                    start = date.fromisoformat(previous['start']) + timedelta(days=7)
                    detail.update(start=start.isoformat(), end=(start + timedelta(days=6)).isoformat())
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)',
                       (code, 'manager_wait', json.dumps(detail, ensure_ascii=False), stamp()))
            self.enqueue(db, code, 'manager')
        return f'开发票测试任务 {display_name or code} 已建立，等待负责人确认。'

    def command(self, text, wechat_role=None):
        original_text = text
        versioned = re.fullmatch(r'(发票[1-9]\d*|FP-\d{8}-[A-F0-9]{8})\s+V([1-9]\d*)\s+(新文件核对无误|收到|核对完成)', text)
        if versioned:
            target, version, verb = versioned.groups()
            text = target + ' ' + verb if target.startswith('发票') else {'新文件核对无误':'员工1确认','收到':'员工2收到','核对完成':'员工2核对完成'}[verb] + ' ' + target
        start_named = re.fullmatch(r'开始\s*(发票[1-9]\d*)', text)
        if text == '开始开发票' or start_named:
            if wechat_role not in (None, 'primary'):
                return '请由负责人启动开发票任务。'
            return self.start(start_named.group(1) if start_named else None)
        if text == '开发票进度':
            with self.store.connect() as db:
                row = db.execute('SELECT code,state FROM invoice_demo_runs ORDER BY rowid DESC LIMIT 1').fetchone()
            return f'{row["code"]}：{row["state"]}' if row else '还没有开发票任务。'
        match = re.fullmatch(r'(负责人确认|员工1确认|员工2收到|员工2核对完成)\s+(FP-\d{8}-[A-F0-9]{8})', text)
        named = re.fullmatch(r'(发票[1-9]\d*)\s*(确认启动|新文件核对无误|核对无误|收到|核对完成)', text)
        named_actions = {'确认启动': '负责人确认', '核对无误': '员工1确认',
                         '新文件核对无误': '员工1确认', '收到': '员工2收到', '核对完成': '员工2核对完成'}
        short = {'发票确认启动': '负责人确认', '发票核对无误': '员工1确认',
                 '发票收到': '员工2收到', '发票核对完成': '员工2核对完成'}
        if not match and not named and text not in short:
            return None
        action, code = match.groups() if match else ((named_actions[named.group(2)], None) if named else (short[text], None))
        steps = {'负责人确认': ('manager_wait', 'export_pending', 'manager', 'employee1_start', 'primary'),
                 '员工1确认': ('employee1_wait', 'employee2_wait', 'employee1_review', 'employee2_review', 'primary'),
                 '员工2收到': ('employee2_wait', 'employee2_received', 'employee2_review', None, 'employee2'),
                 '员工2核对完成': ('employee2_received', 'complete', 'employee2_review', 'manager_done', 'employee2')}
        before, after, prior, notice, role = steps[action]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if code is None:
                active = db.execute("SELECT code,detail FROM invoice_demo_runs WHERE state NOT IN ('complete','failed','cancelled')").fetchall()
                if named:
                    active = [r for r in active if json.loads(r['detail']).get('display_name') == named.group(1)]
                if len(active) != 1:
                    return '未找到唯一匹配的进行中发票任务，请检查任务编号或进度。'
                code = active[0]['code']
            row = db.execute('SELECT * FROM invoice_demo_runs WHERE code=?', (code,)).fetchone()
            if not row or row['state'] != before:
                return '任务不在对应阶段，请发送“开发票进度”。'
            data = json.loads(row['detail'])
            if data.get('correction_reason') and action != '负责人确认' and (not versioned or int(versioned[2]) != data.get('review_revision',1)):
                verb = {'员工1确认':'新文件核对无误','员工2收到':'收到','员工2核对完成':'核对完成'}[action]
                return f"文件已更新，请核对后回复：{data.get('display_name') or code} V{data['review_revision']} {verb}"
            if action == '员工1确认' and not data.get('correction_reason') and data.get('review_revision', 1) > 1 and (not named or named.group(2) != '新文件核对无误'):
                return f"文件已更新，请核对新文件后回复：{data.get('display_name', '发票')} 新文件核对无误"
            actual_role = 'manager' if action == '负责人确认' else 'employee1' if action == '员工1确认' else 'employee2'
            channel_name = data.get('channels', {}).get(actual_role, role)
            channel = self.channel(channel_name)
            expected = data.get('employee1', data['primary']) if actual_role == 'employee1' else data[role]
            if wechat_role != channel_name or not channel or not channel.enabled or channel.route() != expected:
                return '请使用本任务对应的已绑定微信确认。'
            sent = db.execute('SELECT state FROM invoice_demo_notices WHERE code=? AND kind=?', (code, prior)).fetchone()
            if not sent or sent['state'] != 'submitted_unverified':
                return '上一阶段通知尚未提交成功，请先检查发送状态。'
            if action != '负责人确认':
                try:
                    valid = file_hash(data['file']) == data['sha256']
                except (OSError, KeyError):
                    valid = False
                if not valid:
                    return '附件缺失或已变化，已停止交接。'
            db.execute('UPDATE invoice_demo_runs SET state=?, updated=? WHERE code=?', (after, stamp(), code))
            db.execute('INSERT INTO invoice_conversations(code,role,text,at) VALUES (?,?,?,?)', (code,'user', data.get('staff_names',{}).get(actual_role, actual_role) + '：' + original_text, stamp()))
            if notice:
                self.enqueue(db, code, notice)
        return {'负责人确认':'已确认启动，正在通知员工1并准备测试数据。',
                '员工1确认':'已记录核对，准备将同一文件交给员工2。',
                '员工2收到':'已记录收到，请核对完成后再回复。',
                '员工2核对完成':'已记录核对完成，准备向负责人汇报。'}[action]

    def cancel(self, code, expected_state):
        if not self.lock.acquire(blocking=False):
            raise ValueError('正在取数或发送通知，请稍后再关闭。')
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT state FROM invoice_demo_runs WHERE code=?', (code,)).fetchone()
                if not row:
                    raise ValueError('找不到任务。')
                if row['state'] == 'cancelled':
                    return '任务已关闭，后续流程不会继续。'
                if row['state'] == 'complete' or row['state'] != expected_state:
                    raise ValueError('任务状态已变化，请刷新后查看。')
                if db.execute("SELECT 1 FROM invoice_demo_notices WHERE code=? AND state='sending'", (code,)).fetchone():
                    raise ValueError('任务通知正在发送，请稍后再关闭。')
                db.execute("UPDATE invoice_demo_notices SET state='cancelled',detail='任务已关闭，未发送。' WHERE code=? AND state='pending'", (code,))
                db.execute("UPDATE invoice_demo_runs SET state='cancelled',updated=? WHERE code=?", (stamp(),code))
            return '任务已关闭，后续流程不会继续。已发出的消息无法撤回。'
        finally:
            self.lock.release()

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT r.* FROM invoice_demo_runs r JOIN invoice_demo_notices n ON r.code=n.code WHERE r.state='export_pending' AND n.kind='employee1_start' AND n.state='submitted_unverified' ORDER BY r.rowid LIMIT 1").fetchone()
            if row:
                data = json.loads(row['detail'])
                try:
                    from datetime import date
                    path = self.output / row['code'] / '发票样本_模拟日期.xlsx'
                    samples = read_invoice_samples()
                    count, checksum = export_samples(path, date.fromisoformat(data['start']), samples)
                    current = extraction_details(data['start'], data['end'], samples[0])
                    with self.store.connect() as db:
                        previous = None
                        for prior in db.execute('SELECT detail FROM invoice_demo_runs WHERE code<>? ORDER BY rowid DESC', (row['code'],)):
                            old = json.loads(prior['detail'])
                            if old.get('sha256'):
                                previous = old.get('extraction')
                                break
                        data.update(file=str(path), sha256=checksum, count=count, extraction=current,
                                    extraction_comparison=extraction_comparison(current, previous))
                        db.execute('UPDATE invoice_demo_runs SET state=?, detail=?, updated=? WHERE code=? AND state=?',
                                   ('employee1_wait', json.dumps(data,ensure_ascii=False), stamp(), row['code'], 'export_pending'))
                        self.enqueue(db, row['code'], 'employee1_review')
                except Exception:
                    with self.store.connect() as db:
                        db.execute('UPDATE invoice_demo_runs SET state=?,updated=? WHERE code=?', ('failed',stamp(),row['code']))
                        self.enqueue(db, row['code'], 'failure')
            self.notify()
        finally:
            self.lock.release()

    def notify(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT n.id,n.code,n.kind,r.detail FROM invoice_demo_notices n JOIN invoice_demo_runs r ON r.code=n.code WHERE n.state='pending' ORDER BY n.id LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE invoice_demo_notices SET state='sending' WHERE id=?", (row['id'],))
        code, kind, data = row['code'], row['kind'], json.loads(row['detail'])
        display = data.get('display_name')
        interval = f"{data['start']} 至 {data['end']}（含首尾）"
        extraction = data.get('extraction')
        method = (f"取数来源：{extraction['来源']}\n取数方式：{extraction['取数方式']}\n"
                  f"日期规则：{extraction['模拟日期规则']}\n模拟日期范围：{extraction['模拟日期范围']}\n"
                  f"与上次相比：{data.get('extraction_comparison', '无可用记录，无法比较。')}\n"
                  if extraction else '本次未保存完整取数配置，无法提供取数方式或与上次比较。\n')
        messages = {
          'manager': '开发票测试任务即将启动，日期为模拟数据。确认启动请回复：发票确认启动',
          'employee1_start': '今日需要完成开发票测试任务，系统将自动读取样本并生成 Excel。',
          'employee1_review': f"已取出 {data.get('count',0)} 条发票样本。\n{method}日期不代表真实发生时间。核对附件无误后回复：发票核对无误",
          'employee2_review': f'今日发票样本经员工1核实，文件请查收。日期为模拟数据，范围 {interval}。收到后回复：发票收到；核对完成后回复：发票核对完成',
          'manager_done': f'开发票测试任务已完成：员工1和员工2已核对发票样本，模拟日期范围 {interval}。',
          'failure': '开发票测试任务取数或导出失败，未向员工交接，请检查数据库连接。',
        }
        if display:
            messages['manager'] = f'开发票测试任务即将启动，日期为模拟数据。确认启动请回复：{display} 确认启动'
            messages['employee1_review'] = messages['employee1_review'].replace('发票核对无误', f'{display} 核对无误')
            messages['employee2_review'] = messages['employee2_review'].replace('发票收到', f'{display} 收到').replace('发票核对完成', f'{display} 核对完成')
            if data.get('review_revision', 1) > 1:
                messages['employee1_review'] = ('日期已更正，旧文件作废，请使用本次附件。\n' + messages['employee1_review']
                    .replace(f'{display} 核对无误', f'{display} 新文件核对无误'))
        if data.get('correction_reason'):
            label = f"{display or code} V{data['review_revision']}"
            messages['employee1_review'] = f"文件已更正，旧版确认失效。\n更正说明：{data['correction_reason']}\n请核对本次附件后回复：{label} 新文件核对无误"
            messages['employee2_review'] = f"员工1已重新核对新版文件，请使用本次附件。\n收到后回复：{label} 收到\n核对完成后回复：{label} 核对完成"
        try:
            role = 'employee2' if kind == 'employee2_review' else 'employee1' if kind in ('employee1_start', 'employee1_review') else 'manager'
            channel = self.channel(data.get('channels', {}).get(role, 'employee2' if role == 'employee2' else 'primary'))
            expected = data['employee2'] if role == 'employee2' else data.get('employee1',data['primary']) if role == 'employee1' else data['primary']
            media = [Path(data['file'])] if kind in ('employee1_review','employee2_review') else []
            if media and file_hash(media[0]) != data['sha256']:
                raise ValueError('附件已变化')
            state, detail = channel.send_package(f'【开发票测试｜{display or code}】\n' + messages[kind], media, expected_route=expected)
        except Exception:
            state, detail = 'unknown', '发送结果未知或附件校验失败，不自动重发。'
        with self.store.connect() as db:
            db.execute('UPDATE invoice_demo_notices SET state=?,detail=? WHERE id=?', (state,detail,row['id']))
            db.execute('UPDATE invoice_conversations SET text=? WHERE notice_id=?', (f'【开发票测试｜{display or code}】\n' + messages[kind], row['id']))
