import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from zhishi.core.clock import CN
from zhishi.service import Pilot
from zhishi.workflows.daily_summary import daily_summary


class DailySummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pilot = Pilot(Path(self.tmp.name) / 'db.sqlite3')
        self.pilot.employee2_transport = SimpleNamespace(route=lambda: {'peer':'employee2'})
        self.now = datetime(2026,11,6,12,0,tzinfo=CN)
        with self.pilot.connect() as db:
            db.execute('CREATE TABLE invoice_demo_runs(code TEXT,state TEXT,detail TEXT,updated TEXT)')
            db.execute('CREATE TABLE invoice_demo_notices(id INTEGER PRIMARY KEY,code TEXT,kind TEXT,state TEXT)')
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)', ('FP-20261106-AAA', 'employee2_wait',
                json.dumps({'display_name':'发票3','employee2':{'peer':'employee2'}}), '2026-11-06T11:00:00+08:00'))
            db.execute('INSERT INTO invoice_demo_notices(code,kind,state) VALUES (?,?,?)', ('FP-20261106-AAA','employee2_review','unknown'))
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)', ('FP-20261105-BBB', 'complete',
                json.dumps({'display_name':'发票2','employee2':{'peer':'another'}}), '2026-11-06T09:00:00+08:00'))
            db.execute('INSERT INTO invoice_demo_runs VALUES (?,?,?,?)', ('FP-20261104-CCC', 'complete',
                json.dumps({'display_name':'发票旧','employee2':{'peer':'employee2'}}), '2026-11-05T09:00:00+08:00'))
            for title,due,status,finished in [('逾期待办','2026-11-05T10:00:00+08:00','open',None),
                    ('今日完成','2026-11-06T10:00:00+08:00','done','2026-11-06T11:00:00+08:00'),
                    ('旧记录','2026-11-06T10:00:00+08:00','done',None)]:
                db.execute('INSERT INTO tasks(owner,title,due,next_at,created_at,status,completed_at) VALUES (?,?,?,?,?,?,?)',
                    ('pilot-owner',title,due,due,'2026-11-05T08:00:00+08:00',status,finished))

    def tearDown(self):
        self.tmp.cleanup()

    @patch('zhishi.workflows.daily_summary.now')
    def test_summary_tracks_open_done_and_unknown_delivery(self, clock):
        clock.return_value = self.now
        text = daily_summary(self.pilot)
        self.assertIn('发票3：等待员工2接收；通知发送状态需核对', text)
        self.assertIn('发票2：已完成', text)
        self.assertNotIn('发票旧', text)
        self.assertIn('逾期待办：已到期，未完成', text)
        self.assertIn('今日完成：已完成', text)
        self.assertIn('完成日期未记录', text)

    @patch('zhishi.workflows.daily_summary.now')
    def test_employee2_sees_only_own_binding(self, clock):
        clock.return_value = self.now
        text = daily_summary(self.pilot, 'employee2')
        self.assertIn('发票3', text)
        self.assertNotIn('发票2', text)
        self.assertNotIn('逾期待办', text)

    @patch('zhishi.workflows.daily_summary.now')
    def test_chat_entry_is_idempotent(self, clock):
        clock.return_value = self.now
        first = self.pilot.chat('清单', 'daily-test-0001')
        self.assertIn('任务清单', first['reply'])
        self.assertEqual(first, self.pilot.chat('清单', 'daily-test-0001'))
