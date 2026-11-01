"""Execute the registered Excel worker and build a review package."""
import csv
import json
import shutil
import subprocess
from zhishi.config import excel_node
from .files import digest, encoded


class ExcelExecutor:
    def __init__(self, root, output):
        self.root = root
        self.output = output

    def execute(self,run):
        plan=json.loads(run['plan_json']);folder=self.output/run['code']/('V'+str(run['version']))
        snapshot=folder/'输入快照';snapshot.mkdir(parents=True,exist_ok=True)
        if digest(self.root/'workflows'/'excel_worker.mjs')!=plan['processor_sha256']: raise ValueError('执行技能在排队后发生变化，请重新开始。')
        inputs=[]
        for item in plan['inputs']:
            dest=snapshot/item['name'];shutil.copyfile(item['path'],dest)
            if digest(dest)!=item['sha256']: raise ValueError('输入文件在任务开始后变化，请重新开始。')
            inputs.append({'name':item['name'],'path':str(dest)})
        (snapshot/'项目文档.md').write_text(plan['document_text'],encoding='utf-8')
        (snapshot/'技能说明.md').write_text(plan['skill_text'],encoding='utf-8')
        spec={'inputs':inputs,'output':str(folder/'整合结果_示例.xlsx')}
        spec_file=folder/'执行规格.json';spec_file.write_text(encoded(spec),encoding='utf-8')
        result=subprocess.run([str(excel_node()),str(self.root/'workflows'/'excel_worker.mjs'),'--job',str(spec_file)],
            cwd=self.root,capture_output=True,encoding='utf-8',errors='replace',timeout=180,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'运行记录.txt').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode: raise ValueError('表格处理失败，运行记录已保留。请检查输入表头、公式或冲突编号。')
        report=json.loads((folder/'检查结果.json').read_text('utf-8'))
        if not report.get('reconciliation_passed') or report['input_rows']!=report['output_rows']+report['duplicate_rows']+report['invalid_rows']:
            raise ValueError('行数去向检查未通过。')
        with (folder/'异常清单.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['类型','来源文件','来源行','业务编号','原因'])
            for issue in report['issues']:
                values=[issue['kind'],issue['source'],issue['sourceRow'],issue['id'],issue['reason']]
                writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
        names='、'.join(r['name'] for r in plan['recipients_snapshot'])
        summary=f"# {plan['title']}\n\n任务：{run['code']} V{run['version']}\n\n全部为示例数据。输入 {len(inputs)} 份、{report['input_rows']} 行；保留 {report['output_rows']} 行、去重 {report['duplicate_rows']} 行、异常隔离 {report['invalid_rows']} 行。金额合计 {report['total_amount']:,.2f}。\n\n异常行不会进入整合结果，详见异常清单。\n\n确认后的收件人：{names}。渠道：本地模拟收件箱。\n\n确认口令：确认发送 {run['code']} V{run['version']}\n"
        (folder/'完成说明.md').write_text(summary,encoding='utf-8')
        assets=[]
        for name in ['整合结果_示例.xlsx','预览.png','异常清单.csv','完成说明.md','检查结果.json']:
            file=folder/name;assets.append({'name':name,'relative_path':str(file.relative_to(self.output)),'sha256':digest(file),'bytes':file.stat().st_size})

        return report, assets
