"""Classify provider errors without exposing raw bodies, headers or credentials."""
import json


def http_reason(error):
    status = error.code
    retry_after = error.headers.get('Retry-After', '') if error.headers else ''
    details = ''
    try:
        raw = error.read(16384)
        payload = json.loads(raw)
        value = payload.get('error', payload) if isinstance(payload, dict) else {}
        if isinstance(value, dict):
            details = ' '.join(str(value.get(k, '')) for k in ('type','code','message')).casefold()
    except (OSError, ValueError, TypeError):
        pass
    finally:
        error.close()
    messages = {
        400: '平台拒绝了请求参数，请核对完整模型 ID，或在更多设置中关闭扩展选项后重试。',
        401: 'API Key 无效或已失效，请在本机重新填写。',
        403: '接口拒绝访问，请检查 API Key 的模型权限或平台访问限制。',
        404: '接口路径或模型未找到，请核对 Base URL 和平台提供的完整模型 ID。',
    }
    if 300 <= status < 400:
        return '接口发生重定向，未继续转发密钥，请填写最终 API 地址。'
    if status != 429:
        return messages.get(status, f'模型平台暂时无法处理请求（HTTP {status}），请稍后重试。')
    if any(word in details for word in (
        'insufficient_quota','quota_exceeded','billing_hard_limit_reached','insufficient_balance',
        'quota exhausted','insufficient quota','insufficient balance','not enough quota',
        'quota is not enough','余额不足','额度已用尽','配额已用尽',
    )):
        return '平台明确返回额度或余额不足（HTTP 429），请检查平台账户和模型配额。'
    if any(word in details for word in (
        'no_available_channel','no available channel','overloaded','capacity','channel_saturated',
        '无可用渠道','上游繁忙','模型繁忙','渠道已满',
    )):
        reason = '第三方转发线路或上游模型暂时繁忙（HTTP 429），请稍后重试。'
    elif any(word in details for word in ('rate_limit','rate limit','too many requests','速率限制','请求过于频繁','限流')):
        reason = '第三方接口暂时限流（HTTP 429），请稍后重试。'
    else:
        reason = '第三方接口暂时拒绝请求（HTTP 429），原因未明确，不能据此认定余额不足。'
    if isinstance(retry_after, str) and retry_after.isdigit() and 0 < int(retry_after) <= 3600:
        reason += f'平台建议等待 {int(retry_after)} 秒后重试。'
    return reason
