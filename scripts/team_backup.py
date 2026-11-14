import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.team.backup import create_snapshot, restore_snapshot, verify_snapshot
from zhishi.team.errors import TeamError, require


def main():
    parser = argparse.ArgumentParser(description='多人数据库与成果完整备份、验证及恢复到新位置')
    parser.add_argument('command', choices=('create', 'verify', 'restore'))
    parser.add_argument('--directory', type=Path, required=True, help='备份包目录')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--artifact-root', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'verify':
            result = verify_snapshot(args.directory)
        else:
            require(args.database is not None, '需要--database。', 400)
            root = args.artifact_root or args.database.resolve().parent / 'team-artifacts'
            result = create_snapshot(args.database, root, args.directory) if args.command == 'create' else restore_snapshot(args.directory, args.database, root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(str(error) if isinstance(error, TeamError) else '备份或恢复未完成（' + type(error).__name__ + '），不完整目录不可用作恢复来源。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
