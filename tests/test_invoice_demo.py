import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl
from zhishi.core.database import Database
from zhishi.workflows.invoice_demo import InvoiceDemo, extraction_details, extraction_comparison


class Channel:
    enabled = True
    def __init__(self, name):
        self.name = name
        self.sent = []
    def route(self):
        return {'account_id': self.name, 'peer_id': self.name}
    def gateway_listening(self):
        return True
    def send_package(self, text, media, *, expected_route):
        assert expected_route == self.route()
        self.sent.append((text, [str(p) for p in media]))
        return 'submitted_unverified', 'test'


class InvoiceDemoTest(unittest.TestCase):
    def test_full_handoff_and_file_integrity(self):
        with tempfile.TemporaryDirectory() as folder, patch('pathlib.Path.is_file', return_value=True):
            primary, employee2 = Channel('primary'), Channel('employee2')
            pilot = SimpleNamespace(store=Database(Path(folder) / 'test.sqlite3'), transport=primary, employee2_transport=employee2)
            flow = InvoiceDemo(pilot, Path(folder))
            flow.start()
            with flow.store.connect() as db:
                run = db.execute('SELECT * FROM invoice_demo_runs').fetchone()
            code = run['code']
            flow.tick()
            self.assertIn('发票确认启动', primary.sent[-1][0])
            flow.command('发票确认启动', 'primary')
            flow.tick()
            with patch('zhishi.workflows.invoice_demo.read_invoice_samples', return_value=(['商品名称'], [[f'商品{i}'] for i in range(14)])):
                flow.tick()
            self.assertIn('取数来源：PostgreSQL', primary.sent[-1][0])
            self.assertIn('不按真实业务日期筛选', primary.sent[-1][0])
            self.assertIn('无法比较', primary.sent[-1][0])
            path = primary.sent[-1][1][0]
            book = openpyxl.load_workbook(path, read_only=True)
            rows = list(book.active.values)
            book.close()
            self.assertEqual(rows[0][0], '模拟日期')
            self.assertEqual(len(set(row[0] for row in rows[1:])), 7)
            self.assertEqual(len(employee2.sent), 0)
            self.assertIn('对应的已绑定微信', flow.command('员工1确认 ' + code, 'employee2'))
            flow.command('发票核对无误', 'primary')
            flow.tick()
            self.assertEqual(employee2.sent[-1][1], [path])
            self.assertIn('不在对应阶段', flow.command('员工2核对完成 ' + code, 'employee2'))
            flow.command('发票收到', 'employee2')
            flow.command('发票核对完成', 'employee2')
            flow.tick()
            self.assertIn('任务已完成', primary.sent[-1][0])

    def test_recorded_extraction_comparison(self):
        old = extraction_details('2026-11-01', '2026-11-07', ['名称'])
        self.assertIn('一致', extraction_comparison(old, old))
        changed = extraction_details('2026-11-02', '2026-11-08', ['名称', '金额'])
        result = extraction_comparison(changed, old)
        self.assertIn('模拟日期范围：2026-11-01', result)
        self.assertIn('字段：名称 → 名称、金额', result)
        self.assertIn('无法比较', extraction_comparison(changed, None))

    def test_named_invoice_can_be_started_and_confirmed_once(self):
        with tempfile.TemporaryDirectory() as folder, patch('pathlib.Path.is_file', return_value=True):
            primary, employee2 = Channel('primary'), Channel('employee2')
            pilot = SimpleNamespace(store=Database(Path(folder) / 'test.sqlite3'), transport=primary, employee2_transport=employee2)
            flow = InvoiceDemo(pilot, Path(folder))
            self.assertIn('发票1', flow.command('开始发票1', 'primary'))
            flow.tick()
            self.assertIn('发票1 确认启动', primary.sent[-1][0])
            self.assertIn('请使用', flow.command('发票1 确认启动', 'employee2'))
            self.assertIn('已确认启动', flow.command('发票1 确认启动', 'primary'))
            with flow.store.connect() as db:
                db.execute("UPDATE invoice_demo_runs SET state='complete'")
            with self.assertRaisesRegex(ValueError, '已使用'):
                flow.command('开始发票1', 'primary')
            self.assertIn('发票2', flow.command('开始发票2', 'primary'))
            with flow.store.connect() as db:
                rows = db.execute('SELECT detail FROM invoice_demo_runs ORDER BY rowid').fetchall()
            from datetime import date, timedelta
            first, second = (json.loads(row['detail']) for row in rows)
            self.assertEqual(date.fromisoformat(second['start']), date.fromisoformat(first['start']) + timedelta(days=7))
            self.assertEqual(date.fromisoformat(second['end']), date.fromisoformat(second['start']) + timedelta(days=6))

    def test_corrected_file_requires_explicit_new_file_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, patch('pathlib.Path.is_file', return_value=True):
            primary, employee2 = Channel('primary'), Channel('employee2')
            flow = InvoiceDemo(SimpleNamespace(store=Database(Path(folder) / 'test.sqlite3'), transport=primary, employee2_transport=employee2), Path(folder))
            flow.command('开始发票1', 'primary')
            flow.tick()
            flow.command('发票1 确认启动', 'primary')
            flow.tick()
            with patch('zhishi.workflows.invoice_demo.read_invoice_samples', return_value=(['商品名称'], [['样本']])):
                flow.tick()
            with flow.store.connect() as db:
                row = db.execute('SELECT code,detail FROM invoice_demo_runs').fetchone()
                data = json.loads(row['detail']); data['review_revision'] = 2
                db.execute('UPDATE invoice_demo_runs SET detail=? WHERE code=?', (json.dumps(data), row['code']))
            self.assertIn('文件已更新', flow.command('发票1 核对无误', 'primary'))
            self.assertIn('已记录核对', flow.command('发票1 新文件核对无误', 'primary'))

