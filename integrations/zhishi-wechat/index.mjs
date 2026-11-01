import fs from 'node:fs';

export function createHandler(config, fetcher = fetch) {
  const base = new URL(config.serviceUrl);
  if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(base.hostname)) {
    throw new Error('Zhishi bridge accepts only a local service URL.');
  }
  return async (event, context = {}) => {
    const channel = event.channel || context.channelId;
    if (channel !== 'openclaw-weixin') return;
    let route;
    try { route = JSON.parse(fs.readFileSync(config.routePath, 'utf8')); }
    catch { return { handled: true, text: '微信已进入助理通道，账号绑定尚在准备。请稍后再发“我的待办”。' }; }
    const account = context.accountId;
    // Official Weixin 2.4.10 supplies AccountId/To but omits SenderId.
    // The host derives conversationId from that channel-owned direct-chat target.
    // Never use message text, session-key parsing, or a group target as identity.
    const peer = event.senderId || context.senderId ||
      (event.isGroup === false ? context.conversationId : undefined);
    if (event.isGroup || account !== route.account_id || peer !== route.peer_id) {
      // Never fall through into an agent/model with another sender's data.
      return { handled: true, text: '当前为本人试点，这个会话尚未获准访问待办。' };
    }
    const messageId = event.messageId || context.messageId;
    if (!messageId) return { handled: true, text: '未取得微信消息编号，本次未修改待办。请重新发送。' };
    const text = (event.content || event.body || '').trim();
    try {
      const response = await fetcher(new URL('/api/wechat/inbound', base), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Wechat-Bridge': fs.readFileSync(config.secretPath, 'utf8').trim() },
        body: JSON.stringify({ account_id: account, peer_id: peer, source_message_id: String(messageId), text, is_group: false }),
        signal: AbortSignal.timeout(35000),
      });
      const result = await response.json();
      return { handled: true, text: response.ok ? result.reply : (result.error || '待办服务暂时不可用，请先查询待办再重试。') };
    } catch {
      return { handled: true, text: '待办服务暂未返回结果。为避免重复记录，请稍后先发送“我的待办”核对。' };
    }
  };
}

export default {
  id: 'zhishi-wechat-bridge',
  name: 'Zhishi WeChat task bridge',
  description: 'Routes authorized personal WeChat messages to the local task service without a model turn.',
  register(api) {
    api.on('before_dispatch', createHandler(api.pluginConfig), { priority: 1000, timeoutMs: 45000 });
  },
};
