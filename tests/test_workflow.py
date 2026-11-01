import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zhishi.service import Pilot
from zhishi.config import ROOT
from zhishi.workflows.service import Workflows
from zhishi.workflows.files import digest, encoded
from zhishi.core.clock import timestamp


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = ROOT/'projects'/'excel-demo'
        project = self.root/'projects'/'excel-demo'
        (project/'技能').mkdir(parents=True)
        (project/'待处理').mkdir()
        for file in ['项目文档.md','收件人.json','技能/Excel整合.md']:
            shutil.copyfile(source/file, project/file)
        for file in (source/'待处理').glob('*.xlsx'):
            shutil.copyfile(file, project/'待处理'/file.name)
        (self.root/'workflows').mkdir()
        (self.root/'workflows'/'excel_worker.mjs').write_text('// test executor',encoding='utf-8')
        self.pilot=Pilot(self.root/'test.db')
        self.flow=Workflows(self.pilot,self.root)
        self.pilot.workflow=self.flow

    def tearDown(self):
        self.temp.cleanup()

    def ready(self):
        self.flow.start()
        job=self.flow.state()['jobs'][0]
        folder=self.flow.output/job['code']/('V'+str(job['version']))
        folder.mkdir(parents=True)
        file=folder/'fixture.txt';file.write_text('approved content',encoding='utf-8')
        assets=[{'name':file.name,'relative_path':str(file.relative_to(self.flow.output)),'sha256':digest(file),'bytes':file.stat().st_size}]
        with self.pilot.connect() as db:
            db.execute("UPDATE workflow_runs SET state='awaiting_review',assets_json=? WHERE uid=?",(encoded(assets),job['uid']))
        return job,file

    def test_no_delivery_without_explicit_approval(self):
        job,_=self.ready()
        for text in ['确认','没问题','确认发送']:
            self.assertIn('任务和版本',self.flow.command(text))
        self.flow.tick()
        self.assertEqual(self.flow.row(job['code'],job['version'])['state'],'awaiting_review')
        self.assertFalse((self.flow.project/'模拟收件箱').exists())

    def test_approved_exact_files_delivered_once_and_survive_restart(self):
        job,file=self.ready()
        self.flow.approve(job['code'],job['version'])
        registry=self.flow.project/'收件人.json'
        value=json.loads(registry.read_text('utf-8'))
        value['demo-finance']['folder']='changed-after-approval'
        registry.write_text(encoded(value),encoding='utf-8')
        restarted=Workflows(self.pilot,self.root)
        restarted.tick()
        state=restarted.state()['jobs'][0]
        self.assertEqual(state['state'],'delivered')
        self.assertEqual(len(state['deliveries']),2)
        files=list((self.flow.project/'模拟收件箱').rglob('fixture.txt'))
        self.assertEqual(len(files),2)
        self.assertTrue(all(digest(f)==digest(file) for f in files))
        self.assertFalse((self.flow.project/'模拟收件箱'/'changed-after-approval').exists())
        receipts=list((self.flow.project/'模拟收件箱').rglob('投递回执.json'))
        before=[r.read_bytes() for r in receipts]
        self.assertIn('不会重复',restarted.approve(job['code'],job['version']))
        restarted.tick()
        self.assertEqual(before,[r.read_bytes() for r in receipts])

    def test_tampered_asset_blocks_approval(self):
        job,file=self.ready();file.write_text('changed',encoding='utf-8')
        self.assertIn('没有发送',self.flow.approve(job['code'],job['version']))
        self.assertEqual(self.flow.row(job['code'],job['version'])['state'],'blocked')

    def test_tampered_after_approval_blocks_delivery(self):
        job,file=self.ready();self.flow.approve(job['code'],job['version'])
        file.write_text('changed',encoding='utf-8');self.flow.tick()
        self.assertEqual(self.flow.row(job['code'],job['version'])['state'],'blocked')
        self.assertFalse((self.flow.project/'模拟收件箱').exists())

    def test_old_version_cannot_be_approved(self):
        job,_=self.ready();self.flow.start(force_code=job['code'])
        with self.assertRaisesRegex(ValueError,'旧版本'):
            self.flow.approve(job['code'],job['version'])

    def test_rejected_version_is_not_delivered(self):
        job,_=self.ready()
        self.flow.command(f"要求修改 {job['code']} V{job['version']}")
        self.flow.tick()
        self.assertEqual(self.flow.row(job['code'],job['version'])['state'],'rejected')
        with self.assertRaises(ValueError):self.flow.approve(job['code'],job['version'])

    def test_start_and_chat_request_are_deduplicated(self):
        first=self.pilot.chat('开始今日工作','start-fixture-0001')
        self.assertEqual(first,self.pilot.chat('开始今日工作','start-fixture-0001'))
        self.flow.start()
        self.assertEqual(len(self.flow.state()['jobs']),1)

    def test_asset_auth_and_path_traversal_are_rejected(self):
        job,file=self.ready()
        with self.assertRaises(ValueError):self.flow.asset(job['uid'],file.name,'bad')
        with self.assertRaises(ValueError):self.flow.asset(job['uid'],'../../outside',job['review_token'])
        self.assertEqual(self.flow.asset(job['uid'],file.name,job['review_token']),file)

    def test_unregistered_skill_and_real_recipient_are_rejected(self):
        registry=self.flow.project/'收件人.json'
        value=json.loads(registry.read_text('utf-8'));value['demo-finance']['channel']='wechat'
        registry.write_text(encoded(value),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'模拟投递'):self.flow.start()

    def test_failed_execution_has_notification_and_no_delivery(self):
        self.flow.start()
        with patch.object(self.flow,'execute',side_effect=ValueError('输入编号冲突')):
            self.flow.tick()
        job=self.flow.state()['jobs'][0]
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['notices'][0]['kind'],'failure')
        self.assertEqual(job['notices'][0]['state'],'demo_delivered')
        self.assertFalse((self.flow.project/'模拟收件箱').exists())

    def test_real_sample_package_delivery_in_isolated_test_inboxes(self):
        job,_=self.ready()
        source=ROOT/'tests'/'fixtures'/'excel-result'
        names=['整合结果_示例.xlsx','预览.png','异常清单.csv','完成说明.md','检查结果.json']
        assets=[]
        for name in names:
            file=self.flow.output/job['code']/('V'+str(job['version']))/name
            shutil.copyfile(source/name,file)
            assets.append({'name':name,'relative_path':str(file.relative_to(self.flow.output)),'sha256':digest(file),'bytes':file.stat().st_size})
        with self.pilot.connect() as db:
            db.execute('UPDATE workflow_runs SET assets_json=? WHERE uid=?',(encoded(assets),job['uid']))
        self.flow.approve(job['code'],job['version']);self.flow.tick()
        receipts=list((self.flow.project/'模拟收件箱').rglob('投递回执.json'))
        self.assertEqual(len(receipts),2)
        for receipt in receipts:
            self.assertEqual(json.loads(receipt.read_text('utf-8'))['files'],names)
            self.assertTrue(all(digest(receipt.parent/name)==digest(source/name) for name in names))

    def test_executor_package_is_reviewed_before_delivery(self):
        self.flow.start()
        fixture=ROOT/'tests'/'fixtures'/'excel-result'
        def execute(command, **kwargs):
            spec=json.loads(Path(command[-1]).read_text('utf-8'))
            folder=Path(spec['output']).parent
            for name in ['整合结果_示例.xlsx','预览.png','检查结果.json']:
                shutil.copyfile(fixture/name,folder/name)
            return Mock(returncode=0,stdout='completed',stderr='')
        with patch('zhishi.workflows.excel.subprocess.run',side_effect=execute) as worker:
            self.flow.tick()
        worker.assert_called_once()
        job=self.flow.state()['jobs'][0]
        self.assertEqual(job['state'],'awaiting_review')
        self.assertEqual(job['report']['output_rows'],6)
        self.assertEqual(len(job['assets']),5)
        self.assertFalse((self.flow.project/'模拟收件箱').exists())
        self.flow.approve(job['code'],job['version'])
        self.flow.tick()
        self.assertEqual(self.flow.row(job['code'],job['version'])['state'],'delivered')

    def test_changed_skill_blocks_execution_before_launch(self):
        self.flow.start()
        (self.root/'workflows'/'excel_worker.mjs').write_text('// changed after queue',encoding='utf-8')
        with patch('zhishi.workflows.excel.subprocess.run') as worker:
            self.flow.tick()
        worker.assert_not_called()
        job=self.flow.state()['jobs'][0]
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['assets'],[])


if __name__=='__main__':unittest.main()
