"""Persistent two-stage notices; both simulated roles target one pinned owner route."""
import json
import threading

from zhishi.core.clock import timestamp
from .files import scoped


class TwoStageNotifications:
    def __init__(self, demo, transport):
        self.demo = demo
        self.store = demo.store
        self.transport = transport
        self.lock = threading.Lock()
        with self.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS two_stage_notices (
                id INTEGER PRIMARY KEY, run_uid TEXT NOT NULL, kind TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, UNIQUE(run_uid,kind))''')
            db.execute("UPDATE two_stage_notices SET state='unknown',detail='重启前发送结果未确认，不自动重发。' WHERE state='sending'")

    @staticmethod
    def enqueue(db, run, kind):
        if run['notice_enabled']:
            db.execute('INSERT OR IGNORE INTO two_stage_notices(run_uid,kind,created_at) VALUES (?,?,?)',
                       (run['uid'],kind,timestamp()))

    def package(self, run, kind):
        code = f"{run['code']} V{run['version']}"
        if kind=='a_review':
            assets = self.demo.verified_assets(run,'A')
            r = json.loads(run['a_report_json'])
            text = (f"【角色 A｜第一段待验收】\n{code}\nA、B 都是你本人，本次使用示例数据和固定技能。\n"
                    f"A 已整合：输入 {r['input_rows']} 行，保留 {r['output_rows']} 行，去重 {r['duplicate_rows']} 行，隔离 {r['invalid_rows']} 行，合计 {r['total_amount']:,.2f}。\n"
                    f"请核对 Excel、预览和异常清单。B 尚未执行。\n回复：确认第一段 {code}\n确认后自动交接，B 的结果仍发到这个微信。")
            media = [scoped(self.demo.output,a['relative_path']) for a in assets if a['name'] in ('整合结果_示例.xlsx','预览.png','异常清单.csv')]
        elif kind=='b_review':
            self.demo.approval_digest(run,'B')
            assets = self.demo.verified_assets(run,'B')
            source = self.demo.verified_assets(run,'A')
            r = json.loads(run['b_report_json'])
            groups = '\n'.join(f"{g['department']}：{g['rows']} 行，{g['total_amount']:,.2f}" for g in r['departments'])
            text = (f"【角色 B｜第二段待验收】\n{code}\nA 已由你确认，B 已自动读取 A 的 V{r['upstream_version']} 成果并完成部门汇总。\n"
                    f"{groups}\n共 {r['rows']} 行，合计 {r['total_amount']:,.2f}。\n"
                    f"附件含 A 的交接 Excel 和 B 的部门汇总。\n回复：确认第二段 {code}\n这是 B 的独立验收，确认后本轮结束。")
            media = [scoped(self.demo.output,a['relative_path']) for a in assets if a['name']=='部门汇总.csv']
            media += [scoped(self.demo.output,a['relative_path']) for a in source if a['name']=='整合结果_示例.xlsx']
        elif kind=='completed':
            text = f"【两段式微信验证完成】\n{code}\n你已分别以 A、B 身份确认。A→B 交接、自动汇总和两段验收均完成。\n两个角色均使用本人绑定会话，未向其他联系人分发。\n后续可发送：开始微信两段式验证"
            media = []
        else:
            text = f"【两段式验证需要处理】\n{code}\n{run['error']}\n本轮没有自动通过验收，请到协作验证页面检查。"
            media = []
        return text, media

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            with self.store.connect() as db:
                row = db.execute('''SELECT n.*,r.code FROM two_stage_notices n
                    JOIN two_stage_runs r ON r.uid=n.run_uid
                    WHERE n.state='pending' AND r.notice_enabled=1 AND
                    ((n.kind='a_review' AND r.state='a_review') OR
                     (n.kind='b_review' AND r.state='b_review') OR
                     (n.kind='completed' AND r.state='completed') OR
                     (n.kind='failure' AND r.state IN ('failed','blocked')))
                    ORDER BY n.id LIMIT 1''').fetchone()
                if not row:
                    return
                if db.execute("UPDATE two_stage_notices SET state='sending' WHERE id=? AND state='pending'",(row['id'],)).rowcount!=1:
                    return
            try:
                run = self.demo.row(row['code'])
                route = json.loads(run['notice_route_json'])
                expected_state = {'a_review':'a_review','b_review':'b_review','completed':'completed'}.get(row['kind'])
                if expected_state and run['state']!=expected_state:
                    state,detail = 'cancelled','阶段已发生变化，不发送过期通知。'
                elif not self.transport or not self.transport.enabled or self.transport.route()!=route:
                    state,detail = 'blocked','本人绑定不可用或已改变，没有把材料发送到其他会话。'
                else:
                    text,media = self.package(run,row['kind'])
                    state,detail = self.transport.send_package(text,media,expected_route=route)
            except ValueError:
                state,detail = 'blocked','材料校验未通过，未发送；请查看工作台。'
            except Exception:
                state,detail = 'unknown','发送结果未确认，不自动重发；请先核对手机。'
            with self.store.connect() as db:
                db.execute('UPDATE two_stage_notices SET state=?,detail=? WHERE id=?',(state,detail,row['id']))
        finally:
            self.lock.release()
