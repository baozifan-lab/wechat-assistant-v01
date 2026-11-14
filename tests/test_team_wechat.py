from datetime import timedelta
import hashlib
import hmac
import json
import time
import uuid

from zhishi.core.clock import now, stamp
from zhishi.team.errors import TeamError
from tests.team_support import TeamCase, BRIDGE_SECRET


class TeamWechatTests(TeamCase):
    def event(self, value, peer='test-peer', message_id=None):
        return {'account_id': 'test-account', 'peer_id': peer, 'source_message_id': message_id or str(uuid.uuid4()),
                'text': value, 'is_group': False}

    def test_signature_is_bound_to_raw_body_timestamp_and_secret(self):
        raw, timestamp = b'{"test":true}', str(int(time.time()))
        signature = hmac.new(BRIDGE_SECRET.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        self.app.wechat.verify_signature(raw, timestamp, signature)
        for body, ts, sig in [(raw + b' ', timestamp, signature), (raw, str(int(time.time()) - 600), signature), (raw, timestamp, 'wrong')]:
            with self.assertRaises(TeamError):
                self.app.wechat.verify_signature(body, ts, sig)

    def test_code_is_one_use_and_another_peer_cannot_steal_bound_account(self):
        code = self.bind()
        replay = self.app.wechat.receive(self.event('绑定 ' + code, peer='another-peer'))
        self.assertIn('无效', replay['reply'])
        self.assertTrue(self.app.wechat.binding(self.ids['bob'])['bound'])
        challenge = self.app.wechat.begin_binding(self.ids['carol'], self.data())
        result = self.app.wechat.receive(self.event('绑定 ' + challenge['binding_code']))
        self.assertIn('已被占用', result['reply'])

    def test_disabled_user_and_unbound_peer_cannot_dispatch(self):
        self.bind()
        self.app.auth.update_user(self.ids['admin'], self.ids['bob'], {'active': False})
        with self.assertRaises(TeamError):
            self.app.wechat.receive(self.event('我的任务'))
        with self.assertRaises(TeamError):
            self.app.wechat.receive(self.event('我的任务', peer='unknown'))

    def test_duplicate_message_does_not_repeat_state_transition(self):
        self.bind()
        task = self.task()
        event = self.event(f'接单 {task["id"]} V{task["version"]}')
        first = self.app.wechat.receive(event)
        self.assertEqual(first, self.app.wechat.receive(event))
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['version'], 2)
        with self.assertRaises(TeamError):
            self.app.wechat.receive({**event, 'text': '我的任务'})

    def test_group_messages_and_negative_or_conditional_approval_never_advance(self):
        self.bind()
        task = self.task()
        with self.assertRaises(TeamError):
            self.app.wechat.receive({**self.event('收到'), 'is_group': True})
        result = self.app.wechat.receive(self.event('如果没问题就接单'))
        self.assertIn('任务页', result['reply'])
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['state'], 'open')

    def test_multiple_waiting_tasks_require_an_explicit_task(self):
        self.bind()
        self.task()
        self.task()
        result = self.app.wechat.receive(self.event('收到'))
        self.assertIn('多件工作', result['reply'])
        self.assertTrue(all(t['state'] == 'open' for t in self.s.tasks(self.ids['bob'])))

    def test_wrong_role_and_stale_version_cannot_approve(self):
        self.bind()
        task = self.task()
        result = self.app.wechat.receive(self.event(f'通过 {task["id"]} V1'))
        self.assertIn('验收人', result['reply'])
        self.action('bob', task, 'accept')
        result = self.app.wechat.receive(self.event(f'提交 {task["id"]} V1'))
        self.assertIn('版本已变化', result['reply'])

    def test_binding_codes_expire_and_failed_attempts_are_limited(self):
        result = self.app.wechat.begin_binding(self.ids['bob'], self.data())
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_binding_codes SET expires_at=?', (stamp(now() - timedelta(seconds=1)),))
        self.assertIn('过期', self.app.wechat.receive(self.event('绑定 ' + result['binding_code']))['reply'])
        for _ in range(4):
            self.app.wechat.receive(self.event('绑定 DEAD0000'))
        with self.assertRaises(TeamError) as error:
            self.app.wechat.receive(self.event('绑定 DEAD0000'))
        self.assertEqual(error.exception.status, 429)

    def test_unbinding_revokes_wechat_access_and_pending_notifications(self):
        self.bind()
        self.task()
        self.app.wechat.unbind(self.ids['bob'], self.data())
        with self.assertRaises(TeamError):
            self.app.wechat.receive(self.event('我的任务'))
        self.assertEqual(self.s.notifications(self.ids['bob'])[0]['state'], 'cancelled')

    def test_binding_change_between_ingress_and_mutation_cannot_advance_task(self):
        from unittest.mock import patch
        self.bind()
        task = self.task()
        original = self.s.task_action
        def changed(*args, **kwargs):
            with self.app.store.connect(write=True) as db:
                db.execute("UPDATE team_bindings SET generation='new-generation' WHERE user_id=?", (self.ids['bob'],))
            return original(*args, **kwargs)
        with patch.object(self.s, 'task_action', side_effect=changed):
            result = self.app.wechat.receive(self.event('收到'))
        self.assertIn('绑定已变化', result['reply'])
        self.assertEqual(self.s.detail(self.ids['bob'], task['id'])['state'], 'open')


class TeamNotificationTests(TeamCase):
    def test_losing_project_management_blocks_push_even_while_remaining_a_member(self):
        self.bind()
        self.s.set_member(self.ids['alice'], self.project['id'], self.ids['bob'], self.data(role='manager'))
        task = self.task(assignee_id=self.ids['carol'], reviewer_id=self.ids['alice'])
        with self.app.store.connect(write=True) as db:
            notice_id = self.s.notify(db, self.ids['bob'], '项目管理通知', task)
        self.s.set_member(self.ids['alice'], self.project['id'], self.ids['bob'], self.data(role='member'))
        calls = []
        self.app.notifier.sender = lambda *args: calls.append(args)
        self.app.notifier.tick()
        self.assertEqual(calls, [])
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM team_notifications WHERE id=?', (notice_id,)).fetchone()[0], 'cancelled')

    def test_sender_scopes_verified_recipient_without_overwriting_pilot_binding(self):
        from pathlib import Path
        from unittest.mock import Mock, patch
        from zhishi.channels.wechat.bridge import WechatBridge
        from zhishi.team.wechat import WechatSender
        profile = self.root / 'gateway-profile'
        bridge = WechatBridge(self.root, profile)
        original = {'account_id': 'test-account', 'peer_id': 'pilot-peer'}
        bridge.route_path.write_text(json.dumps(original), encoding='utf-8')
        (profile / 'zhishi-bridge.enabled').touch()
        with patch.object(bridge, 'gateway_listening', return_value=True), patch.object(WechatBridge, 'send_package', return_value=('submitted_unverified', 'test')) as send:
            state, _ = WechatSender([bridge])({'account_id': 'test-account', 'peer_id': 'verified-employee'}, 'task notice')
        self.assertEqual(state, 'submitted_unverified')
        self.assertEqual(send.call_args.kwargs['expected_route']['peer_id'], 'verified-employee')
        self.assertEqual(json.loads(bridge.route_path.read_text('utf-8')), original)
    def test_unknown_send_is_not_retried_automatically(self):
        self.bind()
        self.task()
        calls = []
        self.app.notifier.sender = lambda binding, message: (calls.append(message) or ('unknown', '模拟未知结果'))
        self.app.notifier.tick()
        self.app.notifier.tick()
        self.assertEqual(len(calls), 1)
        notice = self.s.notifications(self.ids['bob'])[0]
        self.assertEqual(notice['state'], 'unknown')

    def test_human_retry_requires_nonreceipt_confirmation_and_is_idempotent(self):
        self.bind()
        self.task()
        self.app.notifier.sender = lambda *args: ('unknown', 'unknown')
        self.app.notifier.tick()
        notice = self.s.notifications(self.ids['bob'])[0]
        with self.assertRaises(TeamError):
            self.app.notifier.retry(self.ids['bob'], notice['id'], self.data())
        payload = self.data(confirmed_not_received=True)
        first = self.app.notifier.retry(self.ids['bob'], notice['id'], payload)
        self.assertEqual(first, self.app.notifier.retry(self.ids['bob'], notice['id'], payload))
        self.app.notifier.sender = lambda *args: ('submitted_unverified', '已提交')
        self.app.notifier.tick()
        self.assertEqual(self.s.notifications(self.ids['bob'])[0]['state'], 'submitted_unverified')
        with self.assertRaises(TeamError):
            self.app.notifier.retry(self.ids['bob'], notice['id'], self.data(confirmed_not_received=True))

    def test_delivery_claim_survives_restart_as_unknown_after_lease(self):
        from zhishi.team.application import TeamApplication
        self.bind()
        self.task()
        notice = self.app.notifier.claim()
        restarted = TeamApplication(self.db, bridge_secret=BRIDGE_SECRET)
        self.assertIsNone(restarted.notifier.claim())
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_notifications SET lease_until=? WHERE id=?', (stamp(now() - timedelta(seconds=1)), notice['id']))
        self.assertIsNone(restarted.notifier.claim())
        self.assertEqual(self.s.notifications(self.ids['bob'])[0]['state'], 'unknown')

    def test_binding_change_and_task_version_change_block_old_retries(self):
        self.bind()
        task = self.task()
        self.app.notifier.sender = lambda *args: ('unknown', 'unknown')
        self.app.notifier.tick()
        notice = self.s.notifications(self.ids['bob'])[0]
        self.action('bob', task, 'accept')
        with self.assertRaises(TeamError):
            self.app.notifier.retry(self.ids['bob'], notice['id'], self.data(confirmed_not_received=True))
        self.bind(peer='another-peer')
        with self.assertRaises(TeamError):
            self.app.notifier.retry(self.ids['bob'], notice['id'], self.data(confirmed_not_received=True))

    def test_pending_message_is_cancelled_before_sending_after_task_changes(self):
        self.bind()
        task = self.task()
        self.action('bob', task, 'accept')
        calls = []
        self.app.notifier.sender = lambda *args: calls.append(args)
        self.app.notifier.tick()
        self.assertEqual(calls, [])

    def test_due_reminder_is_created_once_and_inbox_is_always_available(self):
        task = self.task()
        with self.app.store.connect(write=True) as db:
            db.execute('UPDATE team_tasks SET remind_at=? WHERE id=?', (stamp(now() - timedelta(seconds=1)), task['id']))
        self.app.notifier.reminders()
        self.app.notifier.reminders()
        notices = self.s.notifications(self.ids['bob'])
        self.assertEqual(sum('到期提醒' in n['text'] for n in notices), 1)
        self.assertTrue(all(n['state'] == 'delivered_local' for n in notices))
