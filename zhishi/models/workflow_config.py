"""Local workflow API configuration; Windows protects the key with user DPAPI."""
import base64
import ctypes
import json
import os
import re
import secrets
import threading
import urllib.parse
from pathlib import Path


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('当前系统请使用环境变量配置密钥；网页加密保存仅支持 Windows。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    operation.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    try:
        if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise ValueError('无法读取或加密保存 API Key，请在本机重新填写。')
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))
        ctypes.memset(buffer, 0, len(buffer))


def normalize_base(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise ValueError('请填写接口地址。')
    value = value.strip().rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError('接口地址格式不正确。') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or re.search(r'[\s\\]', value)):
        raise ValueError('请填写 http/https 接口地址，不要在地址中放密钥、账号或查询参数。')
    if value.endswith('/chat/completions'):
        value = value[:-len('/chat/completions')]
    if not urllib.parse.urlsplit(value).path:
        value += '/v1'
    return value


class WorkflowConfig:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()

    @staticmethod
    def environment():
        return {'base_url': os.environ.get('WORKFLOW_MODEL_BASE_URL') or os.environ.get('MODEL_BASE_URL', ''),
                'model': os.environ.get('WORKFLOW_MODEL', 'planner-strong'),
                'api_key': os.environ.get('WORKFLOW_MODEL_API_KEY') or os.environ.get('MODEL_API_KEY', ''),
                'disable_thinking': False, 'json_mode': False, 'source': 'environment'}

    def read(self):
        with self.lock:
            if not self.path or not self.path.exists():
                return self.environment()
            try:
                value = json.loads(self.path.read_text('utf-8'))
                value['base_url'] = normalize_base(value['base_url'])
                value['model'] = self.model_id(value['model'])
                encrypted = value.pop('protected_api_key', '')
                value['api_key'] = _dpapi(base64.b64decode(encrypted, validate=True), decrypt=True).decode() if encrypted else ''
                for field in ('disable_thinking', 'json_mode'):
                    if type(value.get(field, False)) is not bool:
                        raise ValueError()
                value['source'] = 'local'
                return value
            except (OSError, ValueError, TypeError, KeyError, UnicodeError):
                raise ValueError('本机 API 配置无法读取，请重新保存；迁移电脑后需要重新填写密钥。') from None

    @staticmethod
    def model_id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}', value.strip()):
            raise ValueError('请填写平台提供的完整模型 ID，例如 qwen3.8-max。')
        return value.strip()

    def save(self, data):
        if not self.path:
            raise ValueError('当前实例未启用本机配置保存。')
        if not isinstance(data, dict) or set(data) - {'base_url','model','api_key','clear_key','disable_thinking','json_mode'}:
            raise ValueError('API 配置字段不正确。')
        base = normalize_base(data.get('base_url'))
        model = self.model_id(data.get('model'))
        key = data.get('api_key', '')
        if (not isinstance(key, str) or len(key) > 4000 or re.search(r'[\s\x00-\x1f\x7f]', key)
                or any(type(data.get(field, False)) is not bool for field in ('clear_key','disable_thinking','json_mode'))):
            raise ValueError('密钥或配置选项格式不正确。')
        with self.lock:
            try:
                old = self.read()
            except ValueError:
                old = {}
            # Never transfer a stored credential to a different endpoint.
            if not key and data.get('clear_key') is not True and old.get('base_url', '').rstrip('/') == base:
                key = old.get('api_key', '')
            value = {'base_url': base, 'model': model,
                     'disable_thinking': data.get('disable_thinking', False), 'json_mode': data.get('json_mode', False),
                     'protected_api_key': base64.b64encode(_dpapi(key.encode())).decode() if key else ''}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(4) + '.tmp')
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.chmod(0o600)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
