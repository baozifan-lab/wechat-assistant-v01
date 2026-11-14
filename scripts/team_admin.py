"""Explicit account initialization and copy-first migration; never prints passwords."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.team.application import TeamApplication
from zhishi.team.migration import import_legacy, verify_team
from zhishi.team.errors import TeamError, require


def main():
    parser = argparse.ArgumentParser(description='知事多人后端初始化、只读验证和副本迁移')
    parser.add_argument('command', choices=('init', 'import', 'verify'))
    parser.add_argument('--database', type=Path, required=True, help='多人数据库；import时必须是不存在的新路径')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--password-env', help='从指定环境变量取得初始口令；省略时在终端安全输入')
    parser.add_argument('--source', type=Path, help='仅import：旧试点数据库')
    parser.add_argument('--owner', default='pilot-owner')
    parser.add_argument('--artifact-root', type=Path)
    args = parser.parse_args()
    try:
        require(args.database.resolve() != (ROOT / 'data/pilot.sqlite3').resolve(), '请使用独立多人库或副本，不能操作默认试点原库。', 409)
        if args.command == 'verify':
            result = verify_team(args.database, args.artifact_root)
        else:
            password = os.environ.get(args.password_env, '') if args.password_env else getpass.getpass('设置初始管理员密码（至少10个字符）：')
            require(bool(password), '未取得初始密码。', 400)
            if args.command == 'import':
                require(args.source is not None, 'import需要--source。', 400)
                result = import_legacy(args.source, args.database, args.username, password,
                                       owner=args.owner, artifact_root=args.artifact_root)
            else:
                app = TeamApplication(args.database, args.artifact_root)
                result = {'ok': True, 'user': app.auth.bootstrap(args.username, password)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, sqlite3.Error, TeamError) as error:
        print(str(error) if isinstance(error, TeamError) else '操作失败（' + type(error).__name__ + '），请检查路径与数据库副本。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
