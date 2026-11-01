import {api} from './api.js';
import {$,node,toast} from './ui.js';
import {refresh} from './app.js';

const nav=node('button','','协作验证');nav.dataset.panel='two-stage';
document.querySelector('.nav').append(nav);
const panel=node('section','panel');panel.id='panel-two-stage';
panel.innerHTML=`<section class="card"><div class="card-head"><div><h2>两段式任务验证</h2><div class="section-caption">A 整合 → 人工确认 → 自动交接 → B 汇总 → 人工确认</div></div><div style="display:flex;gap:8px;flex-wrap:wrap"><button id="two-stage-start">开始本地验证</button><button id="two-stage-wechat" class="primary">开始微信模拟</button></div></div><div class="body"><p>A、B 两个角色由你依次验收。微信模式将两段材料发到你当前绑定的同一个会话；本地模式只在网页操作。固定技能执行，不调用模型 API。</p><p class="caption">验证交接顺序、成果版本和重复确认；不代表已经实现员工登录、权限隔离或模型自主推理。</p></div></section><div id="two-stage-runs" style="display:grid;gap:20px;margin-top:20px"></div>`;
$('panel-settings').before(panel);
nav.onclick=()=>{document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b===nav));document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p===panel));$('heading').textContent='A 确认之后，B 自动接手。';refreshTwoStage()};
let polling=false,snapshot='';
function assetUrl(run,stage,asset){return '/api/two-stage/asset?'+new URLSearchParams({uid:run.uid,stage,name:asset.name,access:run.access_token})}
async function command(text,button){if(button)button.disabled=true;try{const r=await api('/api/chat',{text,request_id:crypto.randomUUID()});toast(r.reply);await Promise.all([refreshTwoStage(),refresh()])}catch(e){toast(e.message)}finally{if(button)button.disabled=false}}
$('two-stage-start').onclick=()=>command('开始两段式验证',$('two-stage-start'));
$('two-stage-wechat').onclick=()=>command('开始微信两段式验证',$('two-stage-wechat'));
function files(run,stage){const container=node('div');container.style.cssText='display:flex;gap:14px;flex-wrap:wrap;margin:16px 0';for(const item of run[stage.toLowerCase()+'_assets']){const a=node('a','',item.name);a.href=assetUrl(run,stage,item);if(item.name.endsWith('.png')){a.target='_blank';a.rel='noreferrer'}container.append(a)}return container}
function renderTwoStage(state){
  const encoded=JSON.stringify(state);if(encoded===snapshot)return;snapshot=encoded;
  const area=$('two-stage-runs');area.replaceChildren();
  if(!state.runs.length){area.append(node('div','empty','点击开始，第一段会读取现有两份示例 Excel。'));return}
  for(const run of state.runs){
    const card=node('article','card'),head=node('div','card-head'),body=node('div','body');
    head.append(node('h2','',run.code+' V'+run.version),node('span','badge',run.state_label));card.append(head,body);
    body.append(node('p','caption',run.role_binding||(run.notice_enabled?'A、B 均为本人已绑定微信':'本机模拟 A、B')));
    if(run.error)body.append(node('p','notice',run.error));
    const a=node('section');a.append(node('h2','','第一段 · 模拟 A：整合报表'));
    if(run.a_report.input_rows!==undefined){const r=run.a_report;a.append(node('p','subtitle',`输入 ${r.input_rows} 行 → 有效 ${r.output_rows} 行；去重 ${r.duplicate_rows} 行，隔离 ${r.invalid_rows} 行，合计 ${r.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2})}。`),files(run,'A'));
      const preview=run.a_assets.find(a=>a.name==='预览.png');if(preview){const img=node('img');img.alt='A 整合结果预览';img.src=assetUrl(run,'A',preview);img.style.cssText='width:100%;height:auto;border:1px solid #e6ebe5;border-radius:8px;margin:10px 0';a.append(img)}
    }
    if(run.state==='a_review'){
      a.append(node('p','notice','请检查 Excel 和异常清单。你确认之前，B 不会执行。'));
      const approve=node('button','primary','以模拟 A 身份确认第一段');approve.onclick=()=>command(`确认第一段 ${run.code} V${run.version}`,approve);a.append(approve);
      a.append(node('p','caption',`也可发送：确认第一段 ${run.code} V${run.version}`));
    }
    body.append(a);
    const b=node('section');b.style.cssText='border-top:1px solid #e6ebe5;margin-top:24px;padding-top:22px';b.append(node('h2','','第二段 · 模拟 B：部门汇总'));
    if(['a_queued','a_running','a_review'].includes(run.state))b.append(node('p','subtitle','等待 A 完成并确认，本阶段尚未启动。'));
    else if(run.state==='b_queued')b.append(node('p','subtitle','交接事件已保存，B 将自动接手。'));
    else if(run.state==='b_running')b.append(node('p','subtitle','B 正在读取 A 已确认的 Excel，生成部门汇总。'));
    if(run.b_report.rows!==undefined){
      const r=run.b_report;b.append(node('p','subtitle',`已接收 A 的 V${r.upstream_version}：${r.rows} 条有效记录，${r.departments.length} 个部门，合计 ${r.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2})}。`));
      const table=node('table','logs');const header=node('tr');for(const t of ['部门','有效记录','金额合计'])header.append(node('th','',t));const thead=node('thead');thead.append(header);table.append(thead);const tbody=node('tbody');
      for(const g of r.departments){const tr=node('tr');for(const v of [g.department,g.rows,g.total_amount.toLocaleString('zh-CN',{minimumFractionDigits:2})])tr.append(node('td','',String(v)));tbody.append(tr)}table.append(tbody);b.append(table,files(run,'B'));
    }
    if(run.state==='b_review'){
      b.append(node('p','notice','B 已自动生成汇总，请核对部门合计和交接单。'));
      const approve=node('button','primary','以模拟 B 身份确认第二段');approve.onclick=()=>command(`确认第二段 ${run.code} V${run.version}`,approve);b.append(approve);
      b.append(node('p','caption',`也可发送：确认第二段 ${run.code} V${run.version}`));
    }
    if(run.state==='completed')b.append(node('p','notice','验证完成：A 确认 → 自动交接 → B 汇总 → B 确认。'+(run.notice_enabled?'两段通知均使用本人绑定微信，未向其他联系人分发。':'本轮材料保存在本地。')));
    for(const event of run.events)b.append(node('p','caption',event.kind==='A_approved'?('A → B 交接：'+(event.state==='consumed'?'B 已接收并处理':'等待 B 处理')):'B 的最终确认已记录'));
    if(run.b_attempts)b.append(node('p','caption','B 执行尝试次数：'+run.b_attempts));
    const noticeLabels={pending:'等待发送',sending:'正在提交微信',submitted_unverified:'已提交微信，手机未确认',unknown:'结果未知，不自动重发',blocked:'发送已阻止',cancelled:'已取消过期通知'};
    for(const notice of run.notices||[])b.append(node('p','caption',({a_review:'A 验收通知',b_review:'B 验收通知',completed:'完成回执',failure:'异常通知'}[notice.kind]||notice.kind)+'：'+(noticeLabels[notice.state]||notice.state)+'。'+notice.detail));
    body.append(b);area.append(card);
  }
}
async function refreshTwoStage(){if(polling)return;polling=true;try{renderTwoStage(await api('/api/two-stage'))}catch(e){if(panel.classList.contains('active'))toast(e.message)}finally{polling=false}}
if(location.pathname==='/two-stage')nav.click();else refreshTwoStage();
setInterval(refreshTwoStage,2000);
