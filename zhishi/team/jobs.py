"""Persistent fixed-skill jobs with renewable leases and fenced result publication."""
from datetime import timedelta
import csv
import hashlib
import io
import json
from pathlib import Path
import secrets
import subprocess
import threading
import zipfile

from zhishi.config import ROOT, excel_node
from zhishi.core.clock import now, stamp
from zhishi.core.observability import log_failure
from zhishi.workflows.excel import ExcelExecutor
from .auth import actor
from .errors import TeamError, require
from .service import encoded, ident
import logging

SKILLS = ('excel.merge.v1', 'excel.summary.v1', 'csv.inspect.v1')


class SkillRunner:
    def __init__(self, service, root=ROOT):
        self.service, self.root = service, Path(root)

    def processor(self, skill):
        return self.root / 'workflows' / ('excel_worker.mjs' if skill == 'excel.merge.v1' else 'department_summary.mjs') if skill != 'csv.inspect.v1' else Path(__file__)

    def checksum(self, skill):
        return hashlib.sha256(self.processor(skill).read_bytes()).hexdigest()

    def execute(self, job):
        snapshot = json.loads(job['inputs_json'])
        require(self.checksum(job['skill']) == snapshot['processor_sha256'], '排队后技能代码已变化，请重新发起。', 409)
        inputs = snapshot['artifacts']
        for item in inputs:
            self.service.file(item)
            if Path(item['name']).suffix.lower() == '.xlsx':
                with zipfile.ZipFile(self.service.file(item)) as archive:
                    entries = archive.infolist()
                    require(len(entries) <= 10000 and sum(e.file_size for e in entries) <= 100_000_000,
                            '表格解压后的内容超过执行上限。', 400)
                    require(all(not e.flag_bits & 1 and '..' not in Path(e.filename).parts and not e.filename.startswith(('/', '\\'))
                                for e in entries), '表格包含加密或越界条目。', 400)
        folder = self.service.artifact_root / '.jobs' / job['id'] / job['lease_token']
        folder.mkdir(parents=True, exist_ok=True)
        if job['skill'] == 'csv.inspect.v1':
            require(all(Path(item['name']).suffix.lower() == '.csv' for item in inputs), 'CSV检查只接受CSV输入。', 400)
            reports = []
            for item in inputs:
                content = self.service.read_file(item).decode('utf-8-sig')
                reader = csv.reader(io.StringIO(content))
                header = next(reader, [])
                require(header and len(header) == len(set(header)), 'CSV表头为空或重复。', 400)
                rows, invalid = 0, []
                for number, row in enumerate(reader, 2):
                    rows += 1
                    if len(row) != len(header):
                        invalid.append(number)
                reports.append({'name': item['name'], 'rows': rows, 'columns': len(header), 'invalid_rows': invalid})
            report = {'skill': job['skill'], 'model_used': False, 'files': reports,
                      'passed': all(not r['invalid_rows'] for r in reports)}
            output = folder / 'CSV检查.json'
            output.write_text(encoded(report), encoding='utf-8')
            return report, [(output.name, output)]
        require(excel_node().is_file(), 'Excel运行环境不可用，请配置EXCEL_WORKER_NODE。', 409)
        if job['skill'] == 'excel.merge.v1':
            require(all(Path(item['name']).suffix.lower() == '.xlsx' for item in inputs), '整合技能只接受XLSX文件。', 400)
            plan = {'title': '项目表格整合', 'inputs': [{'name': f'{index}-{item["name"]}',
                    'path': str(self.service.file(item)), 'sha256': item['sha256']} for index, item in enumerate(inputs)],
                    'processor_sha256': snapshot['processor_sha256'], 'document_text': '整合项目授权的输入文件，成果需要人工验收。',
                    'skill_text': '固定Excel整合技能。', 'recipients_snapshot': []}
            executor = ExcelExecutor(self.root, folder)
            report, assets = executor.execute({'code': job['id'], 'version': job['task_version'], 'plan_json': encoded(plan)})
            # The original executor also produces a pilot-only explanation; it is
            # intentionally not published as a real team deliverable.
            return report, [(('整合结果.xlsx' if a['name'].endswith('.xlsx') else a['name']), folder / a['relative_path'])
                            for a in assets if a['name'] != '完成说明.md']
        require(job['skill'] == 'excel.summary.v1', '技能未登记。', 400)
        books = [item for item in inputs if Path(item['name']).suffix.lower() == '.xlsx']
        checks = [item for item in inputs if item['name'] == '检查结果.json']
        require(len(books) == len(checks) == 1, '部门汇总需要一份已交接的整合表和检查结果.json。', 400)
        spec = {'input': str(self.service.file(books[0])), 'check': str(self.service.file(checks[0])),
                'output': str(folder), 'code': job['task_id'], 'version': job['task_version']}
        spec_file = folder / 'spec.json'
        spec_file.write_text(encoded(spec), encoding='utf-8')
        result = subprocess.run([str(excel_node()), str(self.processor(job['skill'])), str(spec_file)],
                                cwd=self.root, capture_output=True, timeout=180,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(result.returncode == 0, '固定部门汇总技能执行失败，请检查输入格式和上游核对结果。', 409)
        report = json.loads((folder / '部门汇总.json').read_text('utf-8'))
        report.update(demo=False, human_review_required=True)
        (folder / '部门汇总.json').write_text(encoded(report), encoding='utf-8')
        (folder / '部门汇总.md').write_text('# 部门汇总\n\n' + '\n'.join(
            f'- {g["department"]}：{g["rows"]}条，金额合计{g["total_amount"]}' for g in report['departments']) +
            f'\n\n总记录数：{report["rows"]}。合计：{report["total_amount"]}。请人工核对本版成果。\n', encoding='utf-8')
        return report, [(name, folder / name) for name in ('部门汇总.json', '部门汇总.md')]


class JobService:
    def __init__(self, service, runner=None):
        self.service, self.store = service, service.store
        self.runner = runner or SkillRunner(service)

    def enqueue(self, user_id, task_id, data):
        def operation(db, user):
            task = self.service.task(db, task_id, user)
            self.service.version(task, data)
            self.service.project(db, task['project_id'], user, write=True)
            require(task['assignee_id'] == user_id and task['state'] == 'in_progress', '只有当前执行人可以运行固定技能。')
            skill = data.get('skill')
            require(skill in SKILLS, '技能未登记，不能执行自定义脚本。', 400)
            ids = data.get('artifact_ids')
            require(isinstance(ids, list) and 1 <= len(ids) <= 20 and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids),
                    '需要1至20份不重复的输入文件。', 400)
            require(not db.execute("SELECT 1 FROM team_jobs WHERE task_id=? AND task_version=? AND state IN ('queued','running')", (task_id, task['version'])).fetchone(),
                    '本版已有排队或正在执行的技能。', 409)
            inputs = []
            for artifact_id in ids:
                item = db.execute('SELECT * FROM team_artifacts WHERE id=?', (artifact_id,)).fetchone()
                shared = db.execute('SELECT sha256 FROM team_inputs WHERE task_id=? AND artifact_id=?', (task_id, artifact_id)).fetchone()
                require(item is not None and ((item['task_id'] == task_id and (item['kind'] == 'input' or item['result_round'] == task['result_round']))
                                              or (shared and shared['sha256'] == item['sha256'])), '输入文件未获准用于此任务。')
                self.service.file(item)
                inputs.append(dict(item))
            job_id = ident('j_')
            snapshot = {'artifacts': inputs, 'processor_sha256': self.runner.checksum(skill)}
            db.execute('INSERT INTO team_jobs(id,task_id,requested_by,task_version,skill,inputs_json,state,created_at,updated_at) '
                       "VALUES (?,?,?,?,?,?,'queued',?,?)", (job_id, task_id, user_id, task['version'], skill, encoded(snapshot), stamp(), stamp()))
            self.store.audit(db, user_id, 'queue_skill', 'job', job_id, {'skill': skill, 'task_version': task['version']})
            return {'id': job_id, 'state': 'queued', 'task_version': task['version']}
        return self.service.mutate(user_id, data.get('request_id'), 'job:' + task_id, data, operation)

    def claim(self):
        with self.store.connect(write=True) as db:
            expired = db.execute("SELECT * FROM team_jobs WHERE state='running' AND lease_until<=?", (stamp(),)).fetchall()
            for job in expired:
                state = 'failed' if job['attempts'] >= 3 else 'queued'
                db.execute('UPDATE team_jobs SET state=?,error=?,lease_token=NULL,updated_at=? WHERE id=?',
                           (state, '执行租约已过期。' if state == 'failed' else '', stamp(), job['id']))
                if state == 'failed':
                    self.service.notify(db, job['requested_by'], '技能多次中断，需检查后重新发起。')
            rows = db.execute("SELECT j.*,t.version AS current_version,t.state AS task_state,u.active "
                              'FROM team_jobs j JOIN team_tasks t ON t.id=j.task_id JOIN team_users u ON u.id=j.requested_by '
                              "WHERE j.state='queued' ORDER BY j.created_at LIMIT 50").fetchall()
            for row in rows:
                if row['current_version'] != row['task_version'] or row['task_state'] != 'in_progress' or not row['active']:
                    db.execute("UPDATE team_jobs SET state='stale',error='任务版本、阶段或用户状态已变化。',updated_at=? WHERE id=?", (stamp(), row['id']))
                    continue
                token = secrets.token_hex(24)
                db.execute("UPDATE team_jobs SET state='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                           (token, stamp(now() + timedelta(seconds=45)), stamp(), row['id']))
                return dict(db.execute('SELECT * FROM team_jobs WHERE id=?', (row['id'],)).fetchone())

    def renew(self, job):
        with self.store.connect(write=True) as db:
            result = db.execute("UPDATE team_jobs SET lease_until=?,updated_at=? WHERE id=? AND state='running' AND lease_token=? AND lease_until>?",
                                (stamp(now() + timedelta(seconds=45)), stamp(), job['id'], job['lease_token'], stamp()))
            return result.rowcount == 1

    def finish(self, job, result=None, error=None):
        created = []
        try:
            with self.store.connect(write=True) as db:
                current = db.execute('SELECT * FROM team_jobs WHERE id=?', (job['id'],)).fetchone()
                if not current or current['state'] != 'running' or current['lease_token'] != job['lease_token'] or current['lease_until'] <= stamp():
                    return False
                task = dict(db.execute('SELECT * FROM team_tasks WHERE id=?', (job['task_id'],)).fetchone())
                user = db.execute('SELECT * FROM team_users WHERE id=?', (job['requested_by'],)).fetchone()
                member = db.execute('SELECT role FROM team_members WHERE project_id=? AND user_id=?', (task['project_id'], job['requested_by'])).fetchone()
                if task['version'] != job['task_version'] or task['state'] != 'in_progress' or task['assignee_id'] != job['requested_by'] or not user['active'] or not member or member['role'] == 'viewer':
                    db.execute("UPDATE team_jobs SET state='stale',error='执行完成时任务版本或授权已变化。',updated_at=? WHERE id=?", (stamp(), job['id']))
                    return False
                if error:
                    db.execute("UPDATE team_jobs SET state='failed',error=?,updated_at=? WHERE id=?", (error, stamp(), job['id']))
                    self.store.audit(db, job['requested_by'], 'skill_failed', 'job', job['id'])
                    self.service.notify(db, job['requested_by'], '技能执行失败：' + error, task)
                    return True
                report, files = result
                for snapshot in json.loads(job['inputs_json'])['artifacts']:
                    self.service.file(snapshot)
                require(files and len(files) <= 20, '技能没有返回有效成果。', 409)
                task = self.service.bump(db, task, job['requested_by'], task['state'], 'skill_completed')
                artifacts = []
                for name, path in files:
                    require(Path(name).name == name and len(name) <= 120, '技能返回了无效文件名。', 409)
                    path = Path(path).resolve()
                    require(path.is_relative_to((self.service.artifact_root / '.jobs').resolve()), '技能成果超出工作目录。', 409)
                    content = path.read_bytes()
                    require(0 < len(content) <= 10_000_000, '技能成果过大或为空。', 409)
                    key, artifact_id = secrets.token_hex(24), ident('a_')
                    destination = self.service.artifact_root / key
                    with destination.open('xb') as stream:
                        stream.write(content)
                    created.append(destination)
                    db.execute('INSERT INTO team_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (artifact_id, task['id'], name, key, hashlib.sha256(content).hexdigest(), len(content),
                                'output', job['requested_by'], task['version'], task['result_round'], stamp()))
                    artifacts.append(self.service.artifact_info(db.execute('SELECT * FROM team_artifacts WHERE id=?', (artifact_id,)).fetchone()))
                db.execute("UPDATE team_jobs SET state='completed',result_json=?,updated_at=? WHERE id=?", (encoded({'report': report, 'artifacts': artifacts}), stamp(), job['id']))
                self.service.notify(db, job['requested_by'], '技能成果已生成，请核对后提交验收：' + task['title'], task)
                return True
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def tick(self):
        job = self.claim()
        if not job:
            return
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(10):
                try:
                    if not self.renew(job):
                        return
                except Exception:
                    log_failure(logging.getLogger('zhishi.team.jobs'), 'job_heartbeat_failed')
                    return
        thread = threading.Thread(target=heartbeat, daemon=True, name='team-job-heartbeat')
        thread.start()
        try:
            result = self.runner.execute(job)
            self.finish(job, result=result)
        except Exception as exc:
            log_failure(logging.getLogger('zhishi.team.jobs'), 'fixed_skill_failed')
            message = str(exc) if isinstance(exc, TeamError) else '固定技能执行失败，请检查输入和运行环境。'
            self.finish(job, error=message)
        finally:
            stop.set()
            thread.join(2)
