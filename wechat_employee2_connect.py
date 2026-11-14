"""Separate official WeChat login and gateway for the employee-2 demo."""
import json
from pathlib import Path
from zhishi.channels.wechat import login

primary = Path.home() / '.openclaw-zhishi-pilot'
profile = Path.home() / '.openclaw-zhishi-employee2'
profile.mkdir(parents=True, exist_ok=True)
config = profile / 'openclaw.json'
if not config.exists():
    candidates = list((primary / 'npm' / 'projects').glob('*/node_modules/@tencent-weixin/openclaw-weixin'))
    if len(candidates) != 1:
        raise RuntimeError('无法定位现有官方微信插件。')
    config.write_text(json.dumps({'plugins': {'allow': ['openclaw-weixin'],
        'entries': {'openclaw-weixin': {'enabled': True}},
        'load': {'paths': [str(candidates[0])]}}}, ensure_ascii=False, indent=2), encoding='utf-8')
login.PROFILE = profile
login.DATA = login.ROOT / 'data' / 'employee2-wechat'
login.PORT = 18792
login.QR_PROFILE = primary
login.BRIDGE_SECRET = primary / 'zhishi-bridge.secret'
login.main()
