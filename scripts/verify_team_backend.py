"""Exercise the complete three-user HTTP workflow with real registered Excel skills."""
import argparse
import base64
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.team.application import TeamApplication
from zhishi.team.runtime import create_team_server
from zhishi.core.clock import stamp


def verify():
    with tempfile.TemporaryDirectory(prefix='zhishi-team-e2e-') as temporary:
        root = Path(temporary)
        app = TeamApplication(root / 'team.sqlite3')
        password = secrets.token_urlsafe(24)
        app.auth.bootstrap('admin', password)
        server = create_team_server(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base, tokens = f'http://127.0.0.1:{server.server_port}', {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, data=None, user='alice', expected=200):
            headers = {'Content-Type': 'application/json'}
            if user in tokens:
                headers['Authorization'] = 'Bearer ' + tokens[user]
            if data is not None:
                data = {'request_id': str(uuid.uuid4()), **data}
            req = urllib.request.Request(base + '/api/team' + path, headers=headers,
                data=None if data is None else json.dumps(data).encode())
            try:
                response = opener.open(req, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = json.load(response)
                if response.code != expected:
                    raise RuntimeError(f'API验证失败：{path} HTTP {response.code}；' + body.get('error', ''))
                return body

        def task_action(task, action, user):
            return request(f'/tasks/{task["id"]}/{action}', {'expected_version': task['version']}, user)

        def run_job(task, skill, inputs, user):
            queued = request(f'/tasks/{task["id"]}/jobs', {'expected_version': task['version'],
                'skill': skill, 'artifact_ids': inputs}, user, 202)
            app.jobs.tick()
            detail = request('/tasks/' + task['id'], user=user)
            job = next(j for j in detail['jobs'] if j['id'] == queued['id'])
            if job['state'] != 'completed':
                raise RuntimeError('真实技能验证失败：' + job['error'])
            return detail, json.loads(job['result_json'])

        try:
            tokens['admin'] = request('/auth/login', {'username': 'admin', 'password': password}, user=None)['access_token']
            users = {}
            for name, role in [('alice', 'manager'), ('bob', 'employee'), ('carol', 'employee'), ('outsider', 'employee')]:
                users[name] = request('/users', {'username': name, 'display_name': name, 'password': password, 'role': role}, 'admin', 201)
                tokens[name] = request('/auth/login', {'username': name, 'password': password}, user=None)['access_token']
            project = request('/projects', {'name': '隔离的三人Excel协作'}, expected=201)
            for name in ('bob', 'carol', 'outsider'):
                request(f'/projects/{project["id"]}/members/{users[name]["id"]}', {'role': 'member'})
            flow = request('/flows', {'project_id': project['id'], 'title': '部门报表整合与复核',
                'prepare_id': users['alice']['id'], 'review_id': users['bob']['id'], 'approver_id': users['carol']['id']}, expected=201)
            task = task_action(flow['task'], 'accept', 'alice')
            input_ids = []
            for file in sorted((ROOT / 'projects/excel-demo/待处理').glob('*.xlsx')):
                upload = request(f'/tasks/{task["id"]}/artifacts', {'name': file.name, 'kind': 'input',
                    'expected_version': task['version'], 'content_base64': base64.b64encode(file.read_bytes()).decode()}, 'alice', 201)
                task['version'] = upload['task_version']
                input_ids.append(upload['artifact']['id'])
            if len(input_ids) != 2:
                raise RuntimeError('固定示例应包含两份输入表。')
            task, merge = run_job(task, 'excel.merge.v1', input_ids, 'alice')
            request('/tasks/' + task['id'], user='outsider', expected=403)
            task = task_action(task, 'submit', 'alice')
            task_action(task, 'approve', 'bob')
            flow = request('/flows/' + flow['id'], user='bob')
            second = task_action(flow['tasks'][1], 'accept', 'bob')
            detail = request('/tasks/' + second['id'], user='bob')
            inputs = [a['id'] for a in detail['inputs'] if a['name'] in ('整合结果.xlsx', '检查结果.json')]
            second, summary = run_job(second, 'excel.summary.v1', inputs, 'bob')
            second = task_action(second, 'submit', 'bob')
            task_action(second, 'approve', 'carol')
            flow = request('/flows/' + flow['id'], user='carol')
            result = request('/flows/' + flow['id'] + '/approve', {'expected_version': flow['version']}, 'carol')
            if summary['report']['rows'] != 6 or summary['report']['total_amount'] != 5000.75 or result['state'] != 'completed':
                raise RuntimeError('结果对账或最终状态不符合固定样例。')
            return {'at': stamp(), 'passed': True, 'http_users': 3, 'real_registered_skills': ['excel.merge.v1', 'excel.summary.v1'],
                    'input_files': 2, 'retained_rows': summary['report']['rows'], 'total_amount': summary['report']['total_amount'],
                    'flow_state': result['state'], 'unauthorized_access_rejected': True,
                    'human_confirmations_simulated': True, 'real_recipients_contacted': False,
                    'model_called': False, 'business_database_modified': False}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


def main():
    parser = argparse.ArgumentParser(description='隔离验证多人后端HTTP权限、真实Excel技能和最终审批，不联系微信')
    parser.add_argument('--output', type=Path, help='可选：保存验证摘要')
    args = parser.parse_args()
    result = verify()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
