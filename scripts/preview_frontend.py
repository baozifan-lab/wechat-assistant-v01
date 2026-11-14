"""Serve the real workbench with disposable example data and no WeChat workers."""
import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhishi.core.clock import now, stamp
from zhishi.runtime import build_pilot, create_server
from zhishi.web.handler import Handler


class PreviewHandler(Handler):
    def output(self, value, status=200, kind="application/json; charset=utf-8", filename=None):
        if kind.startswith('text/html') and isinstance(value, bytes):
            banner = '<div class="preview-banner">界面预览 · 示例数据 · 不连接真实微信</div>'
            value = value.replace(b'<div class="workspace-bar">', banner.encode() + b'<div class="workspace-bar">', 1)
        return super().output(value, status, kind, filename)


def seed(pilot):
    today = now()
    titles = ['整理本周会议纪要', '确认下周项目排期', '核对本月发票信息', '跟进供应商报价', '准备周会汇报材料', '更新商品资料', '归档客户反馈', '检查本周交付文件', '汇总员工任务进度', '回复客户合作方案', '整理上月采购清单', '确认培训安排']
    with pilot.connect() as db:
        for i, title in enumerate(titles):
            created = today - timedelta(days=[0, 0, 1, 1, 2, 3, 3, 4, 5, 5, 6, 6][i], minutes=i * 9)
            state = 'done' if i in (4, 6, 7, 9, 10) else 'cancelled' if i == 11 else 'open'
            db.execute('INSERT INTO tasks(owner,title,due,next_at,created_at,status,completed_at) VALUES (?,?,?,?,?,?,?)',
                       ('pilot-owner', title, stamp(today + timedelta(hours=i+1)), stamp(today + timedelta(hours=i+1)) if state == 'open' else None, stamp(created), state, stamp(today) if state == 'done' else None))
    for i, (title, state) in enumerate([('完成今日商品核查与复核', 'open'), ('确认发票导出文件', 'blocked'), ('汇总本周协作成果', 'done')]):
        result = pilot.work_orders.create({'request_id': f'preview-order-{i:04}', 'title': title, 'template': 'custom', 'assignees': ['existing-primary', 'existing-employee2'], 'due': stamp(today + timedelta(hours=i+2))})
        code = result['id'].removeprefix('order:')
        with pilot.connect() as db:
            db.execute('UPDATE work_orders SET state=?,error=?,created=?,updated=? WHERE code=?', (state, '示例：等待员工核对文件，请在任务详情继续处理。' if state == 'blocked' else '', stamp(today - timedelta(days=i, minutes=15)), stamp(today), code))
            if state == 'done':
                db.execute("UPDATE work_order_staff SET state='done' WHERE code=?", (code,))
    with pilot.connect() as db:
        pilot.message(db, 'assistant', '你好，我是知事。你可以告诉我需要提醒的事项，或从上方选择任务继续协作。当前是独立预览，使用示例数据。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8770)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='zhishi-ui-') as directory:
        root = Path(directory)
        plan = root / 'projects/ai-dispatch/tasks.json'
        plan.parent.mkdir(parents=True)
        plan.write_text(json.dumps({'tasks': []}), encoding='utf-8')
        pilot = build_pilot(root / 'preview.db', root=root, enable_wechat=False)
        seed(pilot)
        server = create_server(pilot, args.port, handler_class=PreviewHandler)
        print(f'Frontend preview: http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
