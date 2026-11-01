import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from zhishi.config import OWNER
from zhishi.service import Pilot
from zhishi.core.clock import now, stamp


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pilot.db"
        self.app = Pilot(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def make_due(self, ident=1):
        with self.app.connect() as db:
            db.execute("UPDATE tasks SET next_at=? WHERE id=?", (stamp(now() - timedelta(seconds=1)), ident))

    def test_reminder_survives_restart_and_does_not_repeat(self):
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        self.make_due()
        restarted = Pilot(self.path)
        restarted.tick()
        restarted.tick()
        state = restarted.state()
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(state["outbox"][0]["state"], "demo_delivered")
        self.assertEqual(len([m for m in state["messages"] if m["role"] == "reminder"]), 1)

    def test_done_prevents_reminder(self):
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        self.make_due()
        self.app.chat("完成 1", "request-0002")
        self.app.tick()
        self.assertEqual(self.app.state()["tasks"][0]["status"], "done")
        self.assertEqual(self.app.state()["outbox"], [])

    def test_snooze_preserves_deadline_and_allows_new_reminder(self):
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        original = self.app.state()["tasks"][0]["due"]
        self.make_due()
        self.app.tick()
        self.app.chat("10 分钟后提醒 1", "request-0002")
        self.assertEqual(self.app.state()["tasks"][0]["due"], original)
        self.make_due()
        self.app.tick()
        self.assertEqual(len(self.app.state()["outbox"]), 2)

    def test_request_deduplication(self):
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        self.assertEqual(len(self.app.state()["tasks"]), 1)

    def test_cannot_modify_another_owner(self):
        with self.app.connect() as db:
            db.execute("INSERT INTO tasks(owner,title,due,next_at,created_at) VALUES (?,?,?,?,?)",
                       ("another-user", "private", stamp(now()+timedelta(hours=1)), None, stamp()))
        with self.assertRaises(ValueError):
            self.app.chat("完成 1", "request-0001")
        self.assertEqual(self.app.state()["tasks"], [])

    def test_past_date_does_not_create_task(self):
        with self.assertRaises(ValueError):
            self.app.chat("提醒我 2000-01-01 09:00 提交方案", "request-0001")
        self.assertEqual(self.app.state()["tasks"], [])

    def test_unknown_time_is_clarified(self):
        self.app.chat("有空提醒我提交方案", "request-0001")
        self.assertEqual(self.app.state()["tasks"], [])

    def test_model_output_must_be_valid_and_cannot_invent_task_id(self):
        self.app.chat("1 分钟后提醒我提交方案", "request-0001")
        with patch.object(self.app, "interpret", return_value={"action":"done", "id":1}):
            result = self.app.chat("全部完成", "request-0002")
        self.assertIn("编号", result["reply"])
        self.assertEqual(self.app.state()["tasks"][0]["status"], "open")

    def test_model_request_uses_configured_alias(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({"choices":[{"message":{"content":json.dumps({"action":"list"})}}]}).encode()

        with patch.dict("os.environ", {"MODEL_BASE_URL":"http://127.0.0.1:4000/v1", "MODEL_API_KEY":"test-secret"}):
            self.app.configure({"mode":"model", "model":"planner-strong"})
            with patch("urllib.request.urlopen", return_value=Response()) as call:
                self.app.chat("帮我看看现在有哪些事情", "request-0001")
            req = call.call_args.args[0]
            self.assertEqual(json.loads(req.data)["model"], "planner-strong")
            self.assertNotIn("test-secret", json.dumps(self.app.state()))


if __name__ == "__main__":
    unittest.main()
