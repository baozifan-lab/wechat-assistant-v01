import {api} from './api.js';
import {$,toast,node} from './ui.js';
import {refresh} from './app.js';
import './two_stage.js';
import './abc.js';
// Local demonstration workbench; dynamic content is always inserted as text.
const flowNav=node('button','','今日工作');flowNav.dataset.panel='daily';
document.querySelector('[data-panel="work"]').after(flowNav);
const flowPanel=node('section','panel');flowPanel.id='panel-daily';
flowPanel.innerHTML=`<div class="card"><div class="card-head"><div><h2>项目文档 → 执行技能 → 本人验收 → 模拟投递</h2><div class="section-caption">Excel 整合示例 · 全部为虚构数据</div></div><button class="primary" id="flow-start">开始今日工作</button></div><div class="body"><p class="subtitle">两份部门报表，统一字段、去重、隔离异常，生成一张工作表。完成后发送到本人微信；你确认具体任务和版本后，自动投递到两个本地模拟收件箱。</p><p class="caption">当前按需执行。定时执行可在项目文档中开启，服务运行期间生效。</p></div></div><div id="flow-jobs" style="display:grid;gap:20px;margin-top:20px"></div>`;
$('panel-settings').before(flowPanel);
flowNav.onclick=()=>{document.querySelectorAll('.nav button').forEach(n=>n.classList.toggle('active',n===flowNav));document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p===flowPanel));$('heading').textContent='先看结果，再确认发送。';refreshFlows()};
let flowSnapshot='',flowPolling=false;
const flowSendStates={pending:'等待发送',sending:'正在发送',submitted_unverified:'已提交微信，待你在手机核对',unknown:'发送结果未知，请查看手机和本页',blocked:'发送受阻',demo_delivered:'材料保留在工作台',cancelled:'已取消'};
function assetUrl(job,asset){return '/api/workflow/asset?'+new URLSearchParams({uid:job.uid,name:asset.name,access:job.review_token})}
async function flowCommand(text){try{const result=await api('/api/chat',{text,request_id:crypto.randomUUID()});toast(result.reply);await Promise.all([refresh(),refreshFlows()])}catch(e){toast(e.message)}}
$('flow-start').onclick=async()=>{const b=$('flow-start');b.disabled=true;try{await flowCommand('开始今日工作')}finally{b.disabled=false}};
function renderFlows(state){
  const snap=JSON.stringify(state);if(snap===flowSnapshot)return;flowSnapshot=snap;
  const area=$('flow-jobs');area.replaceChildren();
  if(!state.jobs.length){area.append(node('div','empty','还没有执行记录，点击“开始今日工作”体验。'));return}
  for(const job of state.jobs){
    const card=node('article','card'),head=node('div','card-head'),body=node('div','body');
    const title=node('div');title.append(node('h2','',job.title),node('div','section-caption',job.code+' V'+job.version));head.append(title,node('span','badge',job.state_label));card.append(head,body);
    const r=job.report;
    if(r.input_rows!==undefined){
      const metrics=node('div','metrics');
      for(const [label,value,foot] of [['原始记录',r.input_rows,'来自 '+r.input_files.length+' 份 Excel'],['有效记录',r.output_rows,'去重 '+r.duplicate_rows+' 行 · 隔离 '+r.invalid_rows+' 行'],['金额合计',r.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2}),'示例金额']]){const m=node('div','metric');m.append(node('div','label',label),node('div','number',String(value)),node('div','foot',foot));metrics.append(m)}body.append(metrics);
    }
    if(job.error)body.append(node('p','notice',job.error));
    const files=node('div');files.style.cssText='display:flex;flex-wrap:wrap;gap:15px;margin:14px 0';
    for(const asset of job.assets){const a=node('a','',asset.name);a.href=assetUrl(job,asset);if(asset.name.endsWith('.png')){a.target='_blank';a.rel='noreferrer'}files.append(a)}body.append(files);
    const preview=job.assets.find(a=>a.name==='预览.png');if(preview){const img=node('img');img.alt='整合结果预览：示例表格和金额合计';img.src=assetUrl(job,preview);img.style.cssText='max-width:100%;height:auto;border:1px solid #e6ebe5;border-radius:8px;margin:8px 0 18px';body.append(img)}
    body.append(node('p','subtitle','确认后的收件人：'+job.recipients.join('、')+' · 渠道：本地模拟收件箱'));
    for(const n of job.notices)body.append(node('p','caption',({review:'验收通知：',delivery:'投递通知：',failure:'异常通知：'}[n.kind]||'通知：')+(flowSendStates[n.state]||n.state)+'。'+n.detail));
    if(job.state==='awaiting_review'){
      const command='确认发送 '+job.code+' V'+job.version;
      body.append(node('p','notice','核对附件后，可点击下方按钮，或在微信回复：'+command));
      const actions=node('div');actions.style.cssText='display:flex;gap:10px;flex-wrap:wrap';
      const approve=node('button','primary','确认此版本并模拟投递');approve.onclick=()=>flowCommand(command);
      const reject=node('button','','要求修改');reject.onclick=()=>flowCommand('要求修改 '+job.code+' V'+job.version);actions.append(approve,reject);body.append(actions);
    }
    if(['rejected','failed','blocked','awaiting_review','delivered'].includes(job.state)){
      const redo=node('button','','重新制作新版本');redo.style.marginTop='12px';redo.onclick=()=>flowCommand('重新制作 '+job.code);body.append(redo);
    }
    if(job.deliveries.length)body.append(node('p','subtitle','模拟投递：'+job.deliveries.filter(d=>d.state==='delivered_local').length+' / '+job.deliveries.length+' 个收件箱完成'));
    area.append(card);
  }
}
async function refreshFlows(){if(flowPolling)return;flowPolling=true;try{renderFlows(await api('/api/workflows'))}catch(e){if(flowPanel.classList.contains('active'))toast('今日工作连接中断：'+e.message)}finally{flowPolling=false}}
if(location.pathname==='/work')flowNav.click();else refreshFlows();
setInterval(refreshFlows,2500);
