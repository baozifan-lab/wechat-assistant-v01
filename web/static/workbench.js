import {api, apiRequest} from './api.js';
import {routeFor, hashFor} from './navigation.js';
import {initAccounts} from './accounts.js';
import {initAdmin, renderAdmin} from './admin.js';

const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const result = document.createElement(tag);
  if (cls) result.className = cls;
  if (text !== undefined) result.textContent = text;
  return result;
};
const formatTime = value => value ? new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
}).format(new Date(value)) : '';
let state, filter = 'all', route = routeFor(location.pathname, location.hash);
let refreshing = false, sending = false, configDirty = false, configBusy = false, reminderDirty = false;
let chatTarget = 'assistant', chatRevision = null, detailSnapshot = '', chatSnapshot = '', taskSnapshot = '', dashboardSnapshot = '', toastTimer;
let previewLoaded = false;
let replacingTask = null;
const drafts = new Map();

// 新增：从localStorage加载草稿
function loadDraftsFromStorage() {
  try {
    const stored = localStorage.getItem('zhishi_drafts');
    if (stored) {
      const parsed = JSON.parse(stored);
      for (const [key, value] of Object.entries(parsed)) {
        drafts.set(key, value);
      }
    }
  } catch (e) {
    console.warn('加载草稿失败:', e);
  }
}

// 新增：保存草稿到localStorage
function saveDraftsToStorage() {
  try {
    const obj = {};
    for (const [key, value] of drafts.entries()) {
      obj[key] = value;
    }
    localStorage.setItem('zhishi_drafts', JSON.stringify(obj));
  } catch (e) {
    console.warn('保存草稿失败:', e);
  }
}

function toast(text) {
  $('toast').textContent = text;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4500);
}
function button(text, callback, cls = 'btn btn-secondary') {
  const b = el('button', cls, text); b.type = 'button';
  b.addEventListener('click', callback); return b;
}
function navigate(page, params) {
  const hash = hashFor(page, params);
  if (location.hash === hash) applyRoute(); else location.hash = hash;
}
function selectedTask(id) { return state?.tasks.find(t => t.id === id); }
function applyRoute() {
  route = routeFor(location.pathname, location.hash);
  for (const item of document.querySelectorAll('.nav-link')) {
    const active = item.dataset.page === route.page;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
  }
  for (const page of document.querySelectorAll('.page')) page.classList.toggle('active', page.id === 'page-' + route.page);
  document.dispatchEvent(new CustomEvent('zhishi:page', {detail: route.page}));
  if (route.page === 'chat') chooseConversation(route.task || 'assistant');
  if (route.page === 'tasks') renderTasks();
  if (route.page === 'tasks' && route.task && state) showDetail(route.task);
  else if ($('workModal').open) $('workModal').close();
  if (route.preview) $('message-preview').open = true;
}

async function action(path, data, source) {
  if (source?.disabled) return false;
  let originalContent;
  if (source) {
    source.disabled = true;
    source.dataset.busy = 'true';
    // 保存原始文本并显示加载状态
    originalContent = [...source.childNodes];
    source.textContent = '处理中…';
    source.setAttribute('aria-busy', 'true');
  }
  try {
    const result = await api(path, data);
    toast(result.reply || (result.code ? '流程已启动，负责人通知已排队。' : '操作已完成'));
    await refresh(true); return result;
  } catch (error) { toast(error.message); return false; }
  finally {
    if (source) {
      delete source.dataset.busy;
      if (source.isConnected) {
        source.disabled = false;
        source.replaceChildren(...originalContent);
        source.removeAttribute('aria-busy');
      }
    }
  }
}
async function reminderAction(task, kind, source) {
  return action('/api/console/reminder', {action: kind, id: Number(task.id.split(':')[1]),
    minutes: 10, request_id: crypto.randomUUID()}, source);
}
async function download(path, name) {
  try {
    const response = await apiRequest(path);
    if (!response.ok) throw Error('成果文件不可用或校验未通过。');
    const url = URL.createObjectURL(await response.blob()), a = el('a');
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { toast(error.message); }
}
function taskRow(task, compact = false) {
  const item = el('div', 'task-item' + (task.status === 'done' ? ' completed' : ''));
  item.dataset.status = task.status;
  const mark = el('span', 'task-state-mark'); mark.setAttribute('aria-hidden', 'true');
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('fill', 'none');
  icon.setAttribute('stroke', 'currentColor');
  const circle = document.createElementNS(icon.namespaceURI, 'circle');
  circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '9');
  icon.append(circle);
  const paths = {done: 'm8 12 3 3 5-6', blocked: 'M12 7v6m0 3v.01', cancelled: 'm9 9 6 6m0-6-6 6'};
  if (paths[task.status]) {
    const path = document.createElementNS(icon.namespaceURI, 'path');
    path.setAttribute('d', paths[task.status]); icon.append(path);
  }
  mark.append(icon);
  const content = el('div', 'task-content');
  const heading = el('div', 'task-heading');
  heading.append(el('div', 'task-title', task.display_name || task.title), el('span', 'task-state-label', task.state_label));
  content.append(heading);
  const meta = [task.kind_label, '编号 ' + task.code, task.owner].filter(Boolean);
  if (task.due) meta.push('截止 ' + formatTime(task.due));
  if (task.next_at) meta.push('下次提醒 ' + formatTime(task.next_at));
  content.append(el('div', 'task-meta', meta.join(' · ')));
  const actions = el('div', 'task-actions');
  const detail = button('查看详情', () => navigate('tasks', {task: task.id}), 'task-action-btn');
  detail.setAttribute('aria-label', '查看详情：' + (task.display_name || task.title)); actions.append(detail);
  if (!compact && task.messages) actions.append(button('对话记录', () => navigate('chat', {task: task.id}), 'task-action-btn'));
  if (!compact && task.kind === 'reminder') {
    for (const [kind, label] of [['done', '完成'], ['snooze', '10分钟后'], ['cancel', '取消']]) {
      if (!task.actions.includes(kind)) continue;
      const b = button(label, () => reminderAction(task, kind, b), 'task-action-btn'); actions.append(b);
    }
  }
  item.append(mark, content, actions); return item;
}
function renderDashboard() {
  const d = state.dashboard;
  const signature = JSON.stringify([d, state.tasks, state.settings.channels.map(c => c.online), state.now.slice(0, 13)]);
  if (dashboardSnapshot === signature) return;
  dashboardSnapshot = signature;
  $('stat-channel').textContent = state.settings.channels.some(c => c.online) ? '微信' : '本地';
  $('navTaskCount').textContent = d.open + d.blocked;
  $('navTaskCount').hidden = d.open + d.blocked === 0;
  // Recent tasks and charts share the selected overview scope in admin.js.
}
function renderTasks() {
  if (!state) return;
  const query = $('taskSearch').value.trim().toLowerCase(), from = $('taskStartDate').value, until = $('taskEndDate').value;
  const invalid = Boolean(from && until && from > until);
  $('taskSearchError').hidden = !invalid;
  const signature = JSON.stringify([state.tasks, state.templates, state.settings.task_config_error, filter, route.kind, query, from, until]);
  if (taskSnapshot === signature) return;
  taskSnapshot = signature;
  const area = $('tasksList'); area.replaceChildren();
  const tasks = state.tasks.filter(t => {
    const date = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date(t.created_at));
    return !invalid && (!query || [t.id,t.code,t.title,t.display_name].filter(Boolean).join(' ').toLowerCase().includes(query)) &&
      (!from || date >= from) && (!until || date <= until) &&
      (filter === 'all' || (filter === 'history' ? ['work', 'two', 'abc'].includes(t.kind) : t.status === filter)) && (route.kind === 'all' || t.kind === route.kind);
  });
  $('taskResultCount').textContent = `显示 ${tasks.length} / ${state.tasks.length} 项`;
  if (route.kind !== 'all') area.append(button('显示全部任务', () => navigate('tasks'), 'task-action-btn clear-kind'));
  for (const task of tasks) area.append(taskRow(task));
  for (const tab of document.querySelectorAll('.filter-btn')) tab.setAttribute('aria-pressed', String(tab.dataset.filter === filter));
  if (!tasks.length) area.append(el('p', 'empty-state', '当前筛选下没有任务。'));
  const templates = $('workflowTemplates'); templates.replaceChildren();
  if (state.settings.task_config_error) templates.append(el('p', 'error-text', state.settings.task_config_error));
  for (const task of state.templates) {
    const row = el('div', 'template-row'), content = el('div');
    const schedule = task.schedule;
    content.append(el('strong', '', task.next_display_name || task.title), el('p', 'form-help', task.channel_label + ' · ' +
      (task.next_display_name || schedule.kind === 'manual' ? '按需启动' : schedule.kind === 'daily' ? '每天 ' + schedule.at + '（北京时间）' : '单次 ' + formatTime(schedule.at) + '（北京时间）')));
    const start = button('安排任务', async () => {
      openTaskModal(task.id);
    }, 'btn btn-primary');
    start.disabled = !task.can_start;
    if (task.start_hint) content.append(el('p', 'form-help', task.start_hint));
    row.append(content, start); templates.append(row);
  }
  if (!state.templates.length) templates.append(el('p', 'form-help', '尚无可用 AI 任务模板。'));
}

function showDetail(id) {
  const task = selectedTask(id);
  if (!task) { toast('找不到这项任务。'); return; }
  $('workTitle').textContent = task.display_name || task.title;
  const signature = JSON.stringify(task);
  if (signature !== detailSnapshot) {
    detailSnapshot = signature;
    const area = $('workDetail'); area.replaceChildren();
    area.append(el('p', 'status-badge', task.state_label), el('p', 'detail-text', task.summary));
    area.append(el('p', 'form-help', '任务编号：' + task.code + ' · 创建时间：' + formatTime(task.created_at)));
    if (task.can_replace) {
      if (task.kind === 'ai' && task.state === 'blocked') area.append(el('p', 'form-help', '如需更正文件，请上传修正版，并选择需要替换的员工输入文件。保存后将重新检查和核对。'));
      area.append(button('上传文件更正任务', () => openFileReplace(task)));
    }
    if (task.file_versions?.length) {
      const history = el('details'); history.append(el('summary', '', '文件更正记录（' + task.file_versions.length + '）'));
      for (const file of task.file_versions) {
        const row = el('div', 'file-version');
        row.append(el('p', '', formatTime(file.created) + ' · ' + file.name), el('p', 'form-help', file.reason),
          button('下载此版', () => download('/api/console/file/version?id=' + file.id, file.name), 'task-action-btn'));
        history.append(row);
      }
      area.append(history);
    }
    if (task.error) area.append(el('p', 'error-text', task.error));
    if (task.channel_label) area.append(el('p', 'form-help', task.channel_label));
    if (task.actions.includes('close')) {
      const close = button('关闭任务', async () => {
        if (!window.confirm('关闭后将停止后续流程和待发送通知，已经发送的消息无法撤回。确定关闭吗？')) return;
        await action('/api/console/task/close', {id: task.id, state: task.state,
          ...(task.revision !== undefined ? {revision: task.revision} : {}), request_id: crypto.randomUUID()}, close);
      });
      area.append(close);
    }
    if (task.kind === 'ai') {
      area.append(el('p', 'detail-text', task.reply_hint));
      area.append(button('查看任务对话', () => navigate('chat', {task: task.id}), 'btn btn-primary'));
      if (task.actions.includes('retry')) {
        const b = button('重试最新失败回复', () => action('/api/ai-dispatch/retry-reply', {
          code: task.id.slice(3), request_id: task.retry_request_id}, b)); area.append(b);
      }
      if (task.download) area.append(button('下载核查结果', () => download(task.download, '商品交集核查.csv')));
      const notices = el('details'); notices.open = true; notices.append(el('summary', '', '任务通知与协作内容'));
      for (const m of task.messages) notices.append(message(m)); area.append(notices);
      if (task.project_brief) {
        const brief = el('details'); brief.append(el('summary', '', '本次使用的项目说明'), el('pre', '', task.project_brief.text)); area.append(brief);
      }
      const records = el('details'); records.append(el('summary', '', '处理记录'));
      for (const event of task.events) records.append(el('p', 'form-help', formatTime(event.created) + ' · ' + event.detail)); area.append(records);
    } else if (task.kind === 'invoice') {
      area.append(button('查看任务对话', () => navigate('chat', {task: task.id}), 'btn btn-primary'));
      if (task.reply_hint) area.append(el('p', 'detail-text', task.reply_hint));
      if (task.extraction) {
        const details = el('details'); details.append(el('summary', '', '本次取数说明'));
        for (const [key, value] of Object.entries(task.extraction)) {
          details.append(el('p', 'form-help', key + '：' + (Array.isArray(value) ? value.join('、') : value)));
        }
        if (task.comparison) details.append(el('p', 'form-help', '与上次相比：' + task.comparison));
        area.append(details);
      }
      if (task.download) area.append(button('下载发票样本', () => download(task.download, task.display_name + '（模拟日期）.xlsx')));
      const notices = el('details'); notices.open = true; notices.append(el('summary', '', '微信通知状态'));
      for (const notice of task.notices) {
        const labels = {manager:'负责人确认',employee1_start:'员工1任务通知',employee1_review:'员工1核对文件',
          employee1_review_old_v1:'旧版文件（已更正）',employee2_review:'员工2核对文件',manager_done:'负责人完成汇报',failure:'失败通知'};
        const states = {submitted_unverified:'已提交微信，送达未确认',unknown:'发送结果未知',blocked:'发送受阻',pending:'等待发送',sending:'正在提交'};
        notices.append(el('p', 'form-help', (labels[notice.kind] || notice.kind) + '：' + (states[notice.state] || notice.state)));
      }
      area.append(notices);
    } else if (task.kind === 'order') {
      area.append(button('查看任务对话', () => navigate('chat', {task: task.id}), 'btn btn-primary'));
      if (task.linked_id) area.append(button('打开已启动的任务', () => navigate('tasks', {task: task.linked_id})));
      if (['scheduled','open','blocked'].includes(task.state)) {
        const cancel = button('取消安排', () => action('/api/console/task/update', {id:task.id, revision:task.revision, action:'cancel'}, cancel)); area.append(cancel);
      }
      if (task.state === 'blocked') {
        const retry = button('重试派发', () => action('/api/console/task/update', {id:task.id, revision:task.revision, action:'retry'}, retry)); area.append(retry);
      }
      for (const person of task.staff) {
        const row = el('div', 'staff-progress');
        row.append(el('strong','',person.name), el('p','form-help',({pending:'未接单',acknowledged:'已接单',done:'已完成'})[person.state] + ' · ' + (({pending:'等待派发',blocked:'派发受阻',unknown:'发送结果未知',sending:'正在发送',submitted_unverified:'已提交微信，送达未确认'})[person.delivery] || person.delivery)));
        if (person.detail) row.append(el('p','form-help',person.detail));
        if (task.state === 'open' && person.state !== 'done') {
          for (const [operation,label] of [['ack','代记已接单'],['done','代记已完成'], ...(person.delivery === 'blocked' ? [['retry-delivery','重试派单']] : [])]) {
            const b = button(label, () => action('/api/console/task/update', {id:task.id, revision:task.revision, account_id:person.account_id, action:operation}, b), 'task-action-btn'); row.append(b);
          }
        }
        area.append(row);
      }
    } else if (task.kind !== 'reminder') {
      for (const report of task.reports) if (Object.keys(report).length) area.append(reportView(report));
      for (const notice of task.notices) {
        const label = ({submitted_unverified:'已提交微信，送达未确认',unknown:'发送结果未知',blocked:'发送受阻',pending:'等待发送',sending:'正在提交',demo_delivered:'本地模拟消息',delivered_local:'已投递本地收件箱'})[notice.state] || notice.state;
        area.append(el('p', 'form-help', '通知：' + label + ' · ' + notice.detail));
      }
      for (const asset of task.assets) area.append(button('下载 ' + asset.name, () => download(asset.url, asset.name)));
      for (const kind of task.actions) {
        if (kind === 'close') continue;
        const label = kind === 'reject' ? '要求修改，停止本版投递' : task.kind === 'work' ? '已核对，同意投递本地收件箱' : '已核对，继续下一阶段';
        const b = button(label, () => action('/api/console/legacy-action', {id: task.id, state: task.state, action: kind, request_id: crypto.randomUUID()}, b)); area.append(b);
      }
      area.append(el('p', 'form-help', '此项保留原有模拟角色、成果校验与交接规则。'));
    } else {
      area.append(el('p', 'detail-text', '截止时间：' + formatTime(task.due)));
      for (const [kind, label] of [['done', '完成'], ['snooze', '10分钟后提醒'], ['cancel', '取消待办']]) {
        if (task.actions.includes(kind)) { const b = button(label, () => reminderAction(task, kind, b)); area.append(b); }
      }
    }
  }
  if (!$('workModal').open) $('workModal').showModal();
}
function message(value) {
  const block = el('div', 'conversation-message ' + value.role);
  block.append(el('p', 'form-help', [(value.role === 'user' ? '人员回复' : '知事'), value.status, value.estimated ? '历史记录，按阶段还原顺序' : formatTime(value.at || value.created_at)].filter(Boolean).join(' · ')),
    el('p', 'message-text', value.text));
  if (value.detail) block.append(el('p', 'form-help', value.detail));
  return block;
}
function reportView(report) {
  const labels = {input_rows:'输入行数', output_rows:'有效行数', duplicate_rows:'去重行数', invalid_rows:'异常行数',
    total_amount:'合计金额', rows:'行数', department:'部门', departments:'部门汇总', summary:'结果说明', passed:'校验通过',
    input_sha256:'输入文件摘要', output_sha256:'成果文件摘要', sha256:'文件摘要'};
  const box = el('dl', 'report-grid');
  for (const [key, value] of Object.entries(report)) {
    if (value && typeof value === 'object') {
      const details = el('details'); details.append(el('summary', '', labels[key] || key));
      for (const row of Array.isArray(value) ? value : [value]) details.append(typeof row === 'object' ? reportView(row) : el('p', '', String(row)));
      box.append(details);
    } else box.append(el('dt', '', labels[key] || key), el('dd', '', typeof value === 'boolean' ? value ? '是' : '否' : String(value)));
  }
  return box;
}
function saveDraft() {
  drafts.set(chatTarget, {text: $('chatInput').value, revision: chatRevision});
  saveDraftsToStorage(); // 自动保存到localStorage
}
function chooseConversation(target) {
  if (chatTarget !== target) {
    saveDraft(); chatTarget = target;
    const draft = drafts.get(target);
    $('chatInput').value = draft?.text || '';
    chatRevision = draft?.revision ?? selectedTask(target)?.revision ?? null;
    chatSnapshot = '';
  }
  renderChat();
}
function renderChat() {
  if (!state) return;
  const select = $('chatTarget'), options = [['assistant', '日常待办助理'], ...state.tasks.filter(t => t.messages).map(t => [t.id, (t.display_name || t.title) + ' · ' + t.code + ' · ' + t.state_label])];
  if (JSON.stringify(options) !== select.dataset.options) {
    select.replaceChildren(...options.map(([value, label]) => { const option = el('option', '', label); option.value = value; return option; }));
    select.dataset.options = JSON.stringify(options);
  }
  select.value = chatTarget;
  const task = selectedTask(chatTarget);
  if (!$('chatInput').value) chatRevision = task?.revision ?? null;
  const stale = task && chatRevision !== task.revision;
  const canReply = chatTarget === 'assistant' || Boolean(task?.actions.includes('reply'));
  $('chatContextHint').textContent = stale ? '任务已进入新阶段。请先清空旧草稿、查看最新通知，再回复。' : task?.reply_hint || (task ? '此处查看对话记录；员工请从绑定微信回复，也可在任务详情记录处理情况。' : chatTarget === 'assistant' ? '管理普通待办；协作记录请在上方选择具体任务。' : '任务不可用。');
  $('chatInput').disabled = !canReply;
  $('sendBtn').disabled = !canReply || stale || sending;
  $('chatModeBadge').textContent = task ? 'AI 任务协作' : state.settings.reminder.mode === 'model' ? '待办 AI 模式' : '待办规则模式';
  document.querySelector('.quick-phrases').hidden = chatTarget !== 'assistant';
  const messages = task ? task.messages || [] : chatTarget === 'assistant' ? state.messages : [];
  const signature = JSON.stringify(messages);
  if (chatSnapshot !== signature) {
    chatSnapshot = signature;
    const area = $('chatMessages'), atBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 120;
    area.replaceChildren(...messages.map(message));
    if (!messages.length) area.append(el('p', 'empty-state', '还没有对话记录。'));
    if (atBottom) area.scrollTop = area.scrollHeight;
  }
}
async function sendMessage(text) {
  if (sending || !text.trim()) return false;
  const target = chatTarget, revision = chatRevision;
  sending = true; renderChat();
  try {
    const result = await api('/api/console/chat', {target, revision, text: text.trim(), request_id: crypto.randomUUID()});
    if (chatTarget === target) {
      $('chatInput').value = '';
      // 发送成功后自动聚焦输入框，方便继续对话
      setTimeout(() => $('chatInput').focus(), 100);
    }
    drafts.delete(target);
    saveDraftsToStorage(); // 同步删除localStorage中的草稿
    if (target !== 'assistant') toast(result.reply);
    await refresh(true); return true;
  } catch (error) { toast(error.message); return false; }
  finally { sending = false; renderChat(); }
}

const configIds = ['workflowBase', 'workflowModel', 'workflowKey', 'workflowBrief', 'workflowJson'];
function configData() { return {base_url: $('workflowBase').value.trim(), model: $('workflowModel').value.trim(), api_key: $('workflowKey').value,
  disable_thinking: $('workflowBrief').checked, json_mode: $('workflowJson').checked}; }
async function modelAction(kind) {
  if (configBusy) return;
  if (kind !== 'save' && configDirty) { $('workflowFeedback').textContent = '请先保存当前配置，再测试或清除密钥。'; return; }
  configBusy = true;
  for (const field of $('workflowModelForm').querySelectorAll('input,button')) field.disabled = true;
  $('workflowFeedback').textContent = kind === 'test' ? '正在测试已保存的接口，请稍候…' : '正在保存…';
  try {
    const result = await api(kind === 'test' ? '/api/ai-dispatch/model/test' : '/api/ai-dispatch/model',
      kind === 'test' ? {} : {...configData(), ...(kind === 'clear' ? {api_key: '', clear_key: true} : {})});
    if (kind !== 'test') { $('workflowKey').value = ''; configDirty = false; }
    $('workflowFeedback').textContent = kind === 'test' ? result.detail : kind === 'clear' ? '已清除保存的密钥。' : '已保存并应用，可测试连接。';
    await refresh(true);
  } catch (error) { $('workflowFeedback').textContent = error.message; }
  finally { configBusy = false; for (const field of $('workflowModelForm').querySelectorAll('input,button')) field.disabled = false; }
}
function renderSettings() {
  const model = state.settings.workflow_model, settings = state.settings.reminder;
  $('workflowModelStatus').textContent = model.error ? '配置需要检查' : model.test ? model.test.ok ? '连接测试通过' : '连接测试失败' : model.ready ? '已配置 · 待测试' : '待配置';
  $('workflowKeyHint').textContent = model.has_key ? '已保存密钥；留空继续使用，更换接口地址需重新填写。' : '尚未保存密钥。';
  if (!configDirty && !configBusy) {
    $('workflowBase').value = model.base_url || ''; $('workflowModel').value = model.model || 'qwen3.8-max';
    $('workflowBrief').checked = !!model.disable_thinking; $('workflowJson').checked = !!model.json_mode;
  }
  if (!configDirty && !configBusy && (model.error || model.test?.detail)) $('workflowFeedback').textContent = model.error || model.test.detail;
  $('modelStatusBadge').textContent = settings.model_ready ? '待办接口已配置' : '使用规则即可';
  if (!reminderDirty) {
    $('modeSelect').value = settings.mode;
    $('modelSelect').replaceChildren(...settings.aliases.map(alias => { const option = el('option', '', alias); option.value = alias; return option; }));
    $('modelSelect').value = settings.model;
  }
  const tbody = $('logsTable'); tbody.replaceChildren();
  for (const log of state.outbox) {
    const tr = el('tr'); tr.append(el('td', '', log.text), el('td', '', formatTime(log.created_at)),
      el('td', '', ({submitted_unverified: '已提交微信，送达未确认', demo_delivered: '演示已生成', unknown: '发送结果未知', pending: '等待发送', blocked: '发送受阻', sending: '正在提交', cancelled: '已取消'})[log.state] || log.state)); tbody.append(tr);
  }
}
async function refresh(force = false) {
  if (refreshing) { await refreshing; if (force) return refresh(); return; }
  refreshing = (async () => {
  try {
    state = await api('/api/console');
    $('serviceStatus').textContent = '服务已连接'; $('serviceIndicator').className = 'status-dot active'; $('connectionError').hidden = true;
    $('currentTime').textContent = formatTime(state.now) + ' 北京时间';
    renderDashboard(); renderTasks(); renderChat(); renderSettings(); renderAdmin(state);
    if ($('workModal').open && route.task) showDetail(route.task);
    if (route.page === 'tasks' && route.task && !$('workModal').open) showDetail(route.task);
  } catch (error) {
    $('serviceStatus').textContent = '连接异常'; $('serviceIndicator').className = 'status-dot error';
    $('connectionError').textContent = error.message + ' 数据可能尚未更新，将自动重连。'; $('connectionError').hidden = false;
  }
  })();
  try { await refreshing; } finally { refreshing = false; }
}
function updateTaskType() {
  const value = $('taskTemplate').value, custom = value === 'custom' || value.startsWith('copy:');
  $('taskStaffField').hidden = !custom;
  $('taskRoleFields').hidden = custom || value === 'reminder';
  $('taskCreateHint').textContent = value === 'reminder' ? '首次提醒时间也作为截止时间保存' : custom ? '到时间向所选员工派单；每位员工可从自己的微信接单和完成，也可由管理员在任务详情代记。' : '到时间按原有流程启动，使用下方选定的员工分工。电脑和服务需保持运行。';
  if (value.startsWith('copy:')) {
    const task = selectedTask(value.slice(5)); $('taskTitle').value = task?.title || '';
    for (const input of $('taskStaff').querySelectorAll('input')) input.checked = task?.staff?.some(p => p.account_id === input.value) || false;
  } else if (!['reminder','custom'].includes(value)) $('taskTitle').value = state.templates.find(t => t.id === value)?.title || '';
}
async function openTaskModal(template = 'reminder') {
  if (!state) { toast('正在读取任务，请稍后重试。'); return; }
  await refresh(true);
  const select = $('taskTemplate'); select.replaceChildren();
  const options = [['reminder','普通待办提醒'],['custom','自定义员工任务'], ...state.templates.map(t => [t.id,'已有任务：' + t.title]), ...state.tasks.filter(t => t.kind === 'order' && t.template === 'custom' && t.staff?.length).slice(0,30).map(t => ['copy:' + t.id,'复用任务：' + t.title])];
  for (const [value,label] of options) { const option = el('option','',label); option.value = value; select.append(option); }
  select.value = typeof template === 'string' ? template : 'reminder';
  $('taskStaff').replaceChildren(); $('taskRoleFields').replaceChildren();
  for (const person of state.employees) {
    const label = el('label','checkbox'), input = el('input'); input.type = 'checkbox'; input.value = person.id;
    label.append(input,el('span','',person.name)); $('taskStaff').append(label);
  }
  if (!state.employees.length) $('taskStaff').append(el('p','hint','请先在账户管理中添加员工。'));
  for (const [role,label] of [['manager','负责人'],['employee1','员工1（准备及核对）'],['employee2','员工2（接手及复核）']]) {
    const field = el('div','field'), labelNode = el('label','label',label), input = el('select','select');
    input.id = 'assign-' + role; labelNode.htmlFor = input.id;
    for (const person of state.employees) { const option = el('option','',person.name); option.value = person.id; input.append(option); }
    input.value = role === 'employee2' ? 'existing-employee2' : 'existing-primary';
    field.append(labelNode,input); $('taskRoleFields').append(field);
  }
  $('taskFormError').textContent = ''; $('taskTitle').value = ''; updateTaskType();
  const target = new Date(Date.now() + 10 * 60000 + 8 * 3600000);
  $('taskDue').value = target.toISOString().slice(0, 16);
  $('taskModal').showModal(); $('taskTitle').focus();
}
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault(); $('mainContent').focus();
});
for (const id of ['quickAddTask', 'addTaskBtn']) $(id).addEventListener('click', openTaskModal);
$('closeModal').addEventListener('click', () => $('taskModal').close());
$('closeWorkModal').addEventListener('click', () => navigate('tasks', {kind: route.kind === 'all' ? null : route.kind}));
$('workModal').addEventListener('cancel', () => navigate('tasks', {kind: route.kind === 'all' ? null : route.kind}));
$('taskForm').addEventListener('submit', async event => {
  event.preventDefault(); const b = $('taskForm').querySelector('button[type=submit]');
  const template = $('taskTemplate').value, data = {title: $('taskTitle').value.trim(), due: $('taskDue').value + ':00+08:00', request_id: crypto.randomUUID()};
  if (template === 'reminder') data.action = 'create';
  else {
    data.template = template.startsWith('copy:') ? 'custom' : template;
    data.assignees = [...$('taskStaff').querySelectorAll('input:checked')].map(i => i.value);
    data.roles = Object.fromEntries(['manager','employee1','employee2'].map(role => [role,$('assign-' + role).value]));
  }
  b.disabled = true; $('taskFormError').textContent = '';
  try {
    const result = await api(template === 'reminder' ? '/api/console/reminder' : '/api/console/task/create', data);
    $('taskModal').close(); toast(result.reply); await refresh(true);
    if (result.id) navigate('tasks', {task:result.id});
  } catch (error) { $('taskFormError').textContent = error.message; }
  finally { b.disabled = false; }
});
$('taskTemplate').addEventListener('change', updateTaskType);
for (const id of ['taskSearch','taskStartDate','taskEndDate']) $(id).addEventListener('input', renderTasks);
$('taskSearchForm').addEventListener('submit', event => event.preventDefault());
$('clearTaskSearch').addEventListener('click', () => { for (const id of ['taskSearch','taskStartDate','taskEndDate']) $(id).value = ''; renderTasks(); });

function openFileReplace(task) {
  replacingTask = task;
  $('fileReplaceForm').reset(); $('fileReplaceError').textContent = '';
  const slots = task.kind === 'invoice' ? [['result','发票导出文件（Excel）']] : [['employee1','员工1输入文件（Excel）'],['employee2','员工2输入文件（Excel）'], ...(task.download ? [['result','核查结果（CSV，需通过原有校验）']] : [])];
  const placeholder = el('option', '', '请选择需要替换的文件'); placeholder.value = '';
  $('fileSlot').replaceChildren(placeholder, ...slots.map(([value,label]) => {const option = el('option','',label); option.value = value; return option;}));
  updateFileAccept(); $('fileModal').showModal();
}
function updateFileAccept() { $('replacementFile').accept = replacingTask?.kind === 'ai' && $('fileSlot').value === 'result' ? '.csv' : '.xlsx'; }
$('fileSlot').addEventListener('change', updateFileAccept);
for (const id of ['closeFileModal','cancelFileReplace']) $(id).addEventListener('click', () => $('fileModal').close());
$('fileReplaceForm').addEventListener('submit', async event => {
  event.preventDefault(); const file = $('replacementFile').files[0], task = replacingTask, b = $('submitFileReplace');
  if (!file || !task) return;
  if (file.size > 20 * 1024 * 1024) { $('fileReplaceError').textContent = '文件超过 20 MB，请缩小后重试。'; return; }
  b.disabled = true; $('fileReplaceError').textContent = '';
  try {
    const content = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = () => reject(Error('文件无法读取，请重新选择。')); reader.readAsDataURL(file); });
    const result = await api('/api/console/file/replace', {id:task.id, state:task.state, revision:task.revision, updated_at:task.updated_at,
      file_revision:task.file_revision, slot:$('fileSlot').value, name:file.name, reason:$('fileReason').value.trim(), content, request_id:crypto.randomUUID()});
    $('fileModal').close(); toast(result.reply); await refresh(true);
  } catch (error) { $('fileReplaceError').textContent = error.message; }
  finally { b.disabled = false; }
});
// 修复：HTML中使用的是.filter-btn类，不是.filter-tab
for (const tab of document.querySelectorAll('.filter-btn')) tab.addEventListener('click', () => {
  filter = tab.dataset.filter;
  for (const other of document.querySelectorAll('.filter-btn')) other.classList.toggle('active', other === tab);
  renderTasks();
});
$('chatTarget').addEventListener('change', () => navigate('chat', {task: $('chatTarget').value === 'assistant' ? null : $('chatTarget').value}));
// 改进：输入时自动保存草稿到内存
$('chatInput').addEventListener('input', () => {
  saveDraft();
  renderChat();
});
// 新增：支持Ctrl+Enter快捷键发送消息
$('chatInput').addEventListener('keydown', (event) => {
  if (event.ctrlKey && event.key === 'Enter' && !$('sendBtn').disabled) {
    event.preventDefault();
    sendMessage($('chatInput').value);
  }
});
$('chatForm').addEventListener('submit', event => { event.preventDefault(); if (!$('sendBtn').disabled) sendMessage($('chatInput').value); });
for (const b of document.querySelectorAll('[data-say],[data-draft]')) b.addEventListener('click', () => {
  navigate('chat'); chooseConversation('assistant');
  if (b.dataset.draft) { $('chatInput').value = b.dataset.draft; $('chatInput').focus(); } else sendMessage(b.dataset.say);
});
$('testReminderBtn').addEventListener('click', () => action('/api/console/reminder', {action: 'create', title: '检查试点效果', due: new Date(Date.now() + 10000).toISOString(), request_id: crypto.randomUUID()}, $('testReminderBtn')));

// 新增：配置模板预设
const configPresets = {
  'openai': { base: 'https://api.openai.com/v1', model: 'gpt-4o' },
  'qwen': { base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-max' },
  'deepseek': { base: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  'litellm': { base: 'http://127.0.0.1:4000/v1', model: 'gpt-4o' }
};
$('workflowPreset').addEventListener('change', () => {
  const preset = configPresets[$('workflowPreset').value];
  if (preset) {
    $('workflowBase').value = preset.base;
    $('workflowModel').value = preset.model;
    configDirty = true;
    $('workflowFeedback').textContent = '已应用模板，请填写API Key并保存。';
  }
});

// 新增：显示/隐藏密钥
$('workflowKeyToggle').addEventListener('click', () => {
  const input = $('workflowKey');
  const btn = $('workflowKeyToggle');
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = '隐藏';
  } else {
    input.type = 'password';
    btn.textContent = '显示';
  }
});

for (const id of configIds) $(id).addEventListener('input', () => { configDirty = true; $('workflowFeedback').textContent = '配置尚未保存。'; });
$('workflowModelForm').addEventListener('submit', event => { event.preventDefault(); modelAction('save'); });
$('workflowTest').addEventListener('click', () => modelAction('test'));
$('workflowClear').addEventListener('click', () => modelAction('clear'));
for (const id of ['modelSelect', 'modeSelect']) $(id).addEventListener('change', () => { reminderDirty = true; });
$('settingsForm').addEventListener('submit', async event => {
  event.preventDefault(); const b = $('settingsForm').querySelector('button');
  const result = await action('/api/settings', {mode: $('modeSelect').value, model: $('modelSelect').value}, b);
  if (result) { reminderDirty = false; renderSettings(); }
});
$('message-preview').addEventListener('toggle', async () => {
  if (!$('message-preview').open || previewLoaded) return;
  try {
    const preview = await api('/api/ai-dispatch/preview');
    $('message-preview-body').replaceChildren(...preview.messages.map(m => message({role: 'assistant', text: m.text, status: m.label})));
    previewLoaded = true;
  } catch (error) { $('message-preview-body').textContent = error.message; }
});
window.addEventListener('hashchange', applyRoute);

// 页面初始化：加载草稿
loadDraftsFromStorage();
const initialDraft = drafts.get(chatTarget);
$('chatInput').value = initialDraft?.text || '';
chatRevision = initialDraft?.revision ?? null;

initAccounts({toast});
initAdmin({toast});
applyRoute(); refresh();

// 优化：使用页面可见性API，避免后台标签页无效轮询
let refreshTimer = setInterval(refresh, 2500);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // 页面隐藏时停止轮询
    clearInterval(refreshTimer);
  } else {
    // 页面重新可见时立即刷新并恢复轮询
    refresh();
    refreshTimer = setInterval(refresh, 2500);
  }
});
