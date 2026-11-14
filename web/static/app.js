import { api } from './api.js';

// 状态管理
let currentState = null;
let currentFilter = 'all';
let isBusy = false;

// 工具函数
function $(id) {
    return document.getElementById(id);
}

function formatTime(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    const now = new Date();
    const diff = now - date;
    const hours = Math.floor(diff / (1000 * 60 * 60));
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}天前`;
    if (hours > 0) return `${hours}小时前`;
    const minutes = Math.floor(diff / (1000 * 60));
    if (minutes > 0) return `${minutes}分钟前`;
    return '刚刚';
}

function formatDateTime(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    }).format(date);
}

function showToast(message, duration = 3000) {
    const toast = $('toast');
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => {
        toast.classList.remove('show');
    }, duration);
}

// 页面导航
function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            switchPage(page);
        });
    });
}

function switchPage(pageName) {
    // 更新导航状态
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.page === pageName);
    });

    // 切换页面
    document.querySelectorAll('.page').forEach(page => {
        page.classList.remove('active');
    });
    $(`page-${pageName}`).classList.add('active');

    // 特殊处理
    if (pageName === 'chat') {
        scrollChatToBottom();
    }
}

// 渲染工作台
function renderDashboard(state) {
    const openTasks = state.tasks.filter(t => t.status === 'open');
    const doneTasks = state.tasks.filter(t => t.status === 'done');
    const scheduledTasks = openTasks.filter(t => t.next_at);

    // 更新统计数据
    $('stat-pending').textContent = openTasks.length;
    $('stat-completed').textContent = doneTasks.length;
    $('stat-scheduled').textContent = scheduledTasks.length;
    $('stat-channel').textContent = state.channel === 'wechat' ? '微信' : '演示';

    // 问候语
    const hour = new Date().getHours();
    let greeting = '早上好';
    if (hour >= 12 && hour < 18) greeting = '下午好';
    else if (hour >= 18) greeting = '晚上好';
    $('greetingText').textContent = `${greeting}，今天也要加油哦 👋`;

    // 渲染最近任务
    renderRecentTasks(state.tasks.slice(0, 5));
}

function renderRecentTasks(tasks) {
    const container = $('recentTasks');
    container.innerHTML = '';

    if (tasks.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                <p>还没有任务，创建一个开始吧</p>
            </div>
        `;
        return;
    }

    tasks.forEach(task => {
        const item = document.createElement('div');
        item.className = `task-item ${task.status === 'done' ? 'completed' : ''}`;

        const checkbox = document.createElement('div');
        checkbox.className = `task-checkbox ${task.status === 'done' ? 'checked' : ''}`;
        if (task.status === 'open') {
            checkbox.onclick = () => completeTask(task.id);
        }

        const content = document.createElement('div');
        content.className = 'task-content';
        content.innerHTML = `
            <div class="task-title">#${task.id} ${escapeHtml(task.title)}</div>
            <div class="task-meta">
                <span class="task-meta-item">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10"></circle>
                        <polyline points="12 6 12 12 16 14"></polyline>
                    </svg>
                    ${formatDateTime(task.due)}
                </span>
            </div>
        `;

        item.appendChild(checkbox);
        item.appendChild(content);
        container.appendChild(item);
    });
}

// 渲染任务列表
function renderTasks(tasks) {
    const container = $('tasksList');
    container.innerHTML = '';

    // 根据筛选器过滤
    let filteredTasks = tasks;
    if (currentFilter === 'open') {
        filteredTasks = tasks.filter(t => t.status === 'open');
    } else if (currentFilter === 'done') {
        filteredTasks = tasks.filter(t => t.status === 'done');
    }

    if (filteredTasks.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M9 11l3 3L22 4"></path>
                    <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"></path>
                </svg>
                <p>没有找到任务</p>
            </div>
        `;
        return;
    }

    filteredTasks.forEach(task => {
        const item = document.createElement('div');
        item.className = `task-item ${task.status === 'done' ? 'completed' : ''}`;

        const checkbox = document.createElement('div');
        checkbox.className = `task-checkbox ${task.status === 'done' ? 'checked' : ''}`;
        if (task.status === 'open') {
            checkbox.onclick = () => completeTask(task.id);
        }

        const content = document.createElement('div');
        content.className = 'task-content';

        let statusText = task.status === 'done' ? '已完成' :
                        task.next_at ? `下次提醒 ${formatDateTime(task.next_at)}` : '等待中';

        content.innerHTML = `
            <div class="task-title">#${task.id} ${escapeHtml(task.title)}</div>
            <div class="task-meta">
                <span class="task-meta-item">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10"></circle>
                        <polyline points="12 6 12 12 16 14"></polyline>
                    </svg>
                    截止 ${formatDateTime(task.due)}
                </span>
                <span class="task-meta-item">• ${statusText}</span>
            </div>
        `;

        const actions = document.createElement('div');
        actions.className = 'task-actions';

        if (task.status === 'open') {
            actions.innerHTML = `
                <button class="task-action-btn" onclick="window.snoozeTask(${task.id}, 10)">10分钟后</button>
                <button class="task-action-btn" onclick="window.cancelTask(${task.id})">取消</button>
            `;
        }

        item.appendChild(checkbox);
        item.appendChild(content);
        item.appendChild(actions);
        container.appendChild(item);
    });
}

// 渲染聊天
function renderChat(messages) {
    const container = $('chatMessages');
    const shouldScroll = container.scrollHeight - container.scrollTop - container.clientHeight < 100;

    container.innerHTML = '';

    messages.forEach(msg => {
        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${msg.role}`;

        const avatar = document.createElement('div');
        avatar.className = 'chat-avatar';
        avatar.textContent = msg.role === 'user' ? '我' : '知';

        const content = document.createElement('div');
        content.className = 'chat-content';

        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble';
        bubble.textContent = msg.text;

        const time = document.createElement('div');
        time.className = 'chat-time';
        time.textContent = formatTime(msg.created_at);

        content.appendChild(bubble);
        content.appendChild(time);

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(content);

        container.appendChild(messageDiv);
    });

    if (shouldScroll) {
        scrollChatToBottom();
    }
}

function scrollChatToBottom() {
    const container = $('chatMessages');
    setTimeout(() => {
        container.scrollTop = container.scrollHeight;
    }, 100);
}

// 渲染设置
function renderSettings(state) {
    // 模型状态
    $('modelStatusBadge').textContent = state.settings.model_ready ? '已配置' : '未配置';
    $('modelStatusBadge').className = state.settings.model_ready ? 'status-badge' : 'status-badge status-offline';

    // 模式选择
    $('modeSelect').value = state.settings.mode;

    // 模型列表
    const modelSelect = $('modelSelect');
    if (modelSelect.options.length !== state.settings.aliases.length) {
        modelSelect.innerHTML = '';
        state.settings.aliases.forEach(alias => {
            const option = document.createElement('option');
            option.value = alias;
            option.textContent = alias;
            modelSelect.appendChild(option);
        });
    }
    modelSelect.value = state.settings.model;

    // 微信状态
    const wechatBound = state.wechat?.bound;
    const gatewayOffline = (state.wechat?.gateway_ready ?? state.wechat?.gateway_listening) !== true;

    let wechatStatus = '未连接';
    let statusClass = 'status-badge status-offline';

    if (wechatBound && !gatewayOffline) {
        wechatStatus = '已连接';
        statusClass = 'status-badge';
    } else if (wechatBound && gatewayOffline) {
        wechatStatus = ({starting: '正在启动', needs_attention: '连接异常', disabled: '未启用'})[state.wechat?.connection_state] || '网关离线';
    }

    $('wechatStatusBadge').textContent = wechatStatus;
    $('wechatStatusBadge').className = statusClass;

    // 聊天模式徽章
    $('chatModeBadge').textContent = state.settings.mode === 'model' ? 'AI模式' : '规则模式';

    // 渲染日志
    renderLogs(state.outbox);
}

function renderLogs(outbox) {
    const tbody = $('logsTable');
    tbody.innerHTML = '';

    if (outbox.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="3" style="text-align: center; padding: 2rem; color: var(--text-muted);">
                    暂无提醒记录
                </td>
            </tr>
        `;
        return;
    }

    outbox.slice(0, 30).forEach(log => {
        const tr = document.createElement('tr');

        const statusMap = {
            'demo_delivered': '演示已生成',
            'pending': '等待处理',
            'sending': '正在发送',
            'submitted_unverified': '已提交',
            'unknown': '结果未知',
            'blocked': '已阻止',
            'cancelled': '已取消'
        };

        tr.innerHTML = `
            <td>${escapeHtml(log.text.split('\n')[1] || log.text)}</td>
            <td>${formatDateTime(log.created_at)}</td>
            <td>${statusMap[log.state] || log.state}</td>
        `;

        tbody.appendChild(tr);
    });
}

// API 交互
async function sendMessage(text) {
    if (isBusy || !text.trim()) return false;

    isBusy = true;
    $('sendBtn').disabled = true;

    try {
        await api('/api/chat', {
            text: text.trim(),
            request_id: crypto.randomUUID()
        });
        await refreshState();
        return true;
    } catch (error) {
        showToast(error.message);
        return false;
    } finally {
        isBusy = false;
        $('sendBtn').disabled = false;
    }
}

async function completeTask(taskId) {
    await sendMessage(`完成 ${taskId}`);
}

async function cancelTask(taskId) {
    await sendMessage(`取消 ${taskId}`);
}

async function snoozeTask(taskId, minutes) {
    await sendMessage(`${minutes} 分钟后提醒 ${taskId}`);
}

// 暴露到全局
window.completeTask = completeTask;
window.cancelTask = cancelTask;
window.snoozeTask = snoozeTask;

// 刷新状态
async function refreshState() {
    try {
        const state = await api('/api/state');
        currentState = state;

        // 更新所有页面
        renderDashboard(state);
        renderTasks(state.tasks);
        renderChat(state.messages);
        renderSettings(state);

        // 更新时间显示
        updateTimeDisplay(state.now);
    } catch (error) {
        console.error('Failed to refresh state:', error);
    }
}

function updateTimeDisplay(timestamp) {
    const time = new Date(timestamp);
    $('currentTime').textContent = new Intl.DateTimeFormat('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    }).format(time);
}

// 微信连接
let wechatPolling = false;

async function refreshWechatStatus() {
    if (wechatPolling) return;
    wechatPolling = true;

    try {
        const status = await api('/api/wechat/login');

        const qrContainer = $('wechatQrContainer');
        const qrCode = $('wechatQrCode');
        const qrStatus = $('wechatLoginStatus');

        if (status.state === 'waiting_scan' && status.qr_version) {
            qrContainer.style.display = 'block';
            qrCode.src = `/wechat-login.png?v=${encodeURIComponent(status.qr_version)}`;
            qrStatus.textContent = status.detail || '请用微信扫描二维码';
        } else if (status.state === 'waiting_confirm') {
            qrStatus.textContent = '请在手机上确认登录';
        } else if (status.state === 'bound') {
            qrContainer.style.display = 'none';
            qrStatus.textContent = status.detail || '微信已授权，等待通道就绪。';
        } else {
            qrContainer.style.display = 'none';
            qrStatus.textContent = status.detail || '等待连接';
        }
    } catch (error) {
        console.error('Failed to refresh wechat status:', error);
    } finally {
        wechatPolling = false;
    }
}

async function connectWechat() {
    try {
        $('wechatConnectBtn').disabled = true;
        $('wechatConnectBtn').textContent = '正在连接...';

        const result = await api('/api/wechat/connect', {});
        showToast(result.detail || '开始连接微信');

        await refreshWechatStatus();
    } catch (error) {
        showToast(error.message);
    } finally {
        $('wechatConnectBtn').disabled = false;
        $('wechatConnectBtn').textContent = '连接微信 / 刷新二维码';
    }
}

// 工具函数
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// 初始化
function init() {
    initNavigation();

    // 快速添加任务
    $('quickAddTask').addEventListener('click', () => {
        openTaskModal();
    });

    $('addTaskBtn').addEventListener('click', () => {
        openTaskModal();
    });

    // 任务筛选
    document.querySelectorAll('.filter-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentFilter = tab.dataset.filter;
            if (currentState) {
                renderTasks(currentState.tasks);
            }
        });
    });

    // 聊天表单
    $('chatForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const input = $('chatInput');
        const text = input.value.trim();

        if (text && await sendMessage(text)) {
            input.value = '';
            scrollChatToBottom();
        }
    });

    // 快速短语
    document.querySelectorAll('[data-say]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const text = btn.dataset.say;
            await sendMessage(text);
        });
    });

    // 测试提醒
    $('testReminderBtn').addEventListener('click', () => {
        sendMessage('10 秒后提醒我检查试点效果');
    });

    // 设置表单
    $('settingsForm').addEventListener('submit', async (e) => {
        e.preventDefault();

        try {
            await api('/api/settings', {
                mode: $('modeSelect').value,
                model: $('modelSelect').value
            });
            showToast('设置已保存');
            await refreshState();
        } catch (error) {
            showToast(error.message);
        }
    });

    // 微信连接
    $('wechatConnectBtn').addEventListener('click', connectWechat);

    // 任务表单
    $('taskForm').addEventListener('submit', async (e) => {
        e.preventDefault();

        const title = $('taskTitle').value.trim();
        const due = $('taskDue').value;

        if (!title || !due) return;

        const text = `提醒我 ${due.replace('T', ' ')} ${title}`;

        if (await sendMessage(text)) {
            $('taskModal').close();
            $('taskTitle').value = '';
            showToast('任务已创建');
        }
    });

    // 关闭模态框
    $('closeModal').addEventListener('click', () => {
        $('taskModal').close();
    });

    // 开始刷新
    refreshState();
    refreshWechatStatus();

    // 定时刷新
    setInterval(refreshState, 2000);
    setInterval(refreshWechatStatus, 3000);

    // 检查URL hash
    if (location.hash === '#settings' || location.pathname === '/wechat') {
        switchPage('settings');
    }
}

function openTaskModal() {
    const now = new Date(Date.now() + 10 * 60 * 1000); // 默认10分钟后
    const formatted = new Intl.DateTimeFormat('sv-SE', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    }).format(now).replace(' ', 'T');

    $('taskDue').value = formatted;
    $('taskModal').showModal();
    $('taskTitle').focus();
}

// 启动应用
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
