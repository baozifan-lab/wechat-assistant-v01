"""Read local health using the same loopback credential as the browser."""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description='检查正在运行的本机知事服务，不启动后台任务')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口须在1至65535之间')
    base = f'http://127.0.0.1:{args.port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(base, timeout=5) as response:
            html = response.read(1_000_000).decode('utf-8')
        match = re.search(r'<meta name="pilot-token" content="([^"]+)">', html)
        if not match:
            raise ValueError('本机访问凭证不可用')
        request = urllib.request.Request(base + '/api/health', headers={'X-Pilot-Token': match[1]})
        try:
            response = opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            if error.code != 503:
                error.close()
                raise
            response = error
        with response:
            result = json.loads(response.read(1_000_000))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') == 'ok' else 1
    except (OSError, ValueError):
        print('无法读取运行状态，请检查服务是否已启动、端口是否正确。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
