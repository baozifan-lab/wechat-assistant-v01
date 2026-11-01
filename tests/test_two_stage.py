import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from zhishi.config import ROOT
from zhishi.service import Pilot
from zhishi.workflows.files import digest, encoded
from zhishi.workflows.two_stage import TwoStageDemo


class FakeOwnerWechat:
    enabled = True

    def __init__(self):
        self.binding = {'account_id':'fixture-account','peer_id':'fixture-owner'}
        self.sent = []

    def route(self):
        return dict(self.binding)

    def send_package(self, text, media, *, expected_route=None):
        assert expected_route==self.binding
        self.sent.append((text,list(media),dict(expected_route)))
        return 'submitted_unverified','fixture channel submitted'


class TwoStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project = self.root/'projects'/'excel-demo'
        (project/'技能').mkdir(parents=True)
        (project/'待处理').mkdir()
        for name in ['项目文档.md','收件人.json','技能/Excel整合.md']:
            shutil.copyfile(ROOT/'projects'/'excel-demo'/name,project/name)
        for file in (ROOT/'projects'/'excel-demo'/'待处理').glob('*.xlsx'):
            shutil.copyfile(file,project/'待处理'/file.name)
        (self.root/'workflows').mkdir()
        for name in ['excel_worker.mjs','department_summary.mjs']:
            (self.root/'workflows'/name).write_text('// fixture',encoding='utf-8')
        self.pilot = Pilot(self.root/'test.db')
        self.demo = TwoStageDemo(self.pilot,self.root)
        self.pilot.two_stage = self.demo

    def tearDown(self):
        self.temp.cleanup()

    def current(self):
        return self.demo.state()['runs'][0]

    def ready_a(self, *, wechat=False):
        self.demo.start(wechat=wechat)
        def execute(run):
            folder = self.demo.output/run['code']/'V1'
            folder.mkdir(parents=True)
            fixture = ROOT/'tests'/'fixtures'/'excel-result'
            names = ['整合结果_示例.xlsx','预览.png','异常清单.csv','完成说明.md','检查结果.json']
            for name in names:
                shutil.copyfile(fixture/name,folder/name)
            return json.loads((folder/'检查结果.json').read_text('utf-8')),self.demo.manifest(folder,names)
        with patch.object(self.demo.executor,'execute',side_effect=execute):
            self.demo.tick()
        self.assertEqual(self.current()['state'],'a_review')
        return self.current()

    @staticmethod
    def summary_worker(command, **kwargs):
        spec = json.loads(Path(command[-1]).read_text('utf-8'))
        folder = Path(spec['output'])
        report = {'demo':True,'model_used':False,'upstream_code':spec['code'],'upstream_version':1,
                  'rows':6,'total_amount':5000.75,'departments':[
                      {'department':'销售一部','rows':3,'total_amount':1850.75},
                      {'department':'销售二部','rows':3,'total_amount':3150}]}
        (folder/'部门汇总.json').write_text(encoded(report),encoding='utf-8')
        (folder/'部门汇总.md').write_text('示例汇总结果',encoding='utf-8')
        return Mock(returncode=0,stdout='ok',stderr='')

    def ready_b(self):
        run = self.ready_a()
        self.demo.approve(run['code'],1,'A','A')
        with patch('zhishi.workflows.two_stage.subprocess.run',side_effect=self.summary_worker) as worker:
            self.demo.tick()
        worker.assert_called_once()
        self.assertEqual(self.current()['state'],'b_review')
        return self.current()

    def test_b_cannot_start_before_a_approval(self):
        self.ready_a()
        with patch.object(self.demo,'execute_b') as second:
            self.demo.tick()
        second.assert_not_called()
        self.assertEqual(self.current()['events'],[])
        self.assertEqual(self.current()['b_attempts'],0)

    def test_a_approval_atomically_queues_exactly_one_handoff(self):
        run = self.ready_a()
        self.demo.approve(run['code'],1,'A','A')
        self.assertEqual(self.current()['state'],'b_queued')
        self.assertEqual(len(self.current()['events']),1)
        self.assertIn('不会重复',self.demo.approve(run['code'],1,'A','A'))
        self.assertEqual(len(self.current()['events']),1)

    def test_wrong_version_role_and_premature_b_approval_rejected(self):
        run = self.ready_a()
        for version,stage,role in [(2,'A','A'),(1,'A','B'),(1,'B','B')]:
            with self.assertRaises(ValueError):
                self.demo.approve(run['code'],version,stage,role)
        self.assertEqual(self.current()['state'],'a_review')

    def test_modified_a_file_blocks_handoff(self):
        run = self.ready_a()
        file = self.demo.output/run['a_assets'][0]['relative_path']
        file.write_bytes(b'changed')
        self.assertIn('没有交接',self.demo.approve(run['code'],1,'A','A'))
        self.assertEqual(self.current()['state'],'blocked')
        self.assertEqual(self.current()['events'],[])

    def test_modified_upstream_after_approval_stops_b_before_worker(self):
        run = self.ready_a();self.demo.approve(run['code'],1,'A','A')
        (self.demo.output/run['a_assets'][0]['relative_path']).write_bytes(b'changed')
        with patch('zhishi.workflows.two_stage.subprocess.run') as worker:
            self.demo.tick()
        worker.assert_not_called()
        self.assertEqual(self.current()['state'],'failed')
        self.assertEqual(self.current()['b_assets'],[])

    def test_restart_preserves_queued_handoff_and_b_consumes_once(self):
        run = self.ready_a();self.demo.approve(run['code'],1,'A','A')
        self.demo = TwoStageDemo(self.pilot,self.root)
        with patch('zhishi.workflows.two_stage.subprocess.run',side_effect=self.summary_worker) as worker:
            self.demo.tick();self.demo.tick()
        worker.assert_called_once()
        self.assertEqual(self.current()['events'][0]['state'],'consumed')
        self.assertEqual(self.current()['b_attempts'],1)
        handoff = json.loads((self.demo.output/run['code']/'V1'/'第二段'/'交接单.json').read_text('utf-8'))
        self.assertEqual(handoff['version'],1)
        self.assertEqual(handoff['to'],'sim-b')

    def test_b_requires_separate_confirmation(self):
        run = self.ready_b()
        self.assertEqual(run['state'],'b_review')
        self.demo.approve(run['code'],1,'B','B')
        self.assertEqual(self.current()['state'],'completed')
        self.assertEqual(len(self.current()['events']),2)
        self.assertIn('不会重复',self.demo.approve(run['code'],1,'B','B'))
        self.assertEqual(len(self.current()['events']),2)

    def test_changed_b_artifact_blocks_final_approval(self):
        run = self.ready_b()
        (self.demo.output/run['b_assets'][0]['relative_path']).write_bytes(b'changed')
        self.assertIn('没有交接',self.demo.approve(run['code'],1,'B','B'))
        self.assertEqual(self.current()['state'],'blocked')

    def test_asset_auth_stage_and_name_validation(self):
        run = self.ready_a();item = run['a_assets'][0]
        for stage,name,token in [('A',item['name'],'wrong'),('B',item['name'],run['access_token']),('A','../../outside',run['access_token'])]:
            with self.assertRaises(ValueError):
                self.demo.asset(run['uid'],stage,name,token)
        self.assertEqual(digest(self.demo.asset(run['uid'],'A',item['name'],run['access_token'])),item['sha256'])

    def test_chat_start_request_deduplicated(self):
        reply = self.pilot.chat('开始两段式验证','two-stage-fixture-001')
        self.assertEqual(reply,self.pilot.chat('开始两段式验证','two-stage-fixture-001'))
        self.demo.start()
        self.assertEqual(len(self.demo.state()['runs']),1)

    def test_wechat_mode_requires_owner_binding(self):
        with self.assertRaisesRegex(ValueError,'尚未绑定'):
            self.demo.start(wechat=True)
        self.assertEqual(self.demo.state()['runs'],[])

    def test_both_roles_send_once_to_same_pinned_owner(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        run = self.ready_a(wechat=True)
        self.demo.notifications.tick();self.demo.notifications.tick()
        self.assertEqual(len(channel.sent),1)
        self.assertIn('角色 A',channel.sent[0][0])
        self.assertEqual({p.name for p in channel.sent[0][1]},{'整合结果_示例.xlsx','预览.png','异常清单.csv'})
        self.demo.approve(run['code'],1,'A','A')
        with patch('zhishi.workflows.two_stage.subprocess.run',side_effect=self.summary_worker):
            self.demo.tick()
        self.demo.notifications.tick();self.demo.notifications.tick()
        self.assertEqual(len(channel.sent),2)
        self.assertIn('角色 B',channel.sent[1][0])
        self.assertEqual({p.name for p in channel.sent[1][1]},{'部门汇总.csv','整合结果_示例.xlsx'})
        self.assertTrue(all(message[2]==channel.binding for message in channel.sent))
        self.demo.approve(run['code'],1,'B','B')
        self.demo.notifications.tick();self.demo.notifications.tick()
        self.assertEqual(len(channel.sent),3)
        self.assertIn('微信验证完成',channel.sent[-1][0])
        self.assertEqual(channel.sent[-1][1],[])
        self.assertEqual({n['state'] for n in self.current()['notices']},{'submitted_unverified'})

    def test_changed_wechat_binding_blocks_old_materials(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        self.ready_a(wechat=True)
        channel.binding['peer_id']='another-owner'
        self.demo.notifications.tick()
        self.assertEqual(channel.sent,[])
        self.assertEqual(self.current()['notices'][0]['state'],'blocked')

    def test_local_mode_never_uses_notification_transport(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        self.ready_a();self.demo.notifications.tick()
        self.assertEqual(channel.sent,[])
        self.assertEqual(self.current()['notices'],[])

    def test_unknown_send_is_not_retried(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        self.ready_a(wechat=True)
        with patch.object(channel,'send_package',side_effect=TimeoutError) as sending:
            self.demo.notifications.tick();self.demo.notifications.tick()
        sending.assert_called_once()
        self.assertEqual(self.current()['notices'][0]['state'],'unknown')

    def test_pending_notice_is_cancelled_when_user_already_approved(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        run = self.ready_a(wechat=True)
        self.demo.approve(run['code'],1,'A','A')
        self.demo.notifications.tick()
        self.assertEqual(channel.sent,[])
        self.assertEqual(self.current()['notices'][0]['state'],'cancelled')

    def test_restart_does_not_replay_inflight_wechat_notice(self):
        channel = FakeOwnerWechat();self.demo.notifications.transport=channel
        self.ready_a(wechat=True)
        with self.pilot.connect() as db:
            db.execute("UPDATE two_stage_notices SET state='sending'")
        self.demo = TwoStageDemo(self.pilot,self.root)
        self.demo.notifications.transport=channel
        self.demo.notifications.tick()
        self.assertEqual(channel.sent,[])
        self.assertEqual(self.current()['notices'][0]['state'],'unknown')


if __name__=='__main__':
    unittest.main()
