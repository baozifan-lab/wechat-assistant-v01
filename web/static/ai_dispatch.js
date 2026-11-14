import {api,apiRequest} from './api.js';
import {node,toast} from './ui.js';
const $=id=>document.getElementById(id);
const names={manager:'负责人',employee1:'员工1',employee2:'员工2'};
const deliveries={pending:'等待发送',sending:'正在提交',demo_delivered:'本地模拟消息',submitted_unverified:'已提交微信，送达未确认',unknown:'发送结果未知',blocked:'发送受阻',cancelled:'已取消'};
let snapshot='',polling=false,configDirty=false,configBusy=false;
let previewLoaded=false;
$('message-preview').ontoggle=async()=>{
  if(!$('message-preview').open||previewLoaded)return;
  const area=$('message-preview-body');area.textContent='正在读取示例……';
  try{const s=await api('/api/ai-dispatch/preview');area.replaceChildren();for(const m of s.messages){const box=node('section','message');box.append(node('b','',m.label),node('pre','',m.text));area.append(box)}previewLoaded=true}
  catch(e){area.textContent=e.message}
};
if(location.hash==='#message-preview')$('message-preview').open=true;
const configInputs=['api-base','api-model','api-key','api-brief','api-json'];
for(const id of configInputs)$(id).addEventListener('input',()=>{configDirty=true;$('api-feedback').textContent='配置尚未保存；测试按钮只测试已保存的连接。'});
function configData(){return {base_url:$('api-base').value.trim(),model:$('api-model').value.trim(),api_key:$('api-key').value,disable_thinking:$('api-brief').checked,json_mode:$('api-json').checked}}
function busyConfig(value){configBusy=value;for(const id of [...configInputs,'api-save','api-test','api-clear'])$(id).disabled=value}
$('api-form').onsubmit=async event=>{
  event.preventDefault();busyConfig(true);
  try{await api('/api/ai-dispatch/model',configData());$('api-key').value='';configDirty=false;snapshot='';$('api-feedback').textContent='已保存并应用。点击“测试已保存的连接”验证 API。';await refresh()}
  catch(e){$('api-feedback').textContent=e.message}finally{busyConfig(false)}
};
$('api-test').onclick=async()=>{
  if(configDirty){$('api-feedback').textContent='请先保存当前配置，再测试连接。';return}
  busyConfig(true);$('api-feedback').textContent='正在测试已保存的接口与模型，请稍候……';
  try{const result=await api('/api/ai-dispatch/model/test',{});$('api-feedback').textContent=result.detail;snapshot='';await refresh()}
  catch(e){$('api-feedback').textContent=e.message}finally{busyConfig(false)}
};
$('api-clear').onclick=async()=>{
  if(configDirty){$('api-feedback').textContent='请先保存或刷新页面，再清除已保存密钥。';return}
  busyConfig(true);
  try{await api('/api/ai-dispatch/model',{...configData(),api_key:'',clear_key:true});$('api-key').value='';configDirty=false;snapshot='';$('api-feedback').textContent='已清除本机保存的密钥，接口地址和模型保留。';await refresh()}
  catch(e){$('api-feedback').textContent=e.message}finally{busyConfig(false)}
};
async function act(path,data){try{const result=await api(path,data);toast(result.reply||(result.code?'流程已启动，正在通知负责人。':'操作已完成'));await refresh()}catch(e){toast(e.message)}}
async function download(code){try{
  const response=await apiRequest('/api/ai-dispatch/asset?'+new URLSearchParams({code}));
  if(!response.ok)throw Error('成果文件不可用或校验未通过');
  const url=URL.createObjectURL(await response.blob());const a=node('a');a.href=url;a.download='商品交集核查.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}catch(e){toast(e.message)}}
function render(s){
  const m=s.model;
  if(!configDirty){$('api-base').value=m.base_url||'';$('api-model').value=m.model||'qwen3.8-max';$('api-brief').checked=!!m.disable_thinking;$('api-json').checked=!!m.json_mode}
  $('api-source').textContent=m.source==='local'?'已保存于本机':'使用启动配置';
  $('api-key-hint').textContent=m.has_key?'已有密钥，留空保存将继续使用；更换接口地址时需重新填写。':'尚未保存密钥，请在本机粘贴 API Key。';
  $('model').textContent=m.error?'API 配置需要检查':m.test?.ok?'API 连接测试通过':m.ready?'API 已配置 · 待测试':m.base_url?'接口已填写 · 待输入 API Key':'AI API 待配置';
  $('model').className='badge'+(s.model.ready?'':' offline');$('error').textContent=[m.error,s.config_error].filter(Boolean).join('\n');
  const tasks=$('tasks');tasks.replaceChildren();
  for(const task of s.tasks){const row=node('div','row');row.append(node('b','',task.title),node('span','subtitle',task.enabled?'任务已启用':'任务关闭 · 待配置'));
    row.append(node('span','caption','任务渠道：'+(Object.values(task.roles).every(c=>c==='local')?'本地模拟，消息只在网页显示':'使用已绑定微信')));
    const schedule=task.schedule;row.append(node('span','caption',schedule.kind==='manual'?'按需启动':(schedule.kind==='daily'?'每天 ':'单次 ')+schedule.at+'（北京时间）'));
    const start=node('button','primary','启动流程');start.disabled=!task.enabled||!s.model.ready;start.onclick=()=>act('/api/ai-dispatch/start',{task_id:task.id,request_id:crypto.randomUUID()});row.append(start);tasks.append(row);
  }
  const area=$('runs');area.replaceChildren();
  if(!s.runs.length)area.append(node('div','empty','尚未启动 AI 流程。配置完成后，可先用本地模拟角色验证。'));
  for(const run of s.runs){
    const card=node('article','card'),head=node('div','card-head'),body=node('div','body');
    head.append(node('h2','',run.display_name||'商品排查'),node('span','badge',run.state_label));card.append(head,body);body.append(node('p','',run.task.title));
    const local=Object.values(run.task.roles).every(c=>c==='local');
    body.append(node('p','notice',local?'本次流程为本地模拟，未向微信发送消息。':'本次流程使用已绑定微信：负责人和员工1 → 主会话；员工2 → 第二会话。'));
    if(local&&run.state==='manager_wait'){
      const connect=node('button','','切换绑定微信并通知负责人');
      connect.onclick=()=>act('/api/ai-dispatch/connect-wechat',{code:run.code});body.append(connect);
      body.append(node('p','caption','沿用现有商品排查绑定：负责人和员工1共用主微信，员工2使用第二微信。'));
    }
    if(run.error)body.append(node('p','notice',run.error));
    if(run.evidence.employee2){body.append(node('p','',run.evidence.employee2.summary));const b=node('button','','下载核查结果');b.onclick=()=>download(run.code);body.append(b)}
    for(const notice of run.notices){const el=node('div','message');el.append(node('small','subtitle',(names[notice.role]||notice.role)+' · '+(deliveries[notice.state]||notice.state)),node('div','',notice.text),node('small','caption',notice.detail));body.append(el)}
    const role=run.state==='manager_wait'?'manager':run.state.split('_')[0];
    if((run.state.endsWith('_wait')||run.state.endsWith('_review'))&&run.task.roles[role]==='local'){
      const form=node('form');form.append(node('p','subtitle','以本地模拟 '+names[role]+' 回复（不会向真实人员发送）'));
      const input=node('textarea');input.required=true;input.maxLength=2000;input.placeholder=role==='manager'?'例如：可以，开始吧':run.state.endsWith('_review')?'例如：没问题 / 核对过了':role==='employee1'?'例如：收到 / 导好了':'例如：收到 / 帮我查一下';input.setAttribute('aria-label',names[role]+'的回复');
      const button=node('button','primary','提交回复');button.type='submit';form.append(input,button);
      form.onsubmit=async e=>{e.preventDefault();button.disabled=true;await act('/api/ai-dispatch/reply',{code:run.code,role,text:input.value,request_id:crypto.randomUUID()});button.disabled=false};body.append(form);
    }
    const details=node('details');details.append(node('summary','','查看处理记录'),node('p','caption','内部流程编号：'+run.code));
    for(const reply of run.replies)details.append(node('p','subtitle',names[reply.role]+'：'+reply.text+' ['+reply.state+'] '+reply.result));
    const latest=run.replies.at(-1);
    if(latest?.state==='failed'&&latest.revision===run.revision){const retry=node('button','','重试最新失败回复');retry.onclick=()=>act('/api/ai-dispatch/retry-reply',{code:run.code,request_id:latest.request_id});body.append(retry)}
    if(run.task.project_brief){const brief=node('details');brief.append(node('summary','','本次流程使用的项目说明：'+run.task.project_brief.name),node('p','caption','文档摘要 '+run.task.project_brief.sha256),node('pre','',run.task.project_brief.text));body.append(brief)}
    for(const event of run.events)details.append(node('p','caption',event.created+' · '+event.detail));body.append(details);area.append(card);
  }
}
async function refresh(){if(polling)return;polling=true;try{const s=await api('/api/ai-dispatch');const next=JSON.stringify(s);if(next!==snapshot){
  // Leave a draft reply intact while the worker updates status in the background.
  if(document.activeElement?.tagName==='TEXTAREA')return;
  snapshot=next;render(s);
}}catch(e){$('error').textContent=e.message}finally{polling=false}}
refresh();setInterval(refresh,2500);
