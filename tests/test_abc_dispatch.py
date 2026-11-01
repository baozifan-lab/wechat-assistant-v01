import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from zhishi.config import ROOT
from zhishi.service import Pilot
from zhishi.workflows.abc_dispatch import ABCDispatch
from zhishi.workflows.files import encoded


class OwnerChannel:
    enabled = True

    def __init__(self):
        self.binding = {'account_id':'fixture','peer_id':'owner'}
        self.sent = []
        self.result = 'submitted_unverified'

    def route(self):
        return dict(self.binding)

    def send_package(self, text, media, *, expected_route=None):
        assert expected_route==self.binding
        self.sent.append((text,list(media)))
        return self.result,'fixture transport'


class ABCDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        project = self.root/'projects'/'excel-demo'
        (project/'技能').mkdir(parents=True);(project/'待处理').mkdir()
        for name in ('项目文档.md','收件人.json','技能/Excel整合.md'):
            shutil.copyfile(ROOT/'projects'/'excel-demo'/name,project/name)
        for file in (ROOT/'projects'/'excel-demo'/'待处理').glob('*.xlsx'):
            shutil.copyfile(file,project/'待处理'/file.name)
        (self.root/'workflows').mkdir()
        for name in ('excel_worker.mjs','department_summary.mjs'):
            (self.root/'workflows'/name).write_text('// fixture',encoding='utf-8')
        self.channel = OwnerChannel()
        self.pilot = Pilot(self.root/'test.db',self.channel)
        self.demo = ABCDispatch(self.pilot,self.root);self.pilot.abc = self.demo

    def tearDown(self):
        self.temp.cleanup()

    def current(self):
        return self.demo.state()['runs'][0]

    def due(self):
        self.demo.start(delay=0);self.demo.tick();self.demo.notify_tick()
        self.assertEqual(self.current()['state'],'a_waiting')
        return self.current()

    def worker_a(self, run):
        folder = self.demo.output/run['code']/'V1';folder.mkdir(parents=True)
        names = ['整合结果_示例.xlsx','预览.png','异常清单.csv','完成说明.md','检查结果.json']
        for name in names:
            shutil.copyfile(ROOT/'tests'/'fixtures'/'excel-result'/name,folder/name)
        return json.loads((folder/'检查结果.json').read_text('utf-8')),self.demo.manifest(folder,names)

    @staticmethod
    def worker_b(command, **kwargs):
        spec = json.loads(Path(command[-1]).read_text('utf-8'));folder = Path(spec['output'])
        report = {'rows':6,'total_amount':5000.75,'departments':[
            {'department':'销售一部','rows':3,'total_amount':1850.75},
            {'department':'销售二部','rows':3,'total_amount':3150}]}
        (folder/'部门汇总.json').write_text(encoded(report),encoding='utf-8')
        (folder/'部门汇总.md').write_text('fixture',encoding='utf-8')
        return Mock(returncode=0,stdout='',stderr='')

    def ready_b(self):
        run = self.due();self.demo.complete(run['code'],1,'A')
        with patch.object(self.demo.executor,'execute',side_effect=self.worker_a):
            self.demo.tick();self.demo.tick()
        self.assertEqual(self.current()['state'],'b_waiting');self.demo.notify_tick()
        return self.current()

    def finished(self):
        run = self.ready_b();self.demo.complete(run['code'],1,'B')
        with patch('zhishi.workflows.abc_dispatch.subprocess.run',side_effect=self.worker_b) as worker:
            self.demo.tick();self.demo.tick()
        worker.assert_called_once()
        return self.current()

    def test_schedule_waits_and_only_assigns_a_once_when_due(self):
        self.demo.start(delay=30)
        self.demo.tick();self.demo.notify_tick()
        self.assertEqual(self.current()['state'],'scheduled');self.assertEqual(self.channel.sent,[])
        with self.pilot.connect() as db:
            db.execute("UPDATE abc_runs SET scheduled_at='2000-01-01T00:00:00+08:00'")
        for _ in range(2):self.demo.tick();self.demo.notify_tick()
        self.assertEqual(self.current()['state'],'a_waiting')
        self.assertEqual(len(self.channel.sent),1)
        self.assertIn('派给 A',self.channel.sent[0][0])
        self.assertEqual([e['kind'] for e in self.current()['events']],['scheduled','A_assigned'])

    def test_b_and_c_wait_for_actual_confirmations(self):
        run = self.due()
        with self.assertRaises(ValueError):self.demo.complete(run['code'],1,'B')
        with patch.object(self.demo,'prepare_a') as a,patch.object(self.demo,'prepare_b') as b:
            self.demo.tick();self.demo.notify_tick()
        a.assert_not_called();b.assert_not_called()
        self.assertEqual(len(self.channel.sent),1)

    def test_full_dispatch_order_and_c_receives_final_files_without_approval(self):
        run = self.finished()
        self.assertEqual(run['state'],'completed');self.assertEqual(len(self.channel.sent),2)
        self.demo.notify_tick();self.demo.notify_tick()
        self.assertEqual(len(self.channel.sent),3)
        self.assertIn('领导 C｜最终完成汇报',self.channel.sent[2][0])
        self.assertEqual({p.name for p in self.channel.sent[2][1]},{'整合结果_示例.xlsx','部门汇总.csv'})
        self.assertEqual([e['kind'] for e in self.current()['events']],['scheduled','A_assigned','A_confirmed','A_completed','B_assigned','B_confirmed','B_completed','C_report_submitted'])
        with self.assertRaises(ValueError):self.demo.complete(run['code'],1,'C')

    def test_duplicate_user_commands_do_not_repeat_handoffs(self):
        run = self.due();text=f"完成A {run['code']} V1"
        result = self.pilot.chat(text,'abc-fixture-user-001')
        self.assertEqual(result,self.pilot.chat(text,'abc-fixture-user-001'))
        self.assertIn('不重复',self.demo.complete(run['code'],1,'A'))
        self.assertEqual(sum(e['kind']=='A_confirmed' for e in self.current()['events']),1)

    def test_wrong_version_and_premature_confirmation_rejected(self):
        self.demo.start();r=self.current()
        for version,role in ((1,'A'),(1,'B'),(2,'A')):
            with self.assertRaises(ValueError):self.demo.complete(r['code'],version,role)

    def test_changed_a_file_blocks_b_confirmation_and_no_success_report(self):
        r=self.ready_b();(self.demo.output/r['a_assets'][0]['relative_path']).write_bytes(b'changed')
        self.assertIn('已停止',self.demo.complete(r['code'],1,'B'))
        self.assertEqual(self.current()['state'],'blocked')
        self.assertFalse(any(n['kind']=='C_report' for n in self.current()['notices']))

    def test_changed_b_file_blocks_c_report(self):
        r=self.finished();(self.demo.output/r['b_assets'][0]['relative_path']).write_bytes(b'changed')
        self.demo.notify_tick()
        self.assertEqual(len(self.channel.sent),2)
        self.assertEqual(self.current()['notices'][-1]['state'],'blocked')

    def test_changed_binding_blocks_dispatch_and_confirmation(self):
        self.demo.start(delay=0);self.demo.tick();self.channel.binding['peer_id']='other'
        self.demo.notify_tick();r=self.current()
        self.assertEqual(self.channel.sent,[])
        with self.assertRaises(ValueError):self.demo.complete(r['code'],1,'A')

    def test_worker_failure_does_not_assign_b(self):
        r=self.due();self.demo.complete(r['code'],1,'A')
        with patch.object(self.demo.executor,'execute',side_effect=ValueError('fixture failure')):
            self.demo.tick()
        self.assertEqual(self.current()['state'],'failed')
        self.assertFalse(any(e['kind']=='B_assigned' for e in self.current()['events']))

    def test_unknown_notification_is_not_retried_after_restart(self):
        self.channel.result='unknown';self.due()
        restarted=ABCDispatch(self.pilot,self.root);restarted.notify_tick()
        self.assertEqual(len(self.channel.sent),1)
        self.assertEqual(restarted.state()['runs'][0]['notices'][0]['state'],'unknown')

    def test_restart_preserves_confirmation_and_processes_it_once(self):
        r=self.due();self.demo.complete(r['code'],1,'A')
        self.demo=ABCDispatch(self.pilot,self.root)
        with patch.object(self.demo.executor,'execute',side_effect=self.worker_a) as worker:
            self.demo.tick();self.demo.tick()
        worker.assert_called_once()
        self.assertEqual(self.current()['state'],'b_waiting')

    def test_assets_require_valid_role_token_and_manifest_name(self):
        r=self.ready_b();a=r['a_assets'][0]
        self.assertTrue(self.demo.asset(r['uid'],'A',a['name'],r['access_token']).is_file())
        for role,name,token in [('A',a['name'],'wrong'),('C',a['name'],r['access_token']),('A','../private',r['access_token'])]:
            with self.assertRaises(ValueError):self.demo.asset(r['uid'],role,name,token)

    def test_unbound_start_is_rejected(self):
        self.channel.enabled=False
        with self.assertRaises(ValueError):self.demo.start()
        self.assertEqual(self.demo.state()['runs'],[])
