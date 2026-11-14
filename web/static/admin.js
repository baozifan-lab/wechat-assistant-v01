// Presentation and navigation only. Business mutations remain in workbench.js.
import {hashFor, routeFor} from './navigation.js';

const $ = id => document.getElementById(id);
const make = (tag, className, text) => {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const pageNames = {dashboard: '工作台', tasks: '任务清单', chat: '对话助理', accounts: '账户管理', settings: '系统设置'};
const statusNames = {open: '进行中', blocked: '需处理', done: '已完成', cancelled: '已取消'};
const dayKey = value => new Intl.DateTimeFormat('sv-SE', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(value));
const shortTime = value => value ? new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value)) : '未设置';
const greetingHour = new Intl.DateTimeFormat('en-GB', {timeZone: 'Asia/Shanghai', hour: '2-digit', hourCycle: 'h23'});
function renderGreeting(value = new Date()) {
  const hour = Number(greetingHour.format(new Date(value)));
  $('dashboardGreeting').textContent = hour >= 5 && hour < 11 ? '早上好'
    : hour >= 11 && hour < 14 ? '中午好'
    : hour >= 14 && hour < 18 ? '下午好' : '晚上好';
}
let latest, view = 'all', signature = '', notify = () => {}, activeResult = -1, currentPage;

function stored(key) { try { return localStorage.getItem(key); } catch { return null; } }
function store(key, value) { try { localStorage.setItem(key, value); } catch { /* Preferences are optional. */ } }
function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('themeToggle')?.setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#181715' : '#faf9f5');
}
setTheme(stored('zhishi-theme') === 'dark');

function setSidebar(open) {
  const wasOpen = document.body.classList.contains('mobile-nav-open');
  const sidebar = $('appSidebar');
  if (matchMedia('(max-width: 720px)').matches) {
    document.body.classList.toggle('mobile-nav-open', open);
    document.body.classList.remove('sidebar-collapsed');
    sidebar.inert = !open;
    $('mainContent').inert = open;
    if (open) {
      sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true');
      if (!wasOpen) sidebar.querySelector('.nav-link').focus();
    } else {
      sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal');
      if (wasOpen) $('sidebarToggle').focus();
    }
  } else {
    document.body.classList.toggle('sidebar-collapsed', !open);
    document.body.classList.remove('mobile-nav-open');
    sidebar.inert = !open;
    $('mainContent').inert = false;
    sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal');
    store('zhishi-sidebar', open ? 'open' : 'closed');
  }
  $('sidebarToggle').setAttribute('aria-expanded', String(open));
  $('sidebarToggle').setAttribute('aria-label', open ? '收起侧栏' : '展开侧栏');
}
function updateRoute() {
  const page = routeFor(location.pathname, location.hash).page;
  if (currentPage !== undefined && currentPage !== page) window.scrollTo(0, 0);
  currentPage = page;
  document.title = `${pageNames[page]} · 知事`;
  if ($('currentPageName')) $('currentPageName').textContent = pageNames[page];
  for (const link of document.querySelectorAll('[data-top-page]')) {
    if (link.dataset.topPage === page) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  if (matchMedia('(max-width: 720px)').matches) setSidebar(false);
}
function visibleTasks() {
  return (latest?.tasks || []).filter(t => view === 'all' || (view === 'reminder' ? t.kind === 'reminder' : t.kind !== 'reminder'));
}
function renderOverview() {
  const tasks = visibleTasks(), now = new Date(latest.now);
  const counts = Object.fromEntries(Object.keys(statusNames).map(status => [status, tasks.filter(t => t.status === status).length]));
  $('stat-pending').textContent = counts.open;
  $('stat-completed').textContent = counts.done;
  $('stat-scheduled').textContent = counts.blocked;
  $('stat-scheduled').closest('.metric').classList.toggle('needs-attention', counts.blocked > 0);
  const date = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'long'}).format(now);
  $('greetingText').textContent = `${date} · ${counts.open} 件进行中，${counts.blocked} 件需要处理`;
  const days = Array.from({length: 7}, (_, i) => {
    const date = new Date(now.getTime() - (6 - i) * 86400000);
    const key = dayKey(date);
    return {key, label: key.slice(5).replace('-', '/'), count: tasks.filter(t => dayKey(t.created_at) === key).length};
  });
  const max = Math.max(4, ...days.map(d => d.count));
  const chart = $('activityChart'); chart.replaceChildren();
  const axis = make('div', 'chart-axis');
  for (const n of [max, Math.round(max * .75), Math.round(max * .5), Math.round(max * .25), 0]) axis.append(make('span', '', n));
  const plot = make('div', 'chart-plot');
  for (const day of days) {
    const column = make('div', 'chart-column'), track = make('div', 'chart-track');
    const bar = make('div', 'chart-bar' + (day.count === 0 ? ' is-zero' : ''));
    bar.style.height = `${day.count / max * 100}%`;
    bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', `${day.key} 新建 ${day.count} 项任务`);
    bar.title = `${day.key}：${day.count} 项`; track.append(bar);
    if (day.count) track.append(make('span', 'bar-value', day.count));
    column.append(track, make('span', 'chart-date', day.label)); plot.append(column);
  }
  chart.append(axis, plot);
  const total = days.reduce((sum, d) => sum + d.count, 0);
  $('activitySummary').textContent = `近 7 天共新增 ${total} 项`;
  const distribution = $('statusDistribution'); distribution.replaceChildren();
  for (const [status, label] of Object.entries(statusNames)) {
    const count = tasks.filter(t => t.status === status).length;
    const row = make('div', 'distribution-row');
    const labelRow = make('div', 'distribution-label');
    const name = make('span', ''); name.append(make('i', `state-dot state-${status}`), document.createTextNode(label));
    labelRow.append(name, make('strong', '', String(count)));
    const meter = make('div', 'distribution-meter'), fill = make('span', `distribution-fill state-${status}`);
    fill.style.width = `${tasks.length ? count / tasks.length * 100 : 0}%`;
    meter.append(fill); row.append(labelRow, meter); distribution.append(row);
  }
  renderRecent(tasks);
}
function renderRecent(tasks) {
  const area = $('recentTasks'); area.replaceChildren();
  $('recentTaskSummary').textContent = `共 ${tasks.length} 项${view === 'reminder' ? '待办提醒' : view === 'collaboration' ? '协作任务' : '任务'}，优先展示进行中的工作`;
  if (!tasks.length) {
    const empty = make('div', 'dashboard-empty');
    empty.append(make('strong', '', '从一件小事开始'), make('p', '', '创建待办，或到任务清单安排一次协作。'));
    const add = make('button', 'btn-secondary', '新建第一项任务'); add.type = 'button';
    add.addEventListener('click', () => $('quickAddTask').click()); empty.append(add); area.append(empty); return;
  }
  const wrap = make('div', 'recent-table-wrap'), table = make('table', 'recent-table');
  const caption = make('caption', 'sr-only', '最近任务：任务编号、名称、状态、负责人、截止时间和详情'); table.append(caption);
  const head = make('thead', ''), headRow = make('tr', '');
  for (const title of ['任务编号', '任务名称', '状态', '负责人', '截止时间', '']) {
    const cell = make('th', '', title); cell.scope = 'col';
    if (!title) cell.append(make('span', 'sr-only', '操作')); headRow.append(cell);
  }
  head.append(headRow); table.append(head);
  const body = make('tbody', '');
  const rank = {blocked: 0, open: 1, done: 2, cancelled: 3};
  for (const task of [...tasks].sort((a, b) => (rank[a.status] ?? 4) - (rank[b.status] ?? 4) || new Date(b.created_at) - new Date(a.created_at)).slice(0, 5)) {
    const row = make('tr', '');
    row.append(make('td', 'recent-code', task.code || task.id));
    const title = make('td', 'recent-name');
    const link = make('a', '', task.display_name || task.title); link.href = hashFor('tasks', {task: task.id});
    title.append(make('span', 'kind-tag', task.kind_label), link); row.append(title);
    const status = make('td', ''); status.append(make('span', 'table-status status-' + task.status, task.state_label || statusNames[task.status])); row.append(status);
    row.append(make('td', 'recent-owner', task.owner || '—'), make('td', 'recent-due', task.due ? shortTime(task.due) : '—'));
    const action = make('td', 'recent-action'), details = make('a', 'row-detail', '查看');
    details.href = hashFor('tasks', {task: task.id}); details.setAttribute('aria-label', `查看详情：${task.display_name || task.title}`);
    action.append(details); row.append(action); body.append(row);
  }
  table.append(body); wrap.append(table); area.append(wrap);
}
function renderSearch() {
  const query = $('commandInput').value.trim().toLowerCase(), results = $('commandResults');
  results.replaceChildren(); activeResult = -1;
  const pages = Object.entries(pageNames).filter(([, name]) => !query || name.includes(query));
  if (pages.length) results.append(make('p', 'command-group', '前往页面'));
  for (const [page, name] of pages) {
    const link = make('a', 'command-result', name); link.href = hashFor(page);
    link.append(make('span', 'command-result-meta', '页面')); results.append(link);
  }
  const tasks = (latest?.tasks || []).filter(t => [t.title, t.display_name, t.code, t.id].filter(Boolean).join(' ').toLowerCase().includes(query));
  if (tasks.length) results.append(make('p', 'command-group', '任务'));
  for (const task of tasks.slice(0, 20)) {
    const link = make('a', 'command-result', task.display_name || task.title); link.href = hashFor('tasks', {task: task.id});
    link.append(make('span', 'command-result-meta', task.state_label)); results.append(link);
  }
  if (!pages.length && !tasks.length) results.append(make('p', 'empty-state', '没有匹配的任务或页面，试试其他关键词。'));
  if (!latest) results.append(make('p', 'form-help', '任务数据正在连接，稍后可重试搜索。'));
  results.querySelectorAll('a').forEach(link => link.addEventListener('click', () => $('commandDialog').close()));
}
function openSearch() { renderSearch(); $('commandDialog').showModal(); $('commandInput').focus(); }
function exportTasks() {
  if (!latest) { notify('正在读取任务，请稍后再导出。'); return; }
  const quote = value => '"' + String(value ?? '').replace(/^[\s]*[=+@-]/, "'$&").replaceAll('"', '""') + '"';
  const rows = [['编号', '任务名称', '类型', '状态', '负责人', '创建时间（北京时间）', '截止时间（北京时间）'], ...visibleTasks().map(t => [t.code || t.id, t.display_name || t.title, t.kind_label, t.state_label, t.owner, shortTime(t.created_at), t.due ? shortTime(t.due) : ''])];
  const url = URL.createObjectURL(new Blob(['\ufeff' + rows.map(row => row.map(quote).join(',')).join('\r\n')], {type: 'text/csv;charset=utf-8'}));
  const link = make('a', ''); link.href = url; link.download = `知事任务-${dayKey(latest.now)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000); notify(`已导出 ${rows.length - 1} 项任务。`);
}
export function renderAdmin(state) {
  renderGreeting(state.now);
  latest = state;
  const next = JSON.stringify([state.tasks, dayKey(state.now), view]);
  if (signature === next) return;
  signature = next; renderOverview();
  if ($('commandDialog').open) renderSearch();
}
export function initAdmin({toast}) {
  notify = toast;
  renderGreeting();
  setInterval(renderGreeting, 60000);
  $('themeToggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark'; setTheme(dark); store('zhishi-theme', dark ? 'dark' : 'light');
  });
  setSidebar(!matchMedia('(max-width: 720px)').matches && stored('zhishi-sidebar') !== 'closed');
  $('sidebarToggle').addEventListener('click', () => setSidebar($('sidebarToggle').getAttribute('aria-expanded') !== 'true'));
  $('sidebarScrim').addEventListener('click', () => setSidebar(false));
  $('mobileSidebarClose').addEventListener('click', () => setSidebar(false));
  matchMedia('(max-width: 720px)').addEventListener('change', e => setSidebar(!e.matches && stored('zhishi-sidebar') !== 'closed'));
  $('openSearch').addEventListener('click', openSearch);
  $('closeSearch').addEventListener('click', () => $('commandDialog').close());
  $('commandInput').addEventListener('input', renderSearch);
  $('commandInput').addEventListener('keydown', event => {
    const links = [...$('commandResults').querySelectorAll('a')];
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); if (!links.length) return;
      activeResult = (activeResult + (event.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
      links.forEach((link, i) => link.classList.toggle('selected', i === activeResult));
      links[activeResult].scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter' && links.length) { event.preventDefault(); links[Math.max(0, activeResult)].click(); }
  });
  $('helpButton').addEventListener('click', () => $('helpDialog').showModal());
  $('closeHelp').addEventListener('click', () => $('helpDialog').close());
  $('exportTasks').addEventListener('click', exportTasks);
  for (const tab of document.querySelectorAll('[data-overview]')) tab.addEventListener('click', () => {
    view = tab.dataset.overview;
    for (const other of document.querySelectorAll('[data-overview]')) {
      other.classList.toggle('active', tab === other); other.setAttribute('aria-pressed', String(tab === other));
    }
    if (latest) { signature = ''; renderAdmin(latest); }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Tab' && document.body.classList.contains('mobile-nav-open')) {
      const links = [...$('appSidebar').querySelectorAll('a[href], button:not([disabled])')].filter(node => node.getClientRects().length);
      const first = links[0], last = links.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName) || event.target.isContentEditable;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if ($('commandDialog').open) $('commandDialog').close();
      else if (!document.querySelector('dialog[open]')) openSearch();
    }
    if (event.key === '?' && !typing && !document.querySelector('dialog[open]')) $('helpDialog').showModal();
    if (event.key === 'Escape' && document.body.classList.contains('mobile-nav-open')) { setSidebar(false); $('sidebarToggle').focus(); }
  });
  document.addEventListener('zhishi:page', updateRoute);
  updateRoute();
}
