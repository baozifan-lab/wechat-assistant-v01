import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from zhishi.config import ROOT
from zhishi.core.clock import CN
from zhishi.models.workflow import WorkflowModel
from zhishi.service import Pilot
from zhishi.workflows.ai_dispatch import AIDispatch
from zhishi.workflows.ai_dispatch.plans import load
from zhishi.workflows.ai_dispatch.skills import ProductSkills
from zhishi.workflows.product_demo import ProductDemo


class FakeModel:
    action = 'accept'
    confidence = 1
    error = None

    def settings(self):
        return {'ready': True, 'model': 'fake'}

    def interpret(self, context, text):
        self.context = context
        if self.error:
            raise self.error
        return {'action': self.action, 'confidence': self.confidence, 'reason': '测试回复判断'}


class FakeSkills:
    def __init__(self):
        self.calls = []
        self.changed = False

    def execute(self, role, run):
        self.calls.append(role)
        return {'summary': role + ' 已生成待核对材料'}

    def validate(self, run, role):
        if self.changed:
            raise ValueError('文件已变化')

    def verify(self, run):
        self.validate(run, 'employee2')
        return {'passed': True, 'summary': '最终校验通过，未执行实际作废。'}

    def attachments(self, run):
        return []


class FakeChannel:
    enabled = True

    def __init__(self):
        self.binding = {'account_id': 'account', 'peer_id': 'peer'}
        self.calls = []
        self.send_state = 'submitted_unverified'

    def route(self):
        return self.binding.copy()

    def status(self):
        return {'enabled':True,'bound':True}

    def send_package(self, text, media, *, expected_route):
        self.calls.append(text)
        return self.send_state, '测试发送状态'


class AIDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'projects'/'ai-dispatch'/'tasks.json'
        self.path.parent.mkdir(parents=True)
        self.task = {
            'id':'product-investigation','title':'隔离测试的商品排查','enabled':True,
            'schedule':{'kind':'manual'},'roles':dict.fromkeys(('manager','employee1','employee2'),'local'),
            'allow_shared_routes':False,'input_directory':str(self.root/'inputs'),'source':'file',
            'skills':{'employee1':'product.inputs.v1','employee2':'product.compare.v1','final':'product.verify.v1'},
        }
        self.save()
        self.pilot = Pilot(self.root/'test.db')
        self.model, self.skills = FakeModel(), FakeSkills()
        self.flow = AIDispatch(self.pilot, self.root, model=self.model, skills=self.skills)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps({'tasks': [self.task]}), encoding='utf-8')

    def start(self):
        code = self.flow.start(self.task['id'], 'start-request-0001')
        self.flow.notify_tick()
        return code

    def test_named_parallel_run_routes_only_explicit_task(self):
        self.task.update(roles={'manager':'primary','employee1':'primary','employee2':'employee2'}, allow_shared_routes=True)
        self.save()
        self.pilot.transport, self.pilot.employee2_transport = FakeChannel(), FakeChannel()
        old = self.flow.start(self.task['id'], 'old-request-1234')
        new = self.flow.start(self.task['id'], 'new-request-1234', '商品核查1')
        self.assertNotEqual(old, new)
        self.flow.notify_tick()
        self.flow.notify_tick()
        self.assertIn('商品核查1', self.pilot.transport.calls[-1])
        self.assertIn('收到', self.flow.command('商品核查1 确认', 'primary', 'reply-named-1234'))
        self.flow.work_tick()
        self.assertEqual(self.flow.row(old)['state'], 'manager_wait')
        self.assertEqual(self.flow.row(new)['state'], 'employee1_wait')
        with self.assertRaisesRegex(ValueError, '已使用'):
            self.flow.start(self.task['id'], 'new-request-5678', '商品核查1')

    def test_manager_confirmation_is_handled_without_model(self):
        from zhishi.workflows.ai_dispatch.intents import explicit_intent
        self.assertEqual(explicit_intent('manager_wait', '确认')['action'], 'accept')

    def reply(self, code, role, action, text='自然语言回复', channel='local'):
        self.counter += 1
        self.model.action = action
        self.flow.submit(code, role, text, 'reply-request-'+str(self.counter), channel=channel)
        self.flow.work_tick()

    def to_employee1_review(self, code):
        self.reply(code, 'manager', 'accept')
        self.flow.notify_tick()
        self.reply(code, 'employee1', 'help')
        self.flow.work_tick()
        self.flow.notify_tick()

    def to_employee2_review(self, code):
        self.to_employee1_review(code)
        self.reply(code, 'employee1', 'complete')
        self.flow.notify_tick()
        self.reply(code, 'employee2', 'help')
        self.flow.work_tick()
        self.flow.notify_tick()

    def test_employee_problem_requests_workbench_correction(self):
        code = self.start()
        self.to_employee1_review(code)
        self.reply(code, 'employee1', 'blocked', '数据范围不对，员工2清单前五条不应包含')
        run = self.flow.row(code)
        self.assertEqual(run['state'], 'blocked')
        notices = [n for n in self.flow.state()['runs'][0]['notices'] if n['revision'] == run['revision']]
        self.assertTrue(any(n['role'] == 'manager' and '上传文件更正任务' in n['text'] for n in notices))
        self.assertTrue(any(n['role'] == 'employee1' and '修正版交给工作台使用者' in n['text'] for n in notices))
    def test_full_chain_requires_separate_reviews_and_final_report(self):
        code = self.start()
        self.to_employee2_review(code)
        self.assertEqual(self.skills.calls, ['employee1', 'employee2'])
        self.assertEqual(self.flow.row(code)['state'], 'employee2_review')
        self.reply(code, 'employee2', 'complete')
        self.assertEqual(self.flow.row(code)['state'], 'final_check')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'report_pending')
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'], 'complete')
        notices = self.flow.state()['runs'][0]['notices']
        self.assertEqual([n['role'] for n in notices], ['manager','employee1','employee1','employee2','employee2','manager'])

    def test_employee_claim_of_completion_only_starts_skill_before_review(self):
        code = self.start()
        self.reply(code, 'manager', 'accept')
        self.flow.notify_tick()
        self.reply(code, 'employee1', 'complete', '都做好了')
        self.assertEqual(self.flow.row(code)['state'], 'employee1_work')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'employee1_review')
        self.assertNotIn('employee1_confirmed', self.flow.row(code)['evidence'])

    def test_low_confidence_and_questions_do_not_advance(self):
        code = self.start()
        self.model.confidence = .5
        self.reply(code, 'manager', 'accept', '也许吧')
        self.assertEqual(self.flow.row(code)['state'], 'manager_wait')
        self.model.confidence = 1
        self.reply(code, 'manager', 'unclear', '这是做什么的？')
        self.assertEqual(self.flow.row(code)['state'], 'manager_wait')

    def test_api_failure_keeps_stage_and_records_error(self):
        code = self.start()
        self.model.error = ValueError('API 暂时不可用')
        self.reply(code, 'manager', 'accept')
        run = self.flow.state()['runs'][0]
        self.assertEqual(run['state'], 'manager_wait')
        self.assertEqual(run['replies'][0]['state'], 'failed')

    def test_duplicate_start_and_reply_are_idempotent(self):
        code = self.start()
        self.assertEqual(code, self.flow.start(self.task['id'], 'start-request-0001'))
        for _ in range(2):
            self.flow.submit(code, 'manager', '同意', 'same-request-001')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'employee1_wait')
        with self.pilot.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ai_dispatch_inbox').fetchone()[0], 1)

    def test_wrong_role_and_early_reply_rejected(self):
        code = self.flow.start(self.task['id'], 'start-request-0001')
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'manager', '同意', 'reply-request-0001')
        self.flow.notify_tick()
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'employee2', '完成', 'reply-request-0002')

    def test_old_queued_reply_cannot_approve_next_stage(self):
        code = self.start()
        self.flow.submit(code, 'manager', '同意', 'reply-request-0001')
        self.flow.submit(code, 'manager', '同意，开始吧', 'reply-request-0002')
        self.flow.work_tick()
        self.model.action = 'complete'
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'employee1_wait')
        self.assertEqual(self.flow.state()['runs'][0]['replies'][1]['state'], 'failed')

    def test_tampered_review_cannot_handoff_or_report(self):
        code = self.start()
        self.to_employee1_review(code)
        self.skills.changed = True
        self.reply(code, 'employee1', 'complete')
        self.assertEqual(self.flow.row(code)['state'], 'employee1_review')
        self.assertNotIn('employee2', self.skills.calls)

    def test_final_failure_blocks_instead_of_reporting_complete(self):
        code = self.start()
        self.to_employee2_review(code)
        self.reply(code, 'employee2', 'complete')
        self.skills.changed = True
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'blocked')
        self.assertFalse(any(n['kind'] == 'report_pending' for n in self.flow.state()['runs'][0]['notices']))

    def test_daily_schedule_beijing_time_and_restart_do_not_duplicate(self):
        self.task['schedule'] = {'kind':'daily', 'at':'09:00'}
        self.save()
        self.flow.schedule_tick(datetime(2026, 11, 2, 8, 59, tzinfo=CN))
        self.assertEqual(self.flow.state()['runs'], [])
        self.flow.schedule_tick(datetime(2026, 11, 2, 9, 0, tzinfo=CN))
        restarted = AIDispatch(self.pilot, self.root, model=self.model, skills=self.skills)
        restarted.schedule_tick(datetime(2026, 11, 2, 12, 0, tzinfo=CN))
        self.assertEqual(len(restarted.state()['runs']), 1)

    def test_disabled_task_and_unconfigured_model_never_send(self):
        self.task['enabled'] = False
        self.save()
        with self.assertRaises(ValueError):
            self.start()
        self.task['enabled'] = True
        self.save()
        with patch.dict(os.environ, {}, clear=True):
            self.flow.model = WorkflowModel()
            with self.assertRaises(ValueError):
                self.start()
        self.assertEqual(self.flow.state()['runs'], [])

    def test_changed_channel_and_browser_impersonation_rejected(self):
        self.task['roles'] = dict.fromkeys(('manager','employee1','employee2'), 'primary')
        self.task['allow_shared_routes'] = True
        self.save()
        channel = FakeChannel()
        self.pilot.transport = channel
        code = self.start()
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'manager', '同意', 'reply-request-001')
        channel.binding['peer_id'] = 'someone-else'
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'manager', '同意', 'reply-request-001', channel='primary')

    def test_unknown_send_is_not_retried_or_accepted_as_delivered(self):
        self.task['roles'] = dict.fromkeys(('manager','employee1','employee2'), 'primary')
        self.task['allow_shared_routes'] = True
        self.save()
        channel = FakeChannel()
        channel.send_state = 'unknown'
        self.pilot.transport = channel
        code = self.start()
        self.flow.notify_tick()
        self.assertEqual(len(channel.calls), 1)
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'manager', '同意', 'reply-request-001', channel='primary')

    def test_restart_retains_waiting_but_blocks_interrupted_skill(self):
        code = self.start()
        restarted = AIDispatch(self.pilot, self.root, model=self.model, skills=self.skills)
        self.assertEqual(restarted.row(code)['state'], 'manager_wait')
        self.to_employee1_review(code)
        with self.pilot.connect() as db:
            db.execute("UPDATE ai_dispatch_runs SET state='employee2_work' WHERE code=?", (code,))
        restarted = AIDispatch(self.pilot, self.root, model=self.model, skills=self.skills)
        self.assertEqual(restarted.row(code)['state'], 'blocked')

    def test_connect_existing_wechat_sends_manager_once_and_blocks_local_impersonation(self):
        code = self.start()
        primary, second = FakeChannel(), FakeChannel()
        second.binding['peer_id'] = 'second-peer'
        self.pilot.transport, self.pilot.employee2_transport = primary, second
        self.flow.connect_wechat(code)
        self.flow.connect_wechat(code)
        run = self.flow.row(code)
        self.assertEqual(run['task']['roles'], {'manager':'primary','employee1':'primary','employee2':'employee2'})
        self.assertEqual(run['revision'], 1)
        self.flow.notify_tick()
        self.flow.notify_tick()
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(second.calls, [])
        with self.assertRaises(ValueError):
            self.flow.submit(code, 'manager', '同意', 'local-reply-after-connect')
        self.reply(code, 'manager', 'accept', channel='primary')
        self.assertEqual(self.flow.row(code)['state'], 'employee1_wait')

    def test_connect_wechat_requires_both_channels_and_no_outstanding_reply(self):
        code = self.start()
        self.pilot.transport = FakeChannel()
        with self.assertRaises(ValueError):
            self.flow.connect_wechat(code)
        self.assertEqual(self.flow.row(code)['task']['roles']['manager'], 'local')
        self.pilot.employee2_transport = FakeChannel()
        self.flow.submit(code, 'manager', '同意', 'pending-local-reply')
        with self.assertRaises(ValueError):
            self.flow.connect_wechat(code)
        self.assertEqual(self.flow.row(code)['revision'], 0)

    def test_task_cannot_select_arbitrary_skill_or_silently_share_roles(self):
        self.task['skills']['employee1'] = 'execute.anything'
        self.save()
        with self.assertRaises(ValueError):
            load(self.path)
        self.task['skills']['employee1'] = 'product.inputs.v1'
        self.task['roles'] = dict.fromkeys(('manager','employee1','employee2'), 'primary')
        self.save()
        with self.assertRaises(ValueError):
            load(self.path)

    def test_project_brief_is_supplied_and_bound_to_original_version(self):
        brief = self.path.parent/'项目说明.md'
        brief.write_text('原版：负责人确认后再派给员工1。',encoding='utf-8')
        code = self.start()
        brief.write_text('新版：不要悄悄替换运行中说明。',encoding='utf-8')
        self.reply(code,'manager','accept')
        self.assertIn('原版',self.model.context['project_brief']['text'])
        self.assertNotIn('新版',self.model.context['project_brief']['text'])
        self.assertIn('决定是否开始',self.model.context['stage_notification'])

    def test_legacy_run_loads_explicit_brief_before_its_next_model_call(self):
        code=self.start()
        self.assertNotIn('project_brief',self.flow.row(code)['task'])
        (self.path.parent/'项目说明.md').write_text('商品交集核查，不执行商品作废。',encoding='utf-8')
        self.reply(code,'manager','accept')
        self.assertIn('不执行商品作废',self.model.context['project_brief']['text'])
        self.assertTrue(self.flow.row(code)['task']['project_brief']['sha256'])

    def test_project_brief_cannot_escape_project_directory(self):
        self.task['project_document']='../../secret.md'
        self.save()
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.flow.state()['runs'],[])

    def test_retry_failed_reply_advances_once_and_cannot_replay_next_stage(self):
        code=self.start()
        self.model.error=ValueError('模型接口超时')
        self.reply(code,'manager','accept')
        self.model.error=None
        self.flow.retry_reply(code,'reply-request-1')
        self.flow.retry_reply(code,'reply-request-1')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee1_wait')
        self.flow.retry_reply(code,'reply-request-1')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee1_wait')
        self.assertEqual(len([n for n in self.flow.state()['runs'][0]['notices'] if n['kind']=='employee1_wait']),1)

    def test_retry_cannot_apply_older_reply_when_user_sent_newer_intent(self):
        code=self.start()
        self.model.error=ValueError('模型接口超时')
        self.reply(code,'manager','accept','这个计划我觉得可以推进')
        self.reply(code,'manager','blocked','先别启动')
        with self.assertRaises(ValueError):
            self.flow.retry_reply(code,'reply-request-1')

    def test_exact_completion_claim_and_check_request_do_not_need_api(self):
        code=self.start()
        self.reply(code,'manager','accept')
        self.flow.notify_tick()
        self.model.error=ValueError('第三方接口限流')
        self.reply(code,'employee1','unclear','文件已导出，请检查文件并继续')
        self.assertEqual(self.flow.row(code)['state'],'employee1_work')
        self.flow.work_tick()
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')
        self.assertNotIn('employee1_confirmed',self.flow.row(code)['evidence'])
        self.assertEqual(self.skills.calls,['employee1'])
        events=self.flow.state()['runs'][0]['events']
        self.assertTrue(any(e['kind']=='rule_decision' for e in events))

    def test_positive_completion_claim_starts_check_but_negations_are_not_rules(self):
        from zhishi.workflows.ai_dispatch.intents import explicit_intent
        self.assertEqual(explicit_intent('employee1_wait','已完成')['action'],'complete')
        for text in ['还没完成','如果文件没问题就继续','请不要检查文件','文件已导出了吗？','已完成但有错误']:
            self.assertIsNone(explicit_intent('employee1_wait',text))
        self.assertIsNone(explicit_intent('employee1_review','已完成'))

    def real_review(self):
        code=self.start()
        self.pilot.ai_dispatch=self.flow
        self.pilot.transport=FakeChannel()
        self.pilot.employee2_transport=FakeChannel()
        self.pilot.employee2_transport.binding['peer_id']='employee2'
        self.flow.connect_wechat(code)
        self.flow.notify_tick()
        self.pilot.chat('同意，开始吧','chat-manager-accept',wechat_role='primary')
        self.flow.work_tick()
        self.flow.notify_tick()
        self.pilot.chat('已完成','chat-worker1-claim',wechat_role='primary')
        self.flow.work_tick()
        self.flow.work_tick()
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')
        return code

    def test_bare_authenticated_confirmation_routes_to_only_waiting_workflow(self):
        code=self.real_review()
        first=self.pilot.chat('已核对，结果无误','chat-worker1-review',wechat_role='primary')
        second=self.pilot.chat('已核对，结果无误','chat-worker1-review',wechat_role='primary')
        self.assertEqual(first,second)
        self.assertIn('收到',first['reply'])
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee2_wait')
        self.assertEqual(self.pilot.state()['tasks'],[])
        status=self.pilot.chat('已核对，结果无误','chat-worker1-review-again',wechat_role='primary')
        self.assertIn('等待员工2',status['reply'])

    def test_short_confirmation_is_not_swallowed_by_legacy_workflow_hint(self):
        from zhishi.workflows.service import Workflows
        self.pilot.workflow = Workflows(self.pilot, self.root)
        code = self.real_review()
        result = self.pilot.chat('没问题', 'chat-short-review-with-legacy', wechat_role='primary')
        self.assertIn('收到', result['reply'])
        self.assertNotIn('JOB-', result['reply'])
        self.assertEqual(result, self.pilot.chat('没问题', 'chat-short-review-with-legacy', wechat_role='primary'))
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'], 'employee2_wait')
        self.assertEqual(self.pilot.workflow.state()['jobs'], [])

    def test_short_confirmation_still_requires_choice_when_another_flow_waits(self):
        from zhishi.workflows.service import Workflows
        self.pilot.workflow = Workflows(self.pilot, self.root)
        code = self.real_review()
        with self.pilot.connect() as db:
            db.execute('CREATE TABLE product_demo_runs(state TEXT)')
            db.execute("INSERT INTO product_demo_runs VALUES ('manager_wait')")
        result = self.pilot.chat('没问题', 'chat-short-review-ambiguous', wechat_role='primary')
        self.assertIn('第一个', result['reply'])
        self.assertNotIn('JOB-', result['reply'])
        self.assertEqual(self.flow.row(code)['state'], 'employee1_review')

    def test_explicit_job_confirmation_keeps_its_own_workflow(self):
        from zhishi.workflows.service import Workflows
        self.pilot.workflow = Workflows(self.pilot, self.root)
        code = self.real_review()
        with patch.object(self.pilot.workflow, 'approve', return_value='旧文档工作流已处理') as approve:
            result = self.pilot.chat('确认发送 JOB-20261101-001 V1', 'chat-explicit-job-route', wechat_role='primary')
        approve.assert_called_once_with('JOB-20261101-001', 1)
        self.assertEqual(result['reply'], '旧文档工作流已处理')
        self.assertEqual(self.flow.row(code)['state'], 'employee1_review')

    def test_legacy_generic_hint_remains_without_ai_context(self):
        from zhishi.workflows.service import Workflows
        self.pilot.workflow = Workflows(self.pilot, self.root)
        result = self.pilot.chat('没问题', 'chat-no-ai-legacy-hint', wechat_role='primary')
        self.assertIn('JOB-', result['reply'])

    def test_workflow_code_can_touch_chinese_reply_without_a_space(self):
        code=self.real_review()
        reply=self.pilot.chat(code+'已核对，结果无误','chat-no-space-review',wechat_role='primary')
        self.assertIn('收到',reply['reply'])
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee2_wait')
        self.assertEqual(self.pilot.state()['tasks'],[])

    def test_reminder_commands_keep_working_during_workflow_review(self):
        code=self.real_review()
        self.pilot.chat('10 分钟后提醒我提交周报','chat-reminder-priority',wechat_role='primary')
        self.assertEqual(len(self.pilot.state()['tasks']),1)
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_bare_reply_cannot_choose_between_two_active_workflows(self):
        code=self.real_review()
        second=json.loads(json.dumps(self.task))
        second.update(id='other-investigation',roles={'manager':'primary','employee1':'primary','employee2':'employee2'},allow_shared_routes=True)
        self.path.write_text(json.dumps({'tasks':[self.task,second]}),encoding='utf-8')
        self.flow.start(second['id'],'start-other-request')
        reply=self.pilot.chat('已核对，结果无误','chat-ambiguous-review',wechat_role='primary')
        self.assertIn('第一个',reply['reply'])
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_other_workflow_waiting_for_same_account_requires_explicit_code(self):
        code=self.real_review()
        with self.pilot.connect() as db:
            db.execute('CREATE TABLE product_demo_runs(state TEXT)')
            db.execute("INSERT INTO product_demo_runs VALUES ('manager_wait')")
        reply=self.pilot.chat('已核对，结果无误','chat-other-flow-conflict',wechat_role='primary')
        self.assertIn('第一个',reply['reply'])
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_other_bound_account_cannot_confirm_employee1_by_bare_reply(self):
        code=self.real_review()
        response=self.pilot.chat('已核对，结果无误','chat-wrong-actor-bare',wechat_role='employee2')
        self.assertIn('等待员工1',response['reply'])
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_bare_repeat_confirmation_after_completion_reports_status(self):
        code=self.real_review()
        with self.pilot.connect() as db:
            db.execute("UPDATE ai_dispatch_runs SET state='complete' WHERE code=?",(code,))
        result=self.pilot.chat('已核对，结果无误','chat-after-complete',wechat_role='primary')
        self.assertIn('校验完成，汇报已提交',result['reply'])
        self.assertNotIn('提醒时间',result['reply'])
        self.assertEqual(self.flow.row(code)['state'],'complete')

    def test_natural_short_reply_chain_distinguishes_receipt_readiness_and_review(self):
        code=self.start()
        self.model.error=ValueError('模拟 API 暂时不可用')
        self.reply(code,'manager','unclear','好的')
        self.flow.notify_tick()
        self.reply(code,'employee1','unclear','收到')
        self.assertEqual(self.flow.row(code)['state'],'employee1_wait')
        self.assertEqual(self.skills.calls,[])
        self.assertTrue(self.flow.row(code)['evidence']['employee1_acknowledged'])
        self.flow.notify_tick()
        self.reply(code,'employee1','unclear','导好了')
        self.flow.work_tick()
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')
        self.reply(code,'employee1','unclear','收到')
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')
        self.assertNotIn('employee1_confirmed',self.flow.row(code)['evidence'])
        self.flow.notify_tick()
        self.reply(code,'employee1','unclear','没问题')
        self.flow.notify_tick()
        self.reply(code,'employee2','unclear','收到')
        self.flow.work_tick()
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee2_review')
        self.reply(code,'employee2','unclear','核对过了')
        self.flow.work_tick()
        self.flow.notify_tick()
        self.assertEqual(self.flow.row(code)['state'],'complete')
        self.assertEqual(self.skills.calls,['employee1','employee2'])

    def test_repeated_text_while_processing_does_not_queue_another_model_call(self):
        code=self.start()
        self.flow.submit(code,'manager','这个安排我想想再说','duplicate-text-1')
        reply=self.flow.submit(code,'manager','这个安排我想想再说','duplicate-text-2')
        self.assertIn('已经在处理',reply)
        with self.pilot.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ai_dispatch_inbox').fetchone()[0],1)

    def two_choices(self):
        code=self.real_review()
        second=json.loads(json.dumps(self.task))
        second.update(id='other-investigation',roles={'manager':'primary','employee1':'primary','employee2':'employee2'},allow_shared_routes=True)
        self.path.write_text(json.dumps({'tasks':[self.task,second]}),encoding='utf-8')
        other=self.flow.start(second['id'],'start-another-request')
        self.flow.notify_tick()
        response=self.pilot.chat('没问题','choice-original-message',wechat_role='primary')
        self.assertNotIn('AIW-',response['reply'])
        with self.pilot.connect() as db:
            record=db.execute('SELECT * FROM ai_dispatch_selections').fetchone()
            options=json.loads(record['options_json'])
        selected=next(i for i,o in enumerate(options) if o['code']==code)
        return code,other,'第'+'一二三四五六七八九'[selected]+'个'

    def test_plain_ordinal_selects_task_and_preserves_original_reply_once(self):
        code,other,selection=self.two_choices()
        response=self.pilot.chat(selection,'choice-selection-message',wechat_role='primary')
        self.assertIn('收到',response['reply'])
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee2_wait')
        self.assertEqual(self.flow.row(other)['state'],'manager_wait')
        with self.pilot.connect() as db:
            row=db.execute('SELECT text FROM ai_dispatch_inbox WHERE request_id=?',('choice-original-message',)).fetchone()
            self.assertEqual(row['text'],'没问题')
            self.assertEqual(db.execute('SELECT count(*) FROM ai_dispatch_selections').fetchone()[0],0)
        self.pilot.chat(selection,'choice-selection-message',wechat_role='primary')
        self.assertEqual(self.flow.row(other)['state'],'manager_wait')

    def test_choice_survives_restart_and_is_scoped_to_bound_account(self):
        code,other,selection=self.two_choices()
        self.flow=AIDispatch(self.pilot,self.root,model=self.model,skills=self.skills)
        self.pilot.ai_dispatch=self.flow
        self.pilot.chat(selection,'wrong-account-choice',wechat_role='employee2')
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')
        self.pilot.chat(selection,'right-account-choice',wechat_role='primary')
        self.flow.work_tick()
        self.assertEqual(self.flow.row(code)['state'],'employee2_wait')

    def test_choice_does_not_apply_to_changed_stage(self):
        code,other,selection=self.two_choices()
        with self.pilot.connect() as db:
            db.execute("UPDATE ai_dispatch_runs SET state='employee2_wait',revision=revision+1 WHERE code=?",(code,))
        response=self.pilot.chat(selection,'stale-stage-choice',wechat_role='primary')
        self.assertIn('进度已经变化',response['reply'])
        self.assertEqual(self.flow.row(other)['state'],'manager_wait')

    def test_choice_expires_without_using_old_confirmation(self):
        code,other,selection=self.two_choices()
        with self.pilot.connect() as db:
            db.execute("UPDATE ai_dispatch_selections SET created='2000-01-01T00:00:00+08:00'")
        response=self.pilot.chat(selection,'expired-choice',wechat_role='primary')
        self.assertIn('过期',response['reply'])
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_dispatch_messages_explain_work_and_handoff_without_long_code(self):
        preview=self.flow.message_preview()
        self.assertTrue(preview['preview'])
        texts={m['stage']:m['text'] for m in preview['messages']}
        self.assertIn('你来做',texts['employee1_wait'])
        self.assertIn('我来做',texts['employee1_wait'])
        self.assertIn('协作',texts['employee1_wait'])
        self.assertIn('同一版',texts['employee2_wait'])
        self.assertIn('最终检查',texts['manager_wait'])
        self.assertFalse(any('AIW-' in t for t in texts.values()))
        self.assertEqual(self.flow.state()['runs'],[])

    def test_task_question_returns_current_duties_without_api_or_advancing(self):
        code=self.real_review()
        self.model.error=ValueError('不应请求模型')
        response=self.pilot.chat('我需要做什么？','task-duty-question',wechat_role='primary')
        self.assertIn('请你确认',response['reply'])
        self.assertIn('员工2',response['reply'])
        self.assertEqual(self.flow.row(code)['state'],'employee1_review')

    def test_conditional_or_negative_short_text_is_not_automatic_approval(self):
        from zhishi.workflows.ai_dispatch.intents import explicit_intent
        for state in ('manager_wait','employee1_review','employee2_review'):
            for text in ('好像有问题','不是没问题','如果没问题就继续','我还没核对','收到但是有一行不对'):
                self.assertIsNone(explicit_intent(state,text))


class WorkflowModelTests(unittest.TestCase):
    def test_model_contract_rejects_extra_tools_and_invalid_confidence(self):
        for value in [[], {'action': 'complete', 'confidence': True, 'reason': ''},
                      {'action': [], 'confidence': .9, 'reason': ''},
                      {'action': 'complete', 'confidence': float('nan'), 'reason': ''},
                      {'action': 'complete', 'confidence': 1, 'reason': '', 'tool': 'shell'}]:
            with self.assertRaises(ValueError):
                WorkflowModel.validate(value)

    def test_compatible_api_request_and_no_secret_in_public_settings(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'choices':[{'message':{'content':json.dumps({'action':'help','confidence':.95,'reason':'请求协助'})}}]}).encode()
        with patch.dict(os.environ, {'WORKFLOW_MODEL_BASE_URL':'http://127.0.0.1:4000/v1','WORKFLOW_MODEL_API_KEY':'private-test-key','WORKFLOW_MODEL':'planner-strong'}):
            with patch.object(WorkflowModel, '_open', return_value=Response()) as call:
                decision = WorkflowModel().interpret({'role':'员工1'}, '帮我检查一下')
            request = call.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:4000/v1/chat/completions')
            self.assertEqual(json.loads(request.data)['model'], 'planner-strong')
            self.assertEqual(decision['action'], 'help')
            self.assertNotIn('private-test-key', json.dumps(WorkflowModel().settings()))


class ProductSkillTests(unittest.TestCase):
    def setUp(self):
        from openpyxl import Workbook
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = {'input_directory': str(self.root/'inputs'), 'source': 'file'}
        for role, rows in [('员工1', [('A1','商品1','正常'),('A2','商品2','正常')]),
                           ('员工2', [('A2','商品2','作废'),('A3','商品3','作废'),('A1','商品1','正常')])]:
            path = self.root/'inputs'/role/'input.xlsx'
            path.parent.mkdir(parents=True)
            book = Workbook()
            book.active.append(['商品编码','商品名称','现商品状态'])
            for row in rows:
                book.active.append(row)
            book.save(path)
            book.close()
        self.pilot = Pilot(self.root/'test.db')
        self.skills = ProductSkills(ProductDemo(self.pilot, self.root))
        self.run = {'code': 'AIW-20261102-1234ABCD', 'task': self.task, 'evidence': {}}

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        self.run['evidence']['employee1'] = self.skills.execute('employee1', self.run)
        self.run['evidence']['employee2'] = self.skills.execute('employee2', self.run)

    def test_actual_workbooks_compare_and_verify_without_external_services(self):
        self.prepare()
        result = self.run['evidence']['employee2']
        self.assertEqual(result['matched_rows'], 1)
        self.assertEqual(result['matched_codes'], 1)
        self.assertTrue(self.skills.verify(self.run)['passed'])
        self.assertEqual(len(self.skills.attachments(self.run)), 1)

    def test_tampered_generated_csv_blocks_final_check_and_download(self):
        self.prepare()
        path = Path(self.run['evidence']['employee2']['result'])
        path.write_text('被改动的结果', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.skills.verify(self.run)
        with self.assertRaises(ValueError):
            self.skills.attachments(self.run)

    def test_changed_input_blocks_next_skill_and_review(self):
        self.run['evidence']['employee1'] = self.skills.execute('employee1', self.run)
        path = self.root/'inputs'/'员工1'/'input.xlsx'
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaises(ValueError):
            self.skills.execute('employee2', self.run)
        with self.assertRaises(ValueError):
            self.skills.validate(self.run, 'employee1')


if __name__ == '__main__':
    unittest.main()
