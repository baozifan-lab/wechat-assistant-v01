"""Local single-account role simulation for the specified product investigation."""
import csv
import hashlib
import json
import io
import os
import re
import subprocess
import threading
from pathlib import Path

import openpyxl

from zhishi.core.clock import stamp

FOLDER = Path('D:/desktop/agent实机演示')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def only_xlsx(folder):
    files = [p for p in folder.glob('*.xlsx') if not p.name.startswith('~$')]
    if len(files) != 1:
        raise ValueError(f'{folder.name} 文件夹需恰有一份 Excel，当前有 {len(files)} 份')
    return files[0]


def read_rows(path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            sheet.reset_dimensions()
            headers = None
            for rowno, values in enumerate(sheet.iter_rows(values_only=True), 1):
                if headers is None:
                    if '商品编码' in values and '商品名称' in values:
                        headers = {str(x).strip(): i for i, x in enumerate(values) if x is not None}
                    continue
                def val(name):
                    i = headers.get(name)
                    return values[i] if i is not None and i < len(values) else None
                code = val('商品编码')
                if code is not None and str(code).strip():
                    if isinstance(code, float) and code.is_integer():
                        code = int(code)
                    yield {'code': str(code).strip(), 'name': str(val('商品名称') or '').strip(),
                           'status': str(val('现商品状态') or '').strip(),
                           'sheet': sheet.title, 'row': rowno}
            if headers is None:
                raise ValueError(f'{path.name} / {sheet.title} 未找到商品编码与商品名称表头')
    finally:
        workbook.close()


def read_database_products():
    """Read the explicitly selected demo table in one read-only snapshot."""
    base = Path.home() / 'AppData' / 'Local' / 'LocalPostgreSQL'
    executable = base / 'pgsql' / 'bin' / 'psql.exe'
    env = os.environ.copy()
    env['PGPASSWORD'] = (base / 'password.txt').read_text(encoding='utf-8').strip()
    env['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=15000'
    env['PGCLIENTENCODING'] = 'UTF8'
    query = 'SELECT product_id, sku, product_name FROM retail_demo.products ORDER BY product_id'
    try:
        result = subprocess.run([str(executable), '-X', '-h', '127.0.0.1', '-p', '5432',
                                 '-U', 'postgres', '-d', 'localdb', '-v', 'ON_ERROR_STOP=1',
                                 '--csv', '-c', query], env=env, capture_output=True,
                                encoding='utf-8', timeout=25, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('数据库商品表读取失败，请检查数据库连接和权限。') from exc
    rows = []
    for record in csv.DictReader(io.StringIO(result.stdout)):
        code = (record.get('sku') or '').strip()
        if code:
            rows.append({'code':code, 'name':(record.get('product_name') or '').strip(),
                         'sheet':'retail_demo.products', 'row':record['product_id']})
    if not rows:
        raise ValueError('数据库商品表没有可用的 SKU。')
    return rows, hashlib.sha256(result.stdout.encode('utf-8')).hexdigest()


class ProductDemo:
    def __init__(self, pilot, root):
        self.pilot = pilot
        self.store, self.transport = pilot.store, pilot.transport
        self.output = root / 'outputs' / 'product-demo'
        self.worker_lock = threading.Lock()
        with self.store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS product_demo_runs
                (code TEXT PRIMARY KEY, state TEXT NOT NULL, route TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS product_demo_notices
                (id INTEGER PRIMARY KEY, code TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '', UNIQUE(code,kind));''')
            db.execute("UPDATE product_demo_notices SET state='unknown',detail='重启前发送结果未知' WHERE state='sending'")

    def enqueue(self, db, code, kind):
        db.execute('INSERT OR IGNORE INTO product_demo_notices(code,kind) VALUES (?,?)', (code, kind))

    def start(self):
        route = self.transport.route() if self.transport and self.transport.enabled else None
        if not route:
            raise ValueError('微信未绑定。')
        only_xlsx(FOLDER / '员工1'); only_xlsx(FOLDER / '员工2')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT code FROM product_demo_runs WHERE state NOT IN ('complete','failed') LIMIT 1").fetchone()
            if active:
                return f'商品排查 {active["code"]} 尚未结束，请发送 商品排查进度。'
            t2 = self.pilot.employee2_transport
            second = t2.route() if t2 and t2.enabled else None
            if not second or not t2.gateway_listening():
                raise ValueError('员工2微信未连接，暂不能发起双微信演示。')
            day = stamp()[:10].replace('-', '')
            number = db.execute('SELECT count(*) FROM product_demo_runs WHERE code LIKE ?', (f'SP-{day}-%',)).fetchone()[0] + 1
            code = f'SP-{day}-{number:02d}'
            db.execute('INSERT INTO product_demo_runs VALUES (?,?,?,?,?)', (code, 'manager_wait', json.dumps(route), json.dumps({'employee2_route':second, 'source':'database_products'}), stamp()))
            self.enqueue(db, code, 'manager')
        return f'已建立商品排查演示 {code}，等待向部门负责人提交确认消息。'

    def command(self, text, wechat_role=None):
        if text == '开始商品排查':
            if wechat_role == 'employee2':
                return '请由负责人微信启动商品排查。'
            return self.start()
        if text == '商品排查进度':
            with self.store.connect() as db:
                row = db.execute('SELECT code,state FROM product_demo_runs ORDER BY rowid DESC LIMIT 1').fetchone()
            return f'{row["code"]}：{row["state"]}' if row else '尚未建立商品排查演示。'
        match = re.fullmatch(r'(负责人确认|员工1已导出|员工2确认) (SP-\d{8}-\d{2,})', text)
        if not match:
            return None
        action, code = match.groups()
        before, after, notice = {'负责人确认': ('manager_wait','employee1_wait','employee1'),
            '员工1已导出': ('employee1_wait','processing',None), '员工2确认': ('employee2_wait','complete','manager_done')}[action]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM product_demo_runs WHERE code=?', (code,)).fetchone()
            if not row or row['state'] != before:
                return f'{code} 当前不在该确认阶段，请发送 商品排查进度。'
            snapshot = json.loads(row['detail']).get('employee2_route')
            expected_role = 'employee2' if action == '员工2确认' and snapshot else 'primary'
            if wechat_role is not None and wechat_role != expected_role:
                return '这个确认需要由对应角色的微信账号发送。'
            if wechat_role == 'employee2' and (not self.pilot.employee2_transport or self.pilot.employee2_transport.route() != snapshot):
                return '员工2微信绑定已变化，本次确认未接受。'
            previous = {'manager_wait':'manager','employee1_wait':'employee1','employee2_wait':'employee2'}[before]
            sent = db.execute('SELECT state FROM product_demo_notices WHERE code=? AND kind=?', (code, previous)).fetchone()
            if not sent or sent['state'] != 'submitted_unverified':
                return '对应通知尚未确认提交，请先检查发送状态。'
            if before == 'employee2_wait':
                detail = json.loads(row['detail'])
                if not Path(detail['result']).is_file() or digest(Path(detail['result'])) != detail['result_sha256']:
                    return '排查结果文件已变化，不能确认，请重新核查。'
            db.execute('UPDATE product_demo_runs SET state=?,updated=? WHERE code=?', (after, stamp(), code))
            if notice:
                self.enqueue(db, code, notice)
        return {'manager_wait':'已记录负责人确认，将派给员工1。', 'employee1_wait':'已记录员工1的回复，将检查现有导出文件并开始排查。', 'employee2_wait':'已记录员工2确认，将向负责人汇报。'}[before]

    def process(self, code, source='file', *, folder=None):
        folder = Path(folder) if folder is not None else FOLDER
        source1, source2 = only_xlsx(folder/'员工1'), only_xlsx(folder/'员工2')
        h1, h2 = digest(source1), digest(source2)
        if source == 'database_products':
            processing, db_hash = read_database_products()
        else:
            processing, db_hash = list(read_rows(source1)), None
        invalid = [r for r in read_rows(source2) if r['status'] == '作废']
        if not processing or not invalid:
            raise ValueError('加工单或作废记录为空，请检查表格。')
        lookup = {}
        for row in processing:
            lookup.setdefault(row['code'], []).append(row)
        matches = [r for r in invalid if r['code'] in lookup]
        if digest(source1) != h1 or digest(source2) != h2:
            raise ValueError('源文件读取期间发生变化，已停止。')
        output = self.output / code
        output.mkdir(parents=True, exist_ok=True)
        result = output / '商品交集核查.csv'
        def safe(value):
            value = str(value)
            return "'" + value if value.startswith(('=', '+', '-', '@')) else value
        with result.open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['比对结果','商品编码','作废表商品名称','作废表工作表','作废表行号','数据库商品名称' if db_hash else '加工单商品名称','数据库表和商品ID' if db_hash else '加工单来源行'])
            for r in (invalid if db_hash else matches):
                found = lookup.get(r['code'], [])
                writer.writerow(['匹配' if found else '未匹配', safe(r['code']), safe(r['name']), safe(r['sheet']), r['row'],
                                 safe('；'.join(sorted({x['name'] for x in found}))),
                                 safe('；'.join(f"{x['sheet']}!{x['row']}" for x in found))])
        return {'source':source,'database_source':'localdb.retail_demo.products' if db_hash else None,
                'database_snapshot_sha256':db_hash,'processing_file':str(source1),'processing_sha256':h1,'invalid_file':str(source2),
                'invalid_sha256':h2,'processing_rows':len(processing),'invalid_rows':len(invalid),
                'matched_rows':len(matches),'matched_codes':len({r['code'] for r in matches}),
                'result':str(result),'result_sha256':digest(result)}

    def tick(self):
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM product_demo_runs WHERE state='processing' ORDER BY rowid LIMIT 1").fetchone()
            if row:
                code = row['code']
                try:
                    detail = self.process(code, json.loads(row['detail']).get('source','file'))
                    detail['employee2_route'] = json.loads(row['detail']).get('employee2_route')
                    with self.store.connect() as db:
                        db.execute('UPDATE product_demo_runs SET state=?,detail=?,updated=? WHERE code=? AND state=?', ('employee2_wait',json.dumps(detail,ensure_ascii=False),stamp(),code,'processing'))
                        self.enqueue(db,code,'employee2')
                except Exception as exc:
                    with self.store.connect() as db:
                        db.execute('UPDATE product_demo_runs SET state=?,detail=?,updated=? WHERE code=?', ('failed',json.dumps({'error':str(exc)},ensure_ascii=False),stamp(),code))
                        self.enqueue(db,code,'failure')
            self.notify()
        finally:
            self.worker_lock.release()

    def notify(self):
        with self.store.connect() as db:
            row = db.execute("SELECT n.id,n.code,n.kind,r.route,r.detail FROM product_demo_notices n JOIN product_demo_runs r ON r.code=n.code WHERE n.state='pending' ORDER BY n.id LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE product_demo_notices SET state='sending' WHERE id=? AND state='pending'",(row['id'],))
        kind, d, CODE = row['kind'], json.loads(row['detail']), row['code']
        intro = f'【商品排查演示｜{CODE}】\n'
        database = d.get('source') == 'database_products'
        texts = {
          'manager': '【部门负责人｜任务确认】\n部分拟作废商品因存在于商品加工单中无法作废。需要导出加工单，筛选同时出现在加工单和作废清单中的商品并核查。请确认启动，回复：负责人确认 '+CODE,
          'employee1': '【员工1｜导出加工单】\n请导出商品加工单到本机演示的员工1文件夹。现有文件可供本次模拟使用；确认导出完成后回复：员工1已导出 '+CODE,
          'employee2': f"【员工2｜排查结果待确认】\n已检测到员工1文件，自动按商品编码比对加工单与现商品状态为作废的记录。发现 {d.get('matched_codes',0)} 个商品编码、{d.get('matched_rows',0)} 条作废表记录。请核对附件来源行与商品名称；这只是交集，不代表已完成实际作废。确认无误回复：员工2确认 {CODE}",
          'manager_done': f"【部门负责人｜完成汇报】\n员工1已确认导出，系统完成交集排查，员工2已确认结果。匹配 {d.get('matched_codes',0)} 个商品编码、{d.get('matched_rows',0)} 条作废表记录。此为排查完成，未执行商品作废。",
          'failure': f"【任务受阻】\n自动排查未完成：{d.get('error','未知错误')}。尚未向负责人汇报完成。"}
        if database:
            texts.update({
              'manager': '【部门负责人｜数据库接入测试确认】\n本轮测试员工2从数据库读取商品表 retail_demo.products，将 SKU 与作废清单商品编码比对。商品表不是加工单，结果只能验证取数与比对，不能判断加工单是否阻止作废。员工1仍先确认现有导出文件。请回复：负责人确认 '+CODE,
              'employee1': '【员工1｜确认导出】\n请确认员工1文件夹中的加工单导出文件可用。此轮数据库接入测试由员工2读取数据库商品表进行比对，不用该文件作为比对来源。确认后回复：员工1已导出 '+CODE,
              'employee2': f"【员工2｜数据库取数结果待确认】\n已从数据库读取 retail_demo.products 商品表 {d.get('processing_rows',0)} 条，以 SKU 精确比对作废清单商品编码，匹配 {d.get('matched_codes',0)} 个编码、{d.get('matched_rows',0)} 条作废记录。请查看附件。此表不是加工单，不能据此判断商品能否作废。核对无误回复：员工2确认 {CODE}",
              'manager_done': f"【部门负责人｜数据库接入测试完成】\n员工2已确认：数据库商品表读取 {d.get('processing_rows',0)} 条，与作废清单精确匹配 {d.get('matched_codes',0)} 个 SKU、{d.get('matched_rows',0)} 条作废记录。商品表不是加工单，本次未判断作废阻碍，也未执行商品作废。"})
        media = [Path(d['result'])] if kind in ('employee2','manager_done') else []
        try:
            second_route = d.get('employee2_route') if kind == 'employee2' else None
            channel = self.pilot.employee2_transport if second_route else self.transport
            expected_route = second_route or json.loads(row['route'])
            state, detail = channel.send_package(intro+texts[kind],media,expected_route=expected_route)
        except Exception:
            state,detail='unknown','发送结果未知，不自动重发。'
        with self.store.connect() as db:
            db.execute('UPDATE product_demo_notices SET state=?,detail=? WHERE id=?',(state,detail,row['id']))
