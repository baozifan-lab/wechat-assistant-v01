import {api} from './api.js';
import {$,fmt,toast,node} from './ui.js';
let current=null,lastMessage=0,lastState='',busy=false,settingsDirty=false;
function actionButton(label,text){let b=node('button','',label);b.type='button';b.onclick=()=>say(text);return b}
function render(s){current=s;
const live=s.channel==='wechat',received=Boolean(s.settings.wechat_last_inbound),gatewayOffline=s.wechat?.gateway_listening===false;
$('channel-notice').textContent=live&&gatewayOffline?'微信已授权，但接收网关未运行 · 后台正在尝试恢复；确认消息尚需核对是否进入服务。':live?'微信通道已启用 · 到点提醒会发送到本人绑定的微信；发送结果仍需在手机核对。':'演示通道 · 提醒只会出现在本页。连接微信并完成授权后，可验证真实收发。';
$('wechat-status').textContent=s.wechat?.bound&&gatewayOffline?'已授权 · 接收网关离线':received?'已收到本人微信消息':s.wechat?.bound?'已授权 · 待验证':'待授权';
$('wechat-status').className='badge'+(s.wechat?.bound?'':' offline');
$('clock').textContent='北京时间 '+fmt(s.now);$('day').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'long'}).format(new Date(s.now))+' / 个人试点';$('open-count').textContent=s.tasks.filter(t=>t.status==='open').length;$('scheduled-count').textContent=s.tasks.filter(t=>t.status==='open'&&t.next_at).length;$('done-count').textContent=s.tasks.filter(t=>t.status==='done').length;$('mode-badge').textContent=s.settings.mode==='model'?'模型辅助理解':'规则模式';$('model-status').textContent=s.settings.model_ready?'已配置 · 待调用验证':'未配置模型服务';$('model-status').className='badge'+(s.settings.model_ready?'':' offline');
const snapshot=JSON.stringify([s.tasks,s.outbox]);if(snapshot!==lastState){lastState=snapshot;const tasks=$('tasks');tasks.replaceChildren();if(!s.tasks.length){let e=node('div','empty');e.append(node('div','','◷'),node('b','','从第一件待办开始'),node('p','','点击“新建事项”，或直接告诉右侧助理。'));tasks.append(e)}
for(const t of s.tasks){let el=node('article','task'+(t.status==='open'?'':' done'));el.append(node('div','task-title','#'+t.id+' '+t.title));let state=t.status==='done'?'已完成':t.status==='cancelled'?'已取消':t.next_at?'下次提醒 '+fmt(t.next_at):'已提醒 · 等待你的回复';el.append(node('div','task-meta','截止 '+fmt(t.due)+' · '+state));if(t.status==='open'){let btns=node('div','task-buttons');btns.append(actionButton('已完成','完成 '+t.id),actionButton('10 分钟后','10 分钟后提醒 '+t.id),actionButton('取消','取消 '+t.id));el.append(btns)}tasks.append(el)}
const logs=$('log-rows');logs.replaceChildren();if(!s.outbox.length){let tr=node('tr');let td=node('td','','还没有提醒记录。试试创建一条 10 秒后的提醒。');td.colSpan=3;tr.append(td);logs.append(tr)}for(const o of s.outbox){let tr=node('tr');tr.append(node('td','log-title','#'+o.task_id+' '+o.text.split('\n')[1]),node('td','',fmt(o.created_at)),node('td','',({demo_delivered:'演示消息已生成',pending:'等待处理',sending:'正在提交微信',submitted_unverified:'已提交微信 · 手机未确认',unknown:'结果未知 · 未自动重发',blocked:'发送前被阻止',cancelled:'已取消'})[o.state]||o.state));logs.append(tr)}}
let newest=s.messages.at(-1)?.id||0;if(newest!==lastMessage){lastMessage=newest;const chat=$('chat');chat.replaceChildren();for(const m of s.messages){let el=node('div','msg '+m.role);el.append(node('small','',(m.role==='user'?'我':m.role==='reminder'?'助理 · 主动提醒':'工作助理')+'  '+fmt(m.created_at)),node('div','bubble',m.text));chat.append(el)}chat.scrollTop=chat.scrollHeight}
if(!settingsDirty){$('mode').value=s.settings.mode;const models=$('model');if(models.options.length!==s.settings.aliases.length){models.replaceChildren();for(const a of s.settings.aliases){let o=node('option','',a);o.value=a;models.append(o)}}models.value=s.settings.model}}
export async function refresh(){try{render(await api('/api/state'))}catch(e){$('task-status').textContent='连接中断';$('task-status').classList.add('offline')}}
async function say(text){if(busy)return false;busy=true;$('send').disabled=true;try{await api('/api/chat',{text,request_id:crypto.randomUUID()});await refresh();return true}catch(e){toast(e.message);return false}finally{busy=false;$('send').disabled=false}}
$('chat-form').onsubmit=async e=>{e.preventDefault();const text=$('chat-input').value.trim();if(text&&await say(text))$('chat-input').value=''};
document.querySelectorAll('[data-say]').forEach(b=>b.onclick=()=>say(b.dataset.say));$('demo').onclick=()=>say('10 秒后提醒我检查试点效果');
document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button').forEach(n=>n.classList.toggle('active',n===b));document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id==='panel-'+b.dataset.panel));$('heading').textContent=({work:'把事情交代清楚，放心去忙。',settings:'模型由你选择，接入逐步验证。',logs:'每次提醒，都有记录。'})[b.dataset.panel]});
$('add').onclick=()=>{let t=new Date(Date.now()+600000);$('due').value=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(t).replace(' ','T');$('task-dialog').showModal();$('title').focus()};$('close-dialog').onclick=()=>$('task-dialog').close();
$('task-form').onsubmit=async e=>{e.preventDefault();let title=$('title').value.trim(),d=$('due').value;if(!title||!d)return;const text='提醒我 '+d.replace('T',' ')+' '+title;if(await say(text)){$('task-dialog').close();$('title').value='';toast('事项已记录')}};
['mode','model'].forEach(id=>$(id).onchange=()=>settingsDirty=true);$('settings-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/settings',{mode:$('mode').value,model:$('model').value});settingsDirty=false;await refresh();toast('设置已保存')}catch(err){toast(err.message)}};
let qrVersion=null,loginRefreshPending=false,refreshBaseline=null,refreshStarted=0,loginPollBusy=false;
function finishQrRefresh(){loginRefreshPending=false;const b=$('wechat-connect');b.disabled=false;b.textContent='连接微信／刷新二维码'}
async function refreshWechat(){
  if(loginPollBusy)return;loginPollBusy=true;
  try{
    const v=await api('/api/wechat/login');
    const version=v.qr_version||v.updated_at||null;
    const qrReady=v.state==='waiting_scan'&&version!==null;
    const sameOldQr=loginRefreshPending&&qrReady&&version===refreshBaseline;
    if(loginRefreshPending&&(v.state==='waiting_confirm'||['bound','gateway_started','needs_attention','error','expired'].includes(v.state)||(qrReady&&!sameOldQr)))finishQrRefresh();
    $('wechat-login-state').textContent=sameOldQr?'正在申请新二维码，请稍候……':(v.detail||'等待开始连接。');
    const show=(qrReady&&!sameOldQr)||v.state==='waiting_confirm';
    $('wechat-qr').style.display=show?'block':'none';
    if(qrReady&&!sameOldQr&&version!==qrVersion){qrVersion=version;$('wechat-qr').src='/wechat-login.png?v='+encodeURIComponent(version)}
    if(loginRefreshPending&&Date.now()-refreshStarted>80000){finishQrRefresh();$('wechat-qr').style.display='none';$('wechat-login-state').textContent='新二维码尚未生成，请再次刷新；原二维码已隐藏。'}
  }catch(e){$('wechat-login-state').textContent='连接页面与服务暂时中断，请刷新页面重试。';$('wechat-qr').style.display='none';finishQrRefresh()}
  finally{loginPollBusy=false}
}
$('wechat-connect').onclick=async()=>{
  if(loginRefreshPending)return;
  loginRefreshPending=true;refreshBaseline=qrVersion;refreshStarted=Date.now();
  const b=$('wechat-connect');b.disabled=true;b.textContent='正在申请新二维码…';
  $('wechat-qr').style.display='none';$('wechat-login-state').textContent='正在结束旧的扫码请求并申请新二维码……';
  try{const result=await api('/api/wechat/connect',{});$('wechat-login-state').textContent=result.detail||'正在生成新二维码。';await refreshWechat()}
  catch(e){finishQrRefresh();$('wechat-login-state').textContent=e.message;toast(e.message)}
};
if(location.pathname==='/wechat'||location.hash==='#wechat')document.querySelector('[data-panel="settings"]').click();
refresh();refreshWechat();setInterval(refresh,1500);setInterval(refreshWechat,2500);
