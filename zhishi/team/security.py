"""Password and bearer-token primitives using Python's supported standard library."""
import hashlib
import hmac
import re
import secrets

from .errors import TeamError, require

ITERATIONS = 600_000


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def password_hash(password):
    require(isinstance(password, str) and 10 <= len(password) and len(password.encode('utf-8')) <= 256,
            '密码至少10个字符，且不超过256个UTF-8字节。', 400)
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${key}'


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password.encode('utf-8')) > 256:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(iterations) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def username(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,49}', value),
            '用户名须为3至50位字母、数字、点、横线或下划线。', 400)
    return value.lower()


def text(value, name, maximum=2000):
    require(isinstance(value, str) and 1 <= len(value.strip()) <= maximum and '\x00' not in value,
            f'{name}须为1至{maximum}个字符。', 400)
    return value.strip()


def integer(value, name='编号'):
    require(type(value) is int and value > 0, f'{name}须为正整数。', 400)
    return value


def csrf(token):
    return hmac.new(token.encode(), b'zhishi-team-csrf-v1', 'sha256').hexdigest()
