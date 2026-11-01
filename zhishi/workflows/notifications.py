"""Compose owner review, completion and failure notices independently of transport."""
import json
from .files import scoped


class WorkflowNotifications:
    def __init__(self, artifacts, transport, output):
        self.artifacts = artifacts
        self.transport = transport
        self.output = output

    def notice(self,row):
        run=self.artifacts.row(row['code'],row['version']);plan=json.loads(run['plan_json'])
        if row['kind']=='review':
            report=json.loads(run['report_json']);files=self.artifacts.assets(run)
            text=(f"示例工作完成：{run['code']} V{run['version']}\n输入 {len(plan['inputs'])} 份、{report['input_rows']} 行，保留 {report['output_rows']} 行。\n去重 {report['duplicate_rows']} 行，异常隔离 {report['invalid_rows']} 行；金额合计 {report['total_amount']:,.2f}。\n"
                  f"请检查附件和预览。确认后仅投递给：{'、'.join(r['name'] for r in plan['recipients_snapshot'])}的本地模拟收件箱。\n回复：确认发送 {run['code']} V{run['version']}\n需要修改可回复：要求修改 {run['code']} V{run['version']}")
            media=[scoped(self.output,a['relative_path']) for a in files if a['name'] in ['整合结果_示例.xlsx','预览.png','异常清单.csv']]
        elif row['kind']=='failure':
            text=f"{run['code']} V{run['version']} 未完成：{run['error']}\n没有自动通过验收；请在工作台检查，修正后重新制作。"
            media=[]
        else:
            text=f"{run['code']} V{run['version']} 模拟投递完成。两名模拟收件人的收件箱已保存你确认的文件版本。未联系真实人员。"
            media=[]
        transport=self.transport
        if transport and transport.enabled:
            return transport.send_package(text,media) if media else transport.send(text)
        return 'demo_delivered','验收材料可在本地工作台查看。'
