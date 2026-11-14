"""Create private, synthetic demo data and random credentials without starting delivery."""
import argparse
import json
from pathlib import Path
import secrets
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.team.application import TeamApplication
from zhishi.team.errors import TeamError, require


def create_demo(directory, port=8766):
    directory = Path(directory).resolve()
    require(not directory.exists() or not any(directory.iterdir()), '演示目录必须为空，不会覆盖已有数据库或账号。', 409)
    directory.mkdir(parents=True, exist_ok=True)
    app = TeamApplication(directory / 'team.sqlite3')
    records, users = [], {}
    for name, role in [('admin', 'admin'), ('alice', 'manager'), ('bob', 'employee'), ('carol', 'employee')]:
        password = secrets.token_urlsafe(24)
        user = app.auth.bootstrap(name, password) if name == 'admin' else app.auth.create_user(users['admin']['id'],
            {'username': name, 'display_name': name, 'role': role, 'password': password})
        users[name] = user
        records.append({'username': name, 'role': role, 'password': password})
    project = app.service.create_project(users['alice']['id'], {'name': '演示：整理、复核与最终审批', 'request_id': str(uuid.uuid4())})
    for name in ('bob', 'carol'):
        app.service.set_member(users['alice']['id'], project['id'], users[name]['id'], {'role': 'member', 'request_id': str(uuid.uuid4())})
    flow = app.service.start_flow(users['alice']['id'], {'project_id': project['id'], 'title': '示例报表协作',
        'prepare_id': users['alice']['id'], 'review_id': users['bob']['id'], 'approver_id': users['carol']['id'], 'request_id': str(uuid.uuid4())})
    credentials = directory / 'accounts.json'
    with credentials.open('x', encoding='utf-8') as stream:
        json.dump({'demo_only': True, 'service_url': f'http://127.0.0.1:{port}', 'accounts': records}, stream, ensure_ascii=False, indent=2)
    credentials.chmod(0o600)
    return {'ok': True, 'database': str(app.store.path), 'credentials_file': str(credentials),
            'project_id': project['id'], 'flow_id': flow['id'], 'wechat_enabled': False}


def main():
    parser = argparse.ArgumentParser(description='创建独立的三人协作演示数据；随机口令写入私有目录，不输出口令')
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/team-demo')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    try:
        print(json.dumps(create_demo(args.directory, args.port), ensure_ascii=False, indent=2))
    except (OSError, TeamError) as error:
        print(str(error) if isinstance(error, TeamError) else '演示数据创建失败，请检查新目录。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
