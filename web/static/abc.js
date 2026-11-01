import {api} from './api.js';
import {$,node,toast,fmt} from './ui.js';
import {refresh} from './app.js';

const nav=node('button','','三角色派单');nav.dataset.panel='abc';document.querySelector('.nav').append(nav);
const panel=node('section','panel');panel.id='panel-abc';
panel.innerHTML=`<section class="card"><div class="card-head"><h2>定时派 A → A 完成 → 派 B → B 完成 → 汇报 C</h2><button id="abc-start" class="primary">开始 30 秒定时模拟</button></div><div class="body"><p>A、B、领导 C 都使用你本人已绑定的微信，由消息标明角色。C 只接收最终结果，无需确认。</p><p class="caption">本轮使用示例数据：你发送 A、B 完成口令后，固定技能生成并校验对应的模拟成果，再继续交接。未接入真实三人身份或人工上传成果。</p></div></section><div id="abc-runs" style="display:grid;gap:20px;margin-top:20px"></div>`;
$('panel-settings').before(panel);
nav.onclick=()=>{document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b===nav));document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p===panel));$('heading').textContent='先派单，收到完成确认再交接。';poll()};
async function say(text,b){b.disabled=true;try{const r=await api('/api/chat',{text,request_id:crypto.randomUUID()});toast(r.reply);await Promise.all([poll(),refresh()])}catch(e){toast(e.message)}finally{b.disabled=false}}
$('abc-start').onclick=()=>say('开始ABC模拟',$('abc-start'));
let busy=false,snapshot='';
const events={scheduled:'已安排定时任务',A_assigned:'定时触发，派给 A',A_confirmed:'收到 A 完成确认',A_completed:'A 成果生成并校验通过',B_assigned:'已将 A 成果交接 B',B_confirmed:'收到 B 完成确认',B_completed:'B 汇总生成并校验通过',C_report_submitted:'C 汇报已提交通道，手机可见情况待核对'};
const notices={A_task:'A 派单',B_task:'B 派单',C_report:'领导 C 汇报',failure:'异常通知'};
const states={pending:'等待提交',sending:'正在提交',submitted_unverified:'已提交通道，手机未确认',unknown:'结果未知，不自动重发',blocked:'已阻止发送',cancelled:'已取消过期通知'};
function files(r,stage){const wrap=node('div');wrap.style.cssText='display:flex;gap:14px;flex-wrap:wrap;margin:12px 0';for(const a of r[stage.toLowerCase()+'_assets']){const link=node('a','',a.name);link.href='/api/abc/asset?'+new URLSearchParams({uid:r.uid,stage,name:a.name,access:r.access_token});wrap.append(link)}return wrap}
function render(s){const encoded=JSON.stringify(s);if(encoded===snapshot)return;snapshot=encoded;const area=$('abc-runs');area.replaceChildren();if(!s.runs.length){area.append(node('p','empty','开始后等待 30 秒，微信会收到 A 的派单。'));return}
for(const r of s.runs){const card=node('article','card'),head=node('div','card-head'),body=node('div','body');head.append(node('h2','',r.code+' V1'),node('span','badge',r.state_label));card.append(head,body);body.append(node('p','caption','定时触发：'+fmt(r.scheduled_at)+' · A、B、C 均为本人微信'));
if(r.error)body.append(node('p','notice',r.error));
for(const stage of ['A','B']){body.append(node('h2','',stage==='A'?'A · 整合报表':'B · 复核与部门汇总'));const r0=stage==='A'?r.a_report:r.b_report;if(r0.total_amount!==undefined)body.append(node('p','subtitle','有效 '+(r0.output_rows??r0.rows)+' 行，合计 '+r0.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2})),files(r,stage));
if(r.state===stage.toLowerCase()+'_waiting'){const text=`完成${stage} ${r.code} V1`;body.append(node('p','notice','请在微信回复：'+text));const b=node('button','','在网页以模拟 '+stage+' 确认完成');b.onclick=()=>say(text,b);body.append(b)}}
body.append(node('h2','','C · 领导接收汇报'),node('p','caption',r.state==='completed'?'A、B 已完成。下方单独记录 C 汇报是否提交通道。':'等待 A、B 依次完成，不提前汇报成功。'));
for(const e of r.events)body.append(node('p','caption',fmt(e.created_at)+' · '+(events[e.kind]||e.kind)));
for(const n of r.notices)body.append(node('p','caption',(notices[n.kind]||n.kind)+'：'+(states[n.state]||n.state)+'。'+n.detail));area.append(card)}}
async function poll(){if(busy)return;busy=true;try{render(await api('/api/abc'))}catch(e){if(panel.classList.contains('active'))toast(e.message)}finally{busy=false}}
if(location.pathname==='/abc')nav.click();else poll();setInterval(poll,2000);
