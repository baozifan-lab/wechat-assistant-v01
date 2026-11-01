import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHandler } from './index.mjs';

function fixture(t, fetcher) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'zhishi-test-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const routePath=path.join(dir,'route.json'),secretPath=path.join(dir,'key');
  fs.writeFileSync(routePath,JSON.stringify({account_id:'account-a',peer_id:'owner'}));
  fs.writeFileSync(secretPath,'fixture-only');
  return createHandler({serviceUrl:'http://127.0.0.1:8765',routePath,secretPath},fetcher);
}
const event={channel:'openclaw-weixin',senderId:'owner',messageId:'msg-1',content:'我的待办'};
const context={accountId:'account-a'};

test('authorized message is forwarded with stable identity',async t=>{
  let payload;
  const handler=fixture(t,async (url, options)=>{
    payload=JSON.parse(options.body);
    assert.equal(new URL(url).pathname,'/api/wechat/inbound');
    return {ok:true,json:async()=>({reply:'当前没有待办'})};
  });
  assert.deepEqual(await handler(event,context),{handled:true,text:'当前没有待办'});
  assert.equal(payload.source_message_id,'msg-1');
  assert.equal(payload.peer_id,'owner');
});
test('other sender or group cannot fall through to the model',async t=>{
  const handler=fixture(t,()=>{throw Error('must not call service')});
  assert.equal((await handler({...event,senderId:'other'},context)).handled,true);
  assert.equal((await handler({...event,isGroup:true},context)).handled,true);
});
test('other channels remain untouched',async t=>{
  const handler=fixture(t,()=>{throw Error('must not call service')});
  assert.equal(await handler({...event,channel:'telegram'},context),undefined);
});
test('official Weixin direct context without SenderId uses its exact conversation target',async t=>{
  let forwarded;
  const handler=fixture(t,async (url,options)=>{
    forwarded=JSON.parse(options.body);
    return {ok:true,json:async()=>({reply:'当前没有待办'})};
  });
  const {senderId,...withoutSender}=event;
  const result=await handler({...withoutSender,isGroup:false},{...context,conversationId:'owner'});
  assert.equal(result.text,'当前没有待办');
  assert.equal(forwarded.peer_id,'owner');
});
test('conversation fallback rejects groups, unknown chat kind and another peer',async t=>{
  const handler=fixture(t,()=>{throw Error('must not call service')});
  const {senderId,...withoutSender}=event;
  for(const [message,ctx] of [
    [{...withoutSender,isGroup:true},{...context,conversationId:'owner'}],
    [withoutSender,{...context,conversationId:'owner'}],
    [{...withoutSender,isGroup:false},{...context,conversationId:'another'}],
    [{...event,senderId:'another',isGroup:false},{...context,conversationId:'owner'}],
  ])assert.equal((await handler(message,ctx)).handled,true);
});
test('timeout produces an explicit uncertain outcome',async t=>{
  const handler=fixture(t,()=>{throw Error('timeout')});
  const result=await handler(event,context);
  assert.equal(result.handled,true);
  assert.match(result.text,/核对/);
});
