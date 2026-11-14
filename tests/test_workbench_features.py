"""End-to-end contracts for staff scheduling, ordered chat and file corrections."""
import base64
import hashlib
import io
import json
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import openpyxl

from tests import test_workbench as base
from tests.test_invoice_demo import Channel
from zhishi.core.clock import now, stamp
from zhishi.runtime import build_pilot


def workbook(value):
    stream = io.BytesIO()
    book = openpyxl.Workbook()
    book.active.append(['商品名称'])
    book.active.append([value])
    book.save(stream)
    book.close()
    return stream.getvalue()


class WorkbenchFeatureTests(unittest.TestCase):
    setUp = base.WorkbenchTests.setUp
    tearDown = base.WorkbenchTests.tearDown
    request = base.WorkbenchTests.request

    def test_ordered_chat_interleaves_even_with_identical_timestamps_and_survives_restart(self):
        flow = self.pilot.ai_dispatch
        code = flow.start('test-product', 'timeline-start-01')
        flow.notify_tick()
        flow.submit(code, 'manager', '开始吧', 'timeline-reply-01')
        flow.work_tick()
        flow.notify_tick()
        flow.submit(code, 'employee1', '收到', 'timeline-reply-02')
        flow.work_tick()
        flow.notify_tick()
        item = next(t for t in self.request('/api/console')['tasks'] if t['id'] == 'ai:' + code)
        self.assertEqual([m['role'] for m in item['messages']], ['assistant','user','assistant','user','assistant'])
        self.assertEqual(len(set(m['sequence'] for m in item['messages'])), 5)
        reloaded = build_pilot(self.root / 'test.db', root=self.root, enable_wechat=False)
        task = next(t for t in reloaded.workbench.state()['tasks'] if t['id'] == item['id'])
        self.assertEqual(task['messages'], item['messages'])

    def make_order(self, template='custom', request='staff-create-0001'):
        return self.request('/api/console/task/create', {
            'template':template, 'title':'核对本周文件', 'due':stamp(now()+timedelta(minutes=1)),
            'assignees':['existing-primary','existing-employee2'],
            'roles':{'manager':'existing-primary','employee1':'existing-primary','employee2':'existing-employee2'},
            'request_id':request})

    def test_multi_employee_task_dispatch_completion_and_deduplication(self):
        primary, second = Channel('p'), Channel('s')
        self.pilot.transport, self.pilot.employee2_transport = primary, second
        # The workbench also reads status, independently of notification delivery.
        primary.status = second.status = lambda: {'enabled':True,'bound':True}
        first = self.make_order()
        self.assertEqual(first['id'], self.make_order()['id'])
        with self.pilot.connect() as db:
            db.execute('UPDATE work_orders SET due=?', (stamp(now()-timedelta(seconds=1)),))
        self.pilot.work_orders.tick()
        self.assertEqual(len(primary.sent),1)
        self.assertEqual(len(second.sent),1)
        code = first['id'][6:]
        with self.assertRaises(ValueError):
            self.pilot.work_orders.command(code + ' 完成', 'unknown-channel')
        self.pilot.work_orders.command(code + ' 完成', 'primary')
        self.assertEqual(self.pilot.work_orders.items()[0]['state'],'open')
        self.pilot.work_orders.command(code + ' 完成', 'employee2')
        self.assertEqual(self.pilot.work_orders.items()[0]['state'],'done')
        self.assertEqual([m['role'] for m in self.pilot.work_orders.items()[0]['messages']], ['assistant','assistant','user','user'])

    def test_existing_template_uses_selected_employee_channels(self):
        primary, second = Channel('p'), Channel('s')
        self.pilot.transport, self.pilot.employee2_transport = primary, second
        primary.status = second.status = lambda: {'enabled':True,'bound':True}
        order = self.make_order('test-product')
        with self.pilot.connect() as db:
            db.execute('UPDATE work_orders SET due=?', (stamp(now()-timedelta(seconds=1)),))
        self.pilot.work_orders.tick()
        self.pilot.work_orders.tick()
        item = self.pilot.work_orders.items()[0]
        self.assertEqual(item['state'],'launched')
        self.assertEqual(len(self.pilot.ai_dispatch.state()['runs']),1)
        run = self.pilot.ai_dispatch.row(item['linked_id'][3:])
        self.assertEqual(run['task']['roles'], {'manager':'primary','employee1':'primary','employee2':'employee2'})

    def test_new_independent_employee_receives_and_replies_to_assigned_ai_task(self):
        primary, second, extra = Channel('p'), Channel('s'), Channel('new-person')
        self.pilot.transport, self.pilot.employee2_transport = primary, second
        primary.status = second.status = lambda: {'enabled':True,'bound':True}
        person = self.pilot.accounts.save({'name':'新员工','request_id':'new-person-create'})['id']
        original = self.pilot.accounts.transport
        self.pilot.accounts.transport = lambda row, **kwargs: extra if row['id'] == person else original(row, **kwargs)
        extra.secret, extra.authorize, extra.request_id = 'test-secret', lambda data: True, lambda data: data['request_id']
        code = self.pilot.ai_dispatch.start('test-product','new-person-ai-start',
            roles={'manager':'primary','employee1':person,'employee2':'employee2'},
            staff_names={'employee1':'新员工'})
        self.pilot.ai_dispatch.notify_tick()
        self.pilot.ai_dispatch.command(code + ' 开始吧','primary','new-person-manager-reply')
        self.pilot.ai_dispatch.work_tick()
        self.pilot.ai_dispatch.notify_tick()
        self.assertEqual(len(extra.sent),1)
        result = self.pilot.accounts.inbound({'text':code + ' 收到','request_id':'new-person-staff-reply'},'test-secret')
        self.assertIn('收到',result['reply'])
        self.pilot.ai_dispatch.work_tick()
        self.assertTrue(self.pilot.ai_dispatch.row(code)['evidence']['employee1_acknowledged'])
        task = next(t for t in self.request('/api/console')['tasks'] if t['id']=='ai:'+code)
        self.assertIn('新员工',task['channel_label'])

    def invoice(self):
        self.pilot.transport, self.pilot.employee2_transport = Channel('p'), Channel('s')
        self.pilot.transport.status = self.pilot.employee2_transport.status = lambda: {'enabled':True,'bound':True}
        code = 'FP-20261107-1234ABCD'
        file = self.root / 'outputs' / 'invoice-demo' / code / 'original.xlsx'
        file.parent.mkdir(parents=True)
        file.write_bytes(workbook('旧文件'))
        detail = {'file':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                  'primary':self.pilot.transport.route(),'employee2':self.pilot.employee2_transport.route(),
                  'start':'2026-11-01','end':'2026-11-07','display_name':'发票1'}
        with self.pilot.connect() as db:
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)',(code,'employee2_received',json.dumps(detail),stamp()))
            self.pilot.invoice_demo.enqueue(db,code,'employee2_review')
        return code, file

    def upload(self, task, content=None, request='file-correction-001'):
        return self.request('/api/console/file/replace', {
            'id':task['id'], 'state':task['state'], 'updated_at':task['updated_at'], 'file_revision':task['file_revision'],
            'slot':'result','name':'新版.xlsx','content':base64.b64encode(content or workbook('新文件')).decode(),
            'reason':'原文件日期不正确，上传更正版本','request_id':request})

    def test_replacement_download_reconfirmation_and_old_version_guard(self):
        code, old_file = self.invoice()
        task = next(t for t in self.request('/api/console')['tasks'] if t['kind']=='invoice')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.upload(task,b'invalid workbook')
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        self.assertIn('新版',self.upload(task)['reply'])
        self.upload(task)  # Same request is a no-op even after the state changes.
        self.assertEqual(old_file.read_bytes(),workbook('旧文件'))
        revised = next(t for t in self.request('/api/console')['tasks'] if t['id']==task['id'])
        self.assertEqual(revised['state'],'employee1_wait')
        self.assertEqual(revised['file_revision'],2)
        self.assertEqual(len(revised['file_versions']),1)
        self.assertIn('文件已更新',self.pilot.invoice_demo.command('发票1 核对无误','primary'))
        self.pilot.invoice_demo.tick()
        self.assertIn('V2', self.pilot.transport.sent[-1][0])
        self.assertIn('已记录核对',self.pilot.invoice_demo.command('发票1 V2 新文件核对无误','primary'))
        self.pilot.invoice_demo.tick()
        self.assertIn('文件已更新', self.pilot.invoice_demo.command('发票1 收到','employee2'))
        self.assertIn('已记录收到', self.pilot.invoice_demo.command('发票1 V2 收到','employee2'))
        self.upload(next(t for t in self.request('/api/console')['tasks'] if t['id']==task['id']), workbook('第三版'), 'file-correction-002')
        self.assertIn('文件已更新', self.pilot.invoice_demo.command('发票1 V2 新文件核对无误','primary'))

    def test_upload_rejects_stale_version_and_busy_worker(self):
        code, _ = self.invoice()
        task = next(t for t in self.request('/api/console')['tasks'] if t['kind']=='invoice')
        self.pilot.invoice_demo.lock.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.upload(task)
            self.assertEqual(error.exception.code,400)
            error.exception.close()
        finally:
            self.pilot.invoice_demo.lock.release()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.upload(dict(task,file_revision=99))
        self.assertEqual(error.exception.code,400)
        error.exception.close()

    def test_ai_input_replacement_isolated_and_invalidates_old_reply(self):
        flow = self.pilot.ai_dispatch
        code = flow.start('test-product','input-replace-start')
        source = self.root/'inputs'/'员工2'
        source.mkdir(parents=True)
        original = workbook('保留的第二份输入')
        (source/'原始.xlsx').write_bytes(original)
        data = {'id':'ai:'+code,'revision':0,'slot':'employee1','name':'新输入.xlsx',
            'content':base64.b64encode(workbook('新输入')).decode(),'reason':'重新导出本周数据','request_id':'input-replace-001'}
        self.request('/api/console/file/replace',data)
        run = flow.row(code)
        self.assertEqual(run['state'],'employee1_work')
        self.assertEqual(run['evidence'],{})
        self.assertNotEqual(run['task']['input_directory'],str(self.root/'inputs'))
        self.assertEqual((Path(run['task']['input_directory'])/'员工2'/'原始.xlsx').read_bytes(),original)
        self.assertEqual((source/'原始.xlsx').read_bytes(),original)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/console/file/replace',dict(data,request_id='input-replace-stale'))
        self.assertEqual(error.exception.code,400)
        error.exception.close()

    def test_blocked_task_can_replace_employee2_input_and_restart_review(self):
        flow = self.pilot.ai_dispatch
        code = flow.start('test-product', 'blocked-upload-start')
        source = self.root / 'inputs'
        original = workbook('员工2原始')
        for role in ('员工1', '员工2'):
            (source / role).mkdir(parents=True, exist_ok=True)
            (source / role / '原始.xlsx').write_bytes(original if role == '员工2' else workbook(role + '原始'))
        with self.pilot.connect() as db:
            flow._block(db, flow.row(code), '员工2清单有误', reporting_role='employee1')
        before = flow.row(code)
        replacement = workbook('员工2修正版')
        self.request('/api/console/file/replace', {
            'id': 'ai:' + code, 'revision': before['revision'], 'slot': 'employee2',
            'name': '修正版.xlsx', 'content': base64.b64encode(replacement).decode(),
            'reason': '排除不属于作废商品的记录', 'request_id': 'blocked-upload-001'})
        after = flow.row(code)
        self.assertEqual(after['state'], 'employee1_work')
        self.assertEqual(after['evidence'], {})
        self.assertEqual((Path(after['task']['input_directory']) / '员工2' / '输入.xlsx').read_bytes(), replacement)
        self.assertEqual((source / '员工2' / '原始.xlsx').read_bytes(), original)
        self.assertEqual(len(self.pilot.corrections.history('ai:' + code)), 1)


if __name__ == '__main__':
    unittest.main()
