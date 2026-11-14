import {api, apiRequest} from './api.js';

const $ = id => document.getElementById(id);
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, callback, cls = 'task-action-btn') {
  const node = el('button', cls, text); node.type = 'button'; node.addEventListener('click', callback); return node;
}
let snapshot, active = false, loading = false, editing = null, connecting = null;
let qrUrl = null, qrVersion = '', qrLoading = '', requestedVersion = null, connectBusy = false;
let renderSignature = '', notify;
let deleting = null;
const membershipBusy = new Set();

function connectionStatus(account) {
  if (account.deleted_at) return ['已删除', 'attention'];
  const c = account.connection, login = c.login?.state;
  if (c.duplicate) return ['账户重复', 'attention'];
  if (['starting', 'refreshing'].includes(login)) return ['申请二维码中', 'waiting'];
  if (login === 'waiting_scan') return ['等待扫码', 'waiting'];
  if (login === 'waiting_confirm') return ['等待手机确认', 'waiting'];
  if (c.connection_state === 'disabled') return ['已停用', 'attention'];
  if (c.online) return ['已连接', 'online'];
  if (c.connection_state === 'starting' || login === 'gateway_starting') return ['正在启动', 'waiting'];
  if (['error', 'expired', 'needs_attention'].includes(login) || c.connection_state === 'needs_attention') return ['需要检查', 'attention'];
  if (c.bound) return ['已授权 · 离线', 'attention'];
  return ['未连接', 'unbound'];
}
function render() {
  if (!snapshot) return;
  $('accountTotal').textContent = snapshot.summary.total;
  $('accountOnline').textContent = snapshot.summary.online;
  $('accountUnbound').textContent = snapshot.summary.unbound;
  const query = $('accountSearch').value.trim().toLocaleLowerCase(), filter = $('accountFilter').value;
  const signature = JSON.stringify([snapshot, query, filter]);
  if (signature !== renderSignature) {
    renderSignature = signature;
    const deletedMode = filter === 'deleted';
    const accounts = deletedMode ? (snapshot.deleted_accounts || []).map(a => ({...a, shared: a.id === 'existing-primary', connection: {bound: false}})) : snapshot.accounts;
    const rows = accounts.filter(a => {
      const category = connectionStatus(a)[1];
      const matches = [a.name, a.wechat_note, a.note].some(t => t.toLocaleLowerCase().includes(query));
      return matches && (deletedMode || filter === 'all' || category === filter);
    });
    const list = $('accountList'); list.replaceChildren();
    for (const account of rows) {
      const [label, category] = connectionStatus(account);
      const row = el('div', 'account-row'); row.dataset.id = account.id;
      const person = el('div', 'account-person'), avatar = el('span', 'account-avatar', account.name.slice(0, 1));
      avatar.setAttribute('aria-hidden', 'true');
      const personInfo = el('div', 'account-person-info');
      personInfo.append(el('h3', 'account-name', account.name));
      personInfo.append(el('p', 'account-subtext', account.note || (account.shared ? '原主微信 · 共享连接' : '独立员工连接')));
      person.append(avatar, personInfo);
      const wechat = el('div', 'account-wechat');
      wechat.append(el('div', '', account.wechat_note || '未填写微信备注'));
      wechat.append(el('p', 'account-subtext account-fingerprint', account.deleted_at ? '恢复后需重新扫码连接' : account.fingerprint ? '授权指纹 ' + account.fingerprint : '等待员工扫码授权'));
      const status = el('div', 'account-connection');
      const badge = el('span', 'account-status ' + category);
      const dot = el('span', 'status-dot'); dot.setAttribute('aria-hidden', 'true');
      badge.append(dot, document.createTextNode(label)); status.append(badge);
      if (account.last_seen) status.append(el('p', 'account-subtext', '最近消息 ' + new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
      }).format(new Date(account.last_seen))));
      const actions = el('div', 'account-row-actions');
      if (account.deleted_at) {
        const restore = button('恢复员工', () => changeMembership(account, true));
        restore.setAttribute('aria-label', '恢复员工' + account.name);
        restore.disabled = membershipBusy.has(account.id); actions.append(restore);
      } else {
        const connect = button(account.connection.bound ? '管理连接' : '连接微信', () => openConnection(account), account.connection.bound ? 'task-action-btn' : 'btn-secondary');
        connect.disabled = !snapshot.can_connect || membershipBusy.has(account.id);
        connect.setAttribute('aria-label', (account.connection.bound ? '管理' : '连接') + account.name + '的微信');
        const edit = button('编辑', () => openEdit(account)); edit.setAttribute('aria-label', '编辑' + account.name);
        const remove = button('删除', () => confirmRemoval(account), 'task-action-btn account-delete');
        remove.setAttribute('aria-label', '删除员工' + account.name);
        edit.disabled = remove.disabled = membershipBusy.has(account.id);
        actions.append(connect, edit, remove);
      }
      row.append(person, wechat, status, actions); list.append(row);
    }
    if (!rows.length) list.append(el('p', 'empty-state', deletedMode ? '没有已删除的员工' : accounts.length ? '没有符合条件的员工，试试其他姓名或状态' : '还没有员工，点击“添加员工”建立第一个账户'));
    $('accountResultCount').textContent = `显示 ${rows.length} / ${accounts.length} 个账户`;
  }
  if (connecting && $('accountConnectModal').open) {
    if (snapshot.accounts.some(a => a.id === connecting)) renderConnection();
    else { closeConnection(); notify('该员工已删除，连接窗口已关闭'); }
  }
}
function confirmRemoval(account) {
  deleting = account;
  $('deleteAccountName').textContent = account.name;
  $('deleteAccountWarning').textContent = account.shared ? '这条微信由负责人和员工1共用，删除后会同时停用。' : '删除后，这位员工将移出账户列表，对应微信连接也会停用。';
  $('deleteAccountFeedback').textContent = '';
  $('deleteAccountModal').showModal(); $('cancelDeleteAccount').focus();
}
async function changeMembership(account, restore = false) {
  if (membershipBusy.has(account.id)) return;
  membershipBusy.add(account.id); renderSignature = ''; render();
  try {
    await api('/api/accounts/' + (restore ? 'restore' : 'delete'), {id: account.id, revision: account.revision});
    if (!restore) { $('deleteAccountModal').close(); deleting = null; }
    notify(restore ? '员工已恢复，请重新扫码连接微信' : '员工已删除，可在“已删除”中恢复');
    await refresh();
  } catch (error) {
    if (!restore) $('deleteAccountFeedback').textContent = error.message;
    else notify(error.message);
  }
  finally { membershipBusy.delete(account.id); renderSignature = ''; render(); }
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    snapshot = await api('/api/accounts'); $('accountError').hidden = true; render();
  } catch (error) {
    $('accountError').textContent = error.message + ' 请稍后刷新，已有资料会保留。'; $('accountError').hidden = false;
    if (connecting) $('accountConnectStatus').textContent = '暂时无法读取连接状态，请重试';
  } finally { loading = false; }
}
function openEdit(account = null) {
  editing = account;
  $('accountModalTitle').textContent = account ? '编辑员工' : '添加员工';
  $('accountName').value = account?.name || '';
  $('accountWechatNote').value = account?.wechat_note || '';
  $('accountNote').value = account?.note || '';
  $('accountFormFeedback').textContent = '';
  $('accountModal').showModal(); $('accountName').focus();
}
function clearQr() {
  if (qrUrl) URL.revokeObjectURL(qrUrl);
  qrUrl = null; qrVersion = ''; qrLoading = '';
  $('accountQr').hidden = true; $('accountQr').removeAttribute('src');
  $('accountQrPlaceholder').hidden = false;
}
function closeConnection() {
  connecting = null; clearQr(); $('accountConnectModal').close();
}
async function openConnection(account) {
  connecting = account.id; requestedVersion = null; clearQr();
  $('accountConnectName').textContent = account.name;
  $('accountConnectTitle').textContent = account.connection.bound ? '管理微信连接' : '连接微信';
  $('accountConnectModal').showModal(); renderConnection();
  if (!account.connection.bound) await requestQr();
}
async function requestQr() {
  if (!connecting || connectBusy) return;
  const id = connecting, account = snapshot.accounts.find(a => a.id === id);
  if (account.connection.bound && !window.confirm('重新扫码会更新这位员工的微信授权，现有任务仍保留原接收人。确认重新连接吗？')) return;
  connectBusy = true;
  requestedVersion = account.connection.login?.qr_version || '';
  clearQr(); $('refreshAccountQr').disabled = true;
  $('accountConnectStatus').textContent = '正在申请新的授权二维码';
  $('accountConnectDetail').textContent = '首次申请可能需要一些时间，请保持此窗口打开';
  try {
    await api('/api/accounts/connect', {id, request_id: crypto.randomUUID()});
    await refresh();
  } catch (error) {
    if (connecting === id) {
      requestedVersion = null;
      $('accountConnectStatus').textContent = '未能生成二维码'; $('accountConnectDetail').textContent = error.message;
    }
    notify(error.message);
  } finally { connectBusy = false; $('refreshAccountQr').disabled = false; }
}
function renderConnection() {
  const account = snapshot.accounts.find(a => a.id === connecting);
  if (!account) return;
  $('accountConnectName').textContent = account.name;
  const c = account.connection, login = c.login || {};
  $('pauseAccount').hidden = !c.bound || c.connection_state === 'disabled';
  if (requestedVersion !== null && (['starting', 'refreshing'].includes(login.state) ||
      (login.qr_version && login.qr_version !== requestedVersion))) requestedVersion = null;
  const awaitingNew = requestedVersion !== null &&
    (login.qr_version || '') === requestedVersion && !['starting', 'refreshing', 'error', 'expired'].includes(login.state);
  const ready = login.state === 'waiting_scan' && login.qr_version && !awaitingNew && !c.duplicate;
  const [label] = connectionStatus(account);
  $('accountConnectStatus').textContent = awaitingNew ? '正在申请新的授权二维码' : label;
  $('accountConnectDetail').textContent = c.duplicate ? c.detail : awaitingNew ? '请稍候，二维码生成后会自动显示' :
    c.online ? '微信通道已就绪；消息是否送达请以手机实际收发为准' : login.detail || c.detail || '点击刷新二维码，申请本人的微信授权';
  $('refreshAccountQr').textContent = c.bound ? '重新扫码连接' : '刷新二维码';
  if (ready) loadQr(account.id, login.qr_version);
  else {
    if (qrUrl || qrLoading) clearQr();
    $('accountQrPlaceholder').hidden = false;
    $('accountQrPlaceholder').querySelector('span').textContent = c.online && !awaitingNew ? '微信已连接' :
      ['error', 'expired', 'needs_attention'].includes(login.state) ? '请刷新二维码重试' : '等待微信授权';
  }
}
async function loadQr(id, version) {
  const key = id + ':' + version;
  if (qrVersion === key || qrLoading === key) return;
  qrLoading = key;
  try {
    const response = await apiRequest('/api/accounts/qr?id=' + encodeURIComponent(id));
    if (!response.ok) throw Error('二维码尚未就绪，请稍候');
    const blob = await response.blob();
    if (connecting !== id || !qrLoading || qrLoading !== key) return;
    if (qrUrl) URL.revokeObjectURL(qrUrl);
    qrUrl = URL.createObjectURL(blob); qrVersion = key;
    $('accountQr').src = qrUrl; $('accountQr').hidden = false; $('accountQrPlaceholder').hidden = true;
  } catch (error) {
    if (connecting === id) $('accountConnectDetail').textContent = error.message;
  } finally { if (qrLoading === key) qrLoading = ''; }
}
export function initAccounts({toast}) {
  notify = toast;
  document.addEventListener('zhishi:page', event => { active = event.detail === 'accounts'; if (active) refresh(); });
  $('addAccount').addEventListener('click', () => openEdit());
  for (const id of ['closeDeleteAccount', 'cancelDeleteAccount']) $(id).addEventListener('click', () => { deleting = null; $('deleteAccountModal').close(); });
  $('deleteAccountModal').addEventListener('cancel', () => { deleting = null; });
  $('confirmDeleteAccount').addEventListener('click', async () => {
    if (!deleting || $('confirmDeleteAccount').disabled) return;
    $('confirmDeleteAccount').disabled = true;
    try { await changeMembership(deleting); } finally { $('confirmDeleteAccount').disabled = false; }
  });
  for (const id of ['closeAccountModal', 'cancelAccountEdit']) $(id).addEventListener('click', () => $('accountModal').close());
  for (const id of ['closeAccountConnect', 'doneAccountConnect']) $(id).addEventListener('click', closeConnection);
  $('accountConnectModal').addEventListener('cancel', () => { connecting = null; clearQr(); });
  $('refreshAccountQr').addEventListener('click', requestQr);
  $('pauseAccount').addEventListener('click', async () => {
    if (!connecting || $('pauseAccount').disabled) return;
    const account = snapshot.accounts.find(a => a.id === connecting);
    if (!window.confirm((account.shared ? '这条连接由负责人和员工1共用。' : '') + '停用后将暂停后续微信消息收发，已提交消息不能撤回。授权资料会保留。确认停用吗？')) return;
    $('pauseAccount').disabled = true;
    try { await api('/api/accounts/pause', {id: connecting}); notify('微信连接已停用'); await refresh(); }
    catch (error) { notify(error.message); }
    finally { $('pauseAccount').disabled = false; }
  });
  $('refreshAccounts').addEventListener('click', refresh);
  $('accountSearch').addEventListener('input', render);
  $('accountFilter').addEventListener('change', render);
  $('accountForm').addEventListener('submit', async event => {
    event.preventDefault();
    if ($('saveAccount').disabled) return;
    $('saveAccount').disabled = true; $('accountFormFeedback').textContent = '';
    try {
      await api('/api/accounts/save', {name: $('accountName').value.trim(), wechat_note: $('accountWechatNote').value.trim(),
        note: $('accountNote').value.trim(), ...(editing ? {id: editing.id, revision: editing.revision} : {}), request_id: crypto.randomUUID()});
      $('accountModal').close(); notify(editing ? '员工资料已保存' : '员工已添加，可以连接微信'); await refresh();
    } catch (error) { $('accountFormFeedback').textContent = error.message; }
    finally { $('saveAccount').disabled = false; }
  });
  setInterval(() => { if (active && !document.hidden) refresh(); }, 3000);
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) refresh(); });
}
