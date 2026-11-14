import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';

const source=await fs.readFile(new URL('../web/static/api.js',import.meta.url),'utf8');
let sequence=0;
function json(value,status=200){return new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}})}
async function client(fetcher){
  const meta={content:'old-token'};
  const draft={reply:'还在编辑的回复',key:'unsaved-dummy'};
  globalThis.document={querySelector:()=>meta};
  globalThis.location={origin:'http://127.0.0.1:8765',href:'http://127.0.0.1:8765/ai-workflow'};
  globalThis.DOMParser=class{parseFromString(html){return {querySelector:()=>{const value=html.match(/content="([^"]+)"/);return value?{content:value[1]}:null}}}};
  const calls=[];
  globalThis.fetch=async(path,options={})=>{calls.push({path,options});return fetcher(path,options)};
  const module=await import('data:text/javascript;base64,'+Buffer.from(source+'\n// case '+sequence++).toString('base64'));
  return {module,meta,calls,draft};
}
function page(){return new Response('<meta name="pilot-token" content="new-token">',{headers:{'Content-Type':'text/html'}})}

test('stale GET refreshes same-origin credential and preserves drafts',async()=>{
  const c=await client((path,options)=>path==='/'?page():options.headers.get('X-Pilot-Token')==='old-token'?
    json({error:'未找到页面或缺少本机访问凭证。'},404):json({ok:true}));
  assert.deepEqual(await c.module.api('/api/ai-dispatch'),{ok:true});
  assert.equal(c.meta.content,'new-token');
  assert.equal(c.calls.length,3);
  assert.equal(c.calls[1].path,'/');
  assert.equal(c.calls[1].options.cache,'no-store');
  assert.equal(c.calls[1].options.redirect,'error');
  assert.equal(c.draft.reply,'还在编辑的回复');
  assert.equal(c.draft.key,'unsaved-dummy');
});

test('stale start POST retries the same body and performs the action once',async()=>{
  let created=0;
  const c=await client((path,options)=>{
    if(path==='/')return page();
    if(options.headers.get('X-Pilot-Token')==='old-token')return json({error:'缺少本机访问凭证。'},403);
    created++;return json({code:'test-run'});
  });
  const data={task_id:'test',request_id:'same-start-request'};
  assert.equal((await c.module.api('/api/ai-dispatch/start',data)).code,'test-run');
  const posts=c.calls.filter(c=>c.options.method==='POST');
  assert.equal(posts.length,2);
  assert.equal(posts[0].options.body,posts[1].options.body);
  assert.equal(created,1);
});

test('simultaneous old requests share a single credential refresh',async()=>{
  const c=await client(async(path,options)=>{
    if(path==='/'){await new Promise(resolve=>setTimeout(resolve,5));return page()}
    return options.headers.get('X-Pilot-Token')==='old-token'?json({error:'缺少本机访问凭证。'},403):json({ok:true});
  });
  await Promise.all([c.module.api('/api/a'),c.module.api('/api/b')]);
  assert.equal(c.calls.filter(c=>c.path==='/').length,1);
});

test('business denial does not refresh credentials or replay the operation',async()=>{
  const c=await client(()=>json({error:'请求来源不匹配。'},403));
  await assert.rejects(c.module.api('/api/ai-dispatch/start',{request_id:'test'}),/请求来源不匹配/);
  assert.equal(c.calls.length,1);
});

test('a second authentication rejection stops after one retry',async()=>{
  const c=await client(path=>path==='/'?page():json({error:'缺少本机访问凭证。'},403));
  await assert.rejects(c.module.api('/api/ai-dispatch/start',{}),/页面连接状态已失效/);
  assert.equal(c.calls.length,3);
});

test('missing credential in fresh HTML does not replay a POST',async()=>{
  const c=await client(path=>path==='/'?new Response('<html>no meta</html>'):json({error:'缺少本机访问凭证。'},403));
  await assert.rejects(c.module.api('/api/ai-dispatch/start',{}),/连接状态无法更新/);
  assert.equal(c.calls.length,2);
});

test('local access token cannot be sent to another origin',async()=>{
  const c=await client(()=>{throw Error('must not fetch')});
  await assert.rejects(c.module.api('https://other.example/api/chat'),/只允许访问本机/);
  assert.equal(c.calls.length,0);
});

test('binary downloads also recover from an expired page token',async()=>{
  const c=await client((path,options)=>path==='/'?page():options.headers.get('X-Pilot-Token')==='old-token'?
    json({error:'未找到页面或缺少本机访问凭证。'},404):new Response('col1,col2\n1,2'));
  const response=await c.module.apiRequest('/api/ai-dispatch/asset?code=test');
  assert.equal(await response.text(),'col1,col2\n1,2');
});
