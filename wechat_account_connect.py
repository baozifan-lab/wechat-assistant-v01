"""Run the existing official login supervisor for one directory-owned employee."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys

from zhishi.channels.wechat import login


def configure(database, ident, service_url):
    if not re.fullmatch(r'person-[a-f0-9]{24}', ident):
        raise ValueError('员工编号无效。')
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', service_url):
        raise ValueError('仅允许本机知事服务。')
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT id,channel,port FROM connection_accounts WHERE deleted_at IS NULL')]
    row = next((r for r in rows if r['id'] == ident), None)
    if not row or row['channel'] != ident or not 18800 <= row['port'] < 18900:
        raise ValueError('员工连接配置无效。')
    base = Path(database).resolve().parent / 'accounts'
    directory = base / ident
    primary = Path.home() / '.openclaw-zhishi-pilot'
    profile = directory / 'profile'
    login.PROFILE, login.DATA, login.PORT = profile, directory / 'login', row['port']
    login.SERVICE_URL, login.QR_PROFILE = service_url, primary
    login.BRIDGE_SECRET = None
    profile.mkdir(parents=True, exist_ok=True)
    login.DATA.mkdir(parents=True, exist_ok=True)

    def unique_binding(account, peer):
        # Do not let a second profile silently claim an already-connected identity.
        for other in rows:
            if other['id'] == ident:
                continue
            other_profile = (primary if other['channel'] == 'primary' else
                             Path.home() / '.openclaw-zhishi-employee2' if other['channel'] == 'employee2' else
                             base / other['id'] / 'profile')
            try:
                route = json.loads((other_profile / 'zhishi-binding.json').read_text('utf-8'))
            except (OSError, ValueError):
                continue
            if route.get('account_id') == account:
                raise login.BindingRejected('这个微信已经属于其他员工，请使用该员工自己的微信重新扫码。')

    login.BINDING_CHECK = unique_binding
    config = profile / 'openclaw.json'
    if not config.exists():
        candidates = list((primary / 'npm/projects').glob('*/node_modules/@tencent-weixin/openclaw-weixin'))
        if len(candidates) != 1:
            raise RuntimeError('无法定位官方微信插件，请先完成主微信运行环境安装。')
        config.write_text(json.dumps({'plugins': {'allow': ['openclaw-weixin'],
            'entries': {'openclaw-weixin': {'enabled': True}},
            'load': {'paths': [str(candidates[0])]}}}, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--service-url', required=True)
    parser.add_argument('--use-saved', action='store_true')
    args = parser.parse_args()
    try:
        configure(args.database, args.account_id, args.service_url)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        if re.fullmatch(r'person-[a-f0-9]{24}', args.account_id):
            login.DATA = Path(args.database).resolve().parent / 'accounts' / args.account_id / 'login'
            login.DATA.mkdir(parents=True, exist_ok=True)
            login.write_status('error', detail='员工微信初始化失败，请检查官方插件与本机运行环境。')
        return
    sys.argv = [sys.argv[0], *(['--use-saved'] if args.use_saved else [])]
    login.main()


if __name__ == '__main__':
    main()
