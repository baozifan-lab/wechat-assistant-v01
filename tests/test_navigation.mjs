import {test} from 'node:test';
import assert from 'node:assert/strict';
import {routeFor, hashFor} from '../web/static/navigation.js';

test('all legacy entry points resolve to a page in the new workbench', () => {
  for (const [path, kind] of [['/ai-workflow','ai'], ['/work','work'], ['/two-stage','two'], ['/abc','abc']]) {
    assert.deepEqual(routeFor(path, ''), {page:'tasks', kind, preview:false});
    assert.equal(routeFor(path, '#settings').page, 'settings');
  }
  assert.equal(routeFor('/wechat2', '').page, 'accounts');
  assert.equal(routeFor('/accounts', '').page, 'accounts');
  assert.equal(routeFor('/', '#accounts').page, 'accounts');
});
test('task and conversation links survive refresh, hash changes and encoded IDs', () => {
  const id = 'ai:AIW-20261102-ABCD1234';
  for (const page of ['tasks','chat']) assert.equal(routeFor('/', hashFor(page, {task:id})).task, id);
  assert.equal(routeFor('/', '#tasks').page, 'tasks');
  assert.equal(routeFor('/', '#chat').page, 'chat');
  assert.equal(routeFor('/', '#unknown').page, 'dashboard');
  assert.equal(routeFor('/ai-workflow', '#message-preview').preview, true);
});
