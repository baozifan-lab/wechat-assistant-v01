"""Create or verify a local database snapshot. Never restore over a running database."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from zhishi.core.backup import backup_database, inspect_database
from zhishi.core.clock import now


def main():
    parser = argparse.ArgumentParser(description='一致性备份知事数据库，或只读检查已有备份')
    parser.add_argument('--database', type=Path, default=ROOT / 'data' / 'pilot.sqlite3')
    parser.add_argument('--output', type=Path, help='新的备份文件路径；已存在时拒绝覆盖')
    parser.add_argument('--verify', type=Path, help='只读检查指定数据库，不启动知事服务')
    parser.add_argument('--timeout', type=float, default=30, help='在线备份超时秒数')
    args = parser.parse_args()
    if args.verify and args.output:
        parser.error('--verify 不能与 --output 同时使用')
    try:
        if args.verify:
            result = inspect_database(args.verify)
        else:
            destination = args.output or args.database.resolve().parent / 'backups' / (
                'pilot-' + now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
            result = backup_database(args.database, destination, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        # SQLite exception messages may contain private paths; expose only their type.
        print('备份或检查未完成（' + type(error).__name__ + '），请检查输入、目标路径和数据库状态。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
