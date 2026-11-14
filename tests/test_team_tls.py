import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
import unittest
import urllib.request

from zhishi.team.runtime import create_team_server
from tests.team_support import TeamCase, PASSWORD


def openssl_path():
    value = shutil.which('openssl')
    if value:
        return value
    git = shutil.which('git')
    if git:
        candidate = Path(git).resolve().parents[1] / 'usr/bin/openssl.exe'
        if candidate.exists():
            return str(candidate)


@unittest.skipUnless(openssl_path(), 'OpenSSL executable required for ephemeral TLS integration certificate')
class TeamTLSTests(TeamCase):
    def test_tls_login_sets_secure_cookie_and_slow_handshake_does_not_block_other_users(self):
        key, cert = self.root / 'temporary.key', self.root / 'temporary.crt'
        result = subprocess.run([openssl_path(), 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        server = create_team_server(self.app, 0, tls_cert=cert, tls_key=key)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stalled = socket.create_connection(('127.0.0.1', server.server_port), timeout=3)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
            request = urllib.request.Request(f'https://localhost:{server.server_port}/api/team/auth/login',
                data=json.dumps({'username': 'bob', 'password': PASSWORD}).encode(), headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=5) as response:
                result = json.load(response)
                self.assertEqual(result['user']['id'], self.ids['bob'])
                self.assertIn('; Secure', response.headers['Set-Cookie'])
        finally:
            stalled.close()
            server.shutdown()
            server.server_close()
            thread.join(3)
