"""Versioned uploads; replace artifacts only while workers and senders are idle."""
import base64
import binascii
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import shutil
import zipfile
from contextlib import ExitStack

from zhishi.core.clock import stamp
from zhishi.workflows.ai_dispatch.messages import stage_message


MAX_FILE = 20 * 1024 * 1024


class Corrections:
    def __init__(self, pilot, root):
        self.pilot = pilot
        self.output = Path(root) / 'outputs' / 'corrections'
        with pilot.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS task_file_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, slot TEXT NOT NULL,
                name TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, reason TEXT NOT NULL,
                created TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL)''')

    def history(self, ident):
        with self.pilot.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,slot,name,reason,created FROM task_file_versions WHERE task_id=? ORDER BY id DESC', (ident,))]

    def asset(self, ident):
        with self.pilot.connect() as db:
            row = db.execute('SELECT * FROM task_file_versions WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ValueError('找不到文件版本。')
        path = Path(row['path']).resolve()
        if not path.is_relative_to(self.output.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('文件版本校验失败。')
        return path, row['name']

    @staticmethod
    def validate_file(content, suffix):
        if suffix == '.xlsx':
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    if len(archive.infolist()) > 10000 or sum(f.file_size for f in archive.infolist()) > 150 * 1024 * 1024:
                        raise ValueError('Excel 文件解压后过大。')
                    if not {'[Content_Types].xml', 'xl/workbook.xml'} <= set(archive.namelist()):
                        raise ValueError('请上传有效的 Excel 工作簿。')
                    if archive.testzip():
                        raise ValueError('Excel 文件损坏。')
            except (zipfile.BadZipFile, RuntimeError):
                raise ValueError('请上传有效的 .xlsx 文件。') from None
        elif suffix == '.csv':
            try:
                rows = csv.reader(io.StringIO(content.decode('utf-8-sig')))
                if not next(rows, None):
                    raise ValueError('CSV 内容为空。')
            except UnicodeError:
                raise ValueError('请使用 UTF-8 编码的 CSV。') from None

    def replace(self, data):
        ident, slot, request = data.get('id'), data.get('slot'), data.get('request_id')
        name, reason = data.get('name'), data.get('reason', '')
        if not isinstance(request, str) or not 8 <= len(request) <= 100:
            raise ValueError('缺少有效请求编号。')
        if not isinstance(ident, str) or not re.fullmatch(r'(ai:AIW|invoice:FP)-\d{8}-[A-F0-9]{8}', ident):
            raise ValueError('请选择商品核查或发票任务。')
        if not isinstance(name, str) or not name or len(name) > 160 or any(c in name for c in '/\\:\r\n\x00') or name in ('.','..'):
            raise ValueError('文件名不正确。')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError('请填写 1–1000 字的更正说明。')
        try:
            content = base64.b64decode(data.get('content', ''), validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise ValueError('上传内容无法读取。') from None
        if not 0 < len(content) <= MAX_FILE:
            raise ValueError('请选择不超过 20 MB 的文件。')
        suffix = Path(name).suffix.lower()
        required = '.xlsx' if ident.startswith('invoice:') or slot in ('employee1', 'employee2') else '.csv'
        if suffix != required or slot not in ('employee1', 'employee2', 'result'):
            raise ValueError('文件类型与替换位置不匹配。')
        self.validate_file(content, suffix)
        checksum = hashlib.sha256(content).hexdigest()
        service = self.pilot.invoice_demo if ident.startswith('invoice:') else self.pilot.ai_dispatch
        locks = [service.lock] if ident.startswith('invoice:') else [service.worker_lock, service.send_lock, service.lock]
        with ExitStack() as stack:
            for lock in locks:
                if not lock.acquire(blocking=False):
                    raise ValueError('任务正在处理或发送文件，请稍后重试。')
                stack.callback(lock.release)
            with self.pilot.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                saved = db.execute('SELECT * FROM task_file_versions WHERE request_id=?', (request,)).fetchone()
                if saved:
                    if saved['task_id'] != ident or saved['sha256'] != checksum or saved['slot'] != slot or saved['reason'] != reason.strip() or saved['name'] != name:
                        raise ValueError('请求编号已用于其他文件。')
                    return {'reply': '本次文件更正已保存。'}
                code = ident.split(':',1)[1]
                folder = self.output / code / secrets.token_hex(12)
                folder.mkdir(parents=True)
                path = folder / ('replacement' + suffix)
                path.write_bytes(content)
                try:
                    if ident.startswith('invoice:'):
                        if slot != 'result':
                            raise ValueError('发票任务仅支持替换导出文件。')
                        row = db.execute('SELECT * FROM invoice_demo_runs WHERE code=?', (code,)).fetchone()
                        if not row or row['state'] in ('cancelled','export_pending','manager_wait') or data.get('state') != row['state'] or data.get('updated_at') != row['updated']:
                            raise ValueError('任务已变化或尚未生成文件，请刷新后重试。')
                        detail = json.loads(row['detail'])
                        if data.get('file_revision', 1) != detail.get('review_revision', 1):
                            raise ValueError('文件已有新版本，请刷新后再修改。')
                        if not detail.get('file'):
                            raise ValueError('本任务还没有可替换的文件。')
                        # Store under the existing download boundary without overwriting any old file.
                        target = service.output / code / ('更正_' + folder.name + '.xlsx')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                        detail.setdefault('file_history', []).append({k: detail.get(k) for k in ('file','sha256','review_revision','count','extraction')})
                        revision = detail.get('review_revision', 1) + 1
                        detail.update(file=str(target), sha256=checksum, review_revision=revision, correction_reason=reason.strip())
                        detail.pop('extraction', None)
                        detail.pop('extraction_comparison', None)
                        detail.pop('count', None)
                        for notice in db.execute('SELECT id,kind FROM invoice_demo_notices WHERE code=?', (code,)).fetchall():
                            if not notice['kind'].startswith('old-'):
                                db.execute("UPDATE invoice_demo_notices SET kind=?,state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END WHERE id=?", ('old-' + str(revision) + '-' + notice['kind'], notice['id']))
                        db.execute("UPDATE invoice_demo_runs SET detail=?,state='employee1_wait',updated=? WHERE code=?", (json.dumps(detail, ensure_ascii=False), stamp(), code))
                        db.execute('INSERT INTO invoice_conversations(code,role,text,at) VALUES (?,?,?,?)', (code,'user','上传新版文件：' + name + '。更正说明：' + reason.strip(),stamp()))
                        service.enqueue(db, code, 'employee1_review')
                    else:
                        run = service.row(code)
                        if run['state'] == 'cancelled' or data.get('revision') != run['revision']:
                            raise ValueError('任务版本已变化或任务已关闭，请刷新后重试。')
                        if slot == 'result':
                            if not run['evidence'].get('employee2'):
                                raise ValueError('本任务尚未生成核查结果。')
                            target = service.skills.demo.output / code / ('更正_' + folder.name + '.csv')
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(path, target)
                            run['evidence']['employee2'].update(result=str(target),result_sha256=checksum)
                            service.skills.verify(run)
                            for key in ('employee2_confirmed', 'final'):
                                run['evidence'].pop(key, None)
                            new_state = 'employee2_review'
                        else:
                            source = Path(run['task']['input_directory'])
                            for role, directory in [('employee1','员工1'), ('employee2','员工2')]:
                                destination = folder / directory
                                destination.mkdir()
                                if role == slot:
                                    shutil.copyfile(path, destination / '输入.xlsx')
                                else:
                                    for old in (source / directory).glob('*.xlsx'):
                                        shutil.copyfile(old, destination / old.name)
                            run['task']['input_directory'] = str(folder)
                            run['evidence'] = {}
                            new_state = 'employee1_work'
                        db.execute('UPDATE ai_dispatch_runs SET task_json=?,error=? WHERE code=?', (json.dumps(run['task'],ensure_ascii=False),'',code))
                        db.execute("UPDATE ai_dispatch_inbox SET state='cancelled',result='文件已更正，旧回复失效。' WHERE code=? AND state IN ('pending','failed')", (code,))
                        service._event(db, code, 'file_replaced', name + '：' + reason.strip())
                        text = '文件已更正，旧版确认失效。更正说明：' + reason.strip() + '\n' + (stage_message(new_state,run['task'],run['evidence']) if new_state == 'employee2_review' else '系统将重新检查输入文件，再请员工1核对。')
                        service._move(db, run, new_state, text, 'employee2' if slot == 'result' else 'employee1')
                    db.execute('INSERT INTO task_file_versions(task_id,slot,name,path,sha256,reason,created,request_id) VALUES (?,?,?,?,?,?,?,?)',
                               (ident, slot, name, str(path), checksum, reason.strip(), stamp(), request))
                except Exception:
                    # Only this new, verified task directory is removed on rollback.
                    shutil.rmtree(folder)
                    raise
        return {'reply': '新版文件已保存，已退回重新核对，旧版记录保留。'}
