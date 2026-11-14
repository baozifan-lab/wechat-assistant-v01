"""Registered product skills. No tool names, paths or SQL come from the model."""
import csv
from pathlib import Path

from zhishi.workflows.product_demo import digest, only_xlsx, read_rows, read_database_products


class ProductSkills:
    def __init__(self, product_demo):
        self.demo = product_demo

    @staticmethod
    def inputs(task):
        folder = Path(task['input_directory'])
        paths = [only_xlsx(folder / role) for role in ('员工1', '员工2')]
        hashes = [digest(path) for path in paths]
        rows = [list(read_rows(path)) for path in paths]
        if not rows[0] or not any(r['status'] == '作废' for r in rows[1]):
            raise ValueError('加工单或作废记录为空，请补齐输入后重新发起。')
        if hashes != [digest(path) for path in paths]:
            raise ValueError('读取期间源文件发生变化。')
        return {'files': [{'path': str(p.resolve()), 'sha256': h} for p, h in zip(paths, hashes)],
                'employee1_rows': len(rows[0]), 'invalid_rows': sum(r['status'] == '作废' for r in rows[1]),
                'summary': f'已检查员工1输入 {len(rows[0])} 行，员工2清单中有 {sum(r["status"] == "作废" for r in rows[1])} 条作废记录。'}

    @staticmethod
    def check_inputs(report):
        if not report.get('files') or any(digest(Path(f['path'])) != f['sha256'] for f in report['files']):
            raise ValueError('员工1确认的输入已变化，需要重新发起并确认新版本。')

    @classmethod
    def check_selection(cls, task, report):
        folder = Path(task['input_directory'])
        actual = [str(only_xlsx(folder / role).resolve()) for role in ('员工1', '员工2')]
        if actual != [f['path'] for f in report['files']]:
            raise ValueError('当前输入文件与员工1确认的文件不同。')
        cls.check_inputs(report)

    def execute(self, role, run):
        if role == 'employee1':
            return self.inputs(run['task'])
        previous = run['evidence']['employee1']
        self.check_selection(run['task'], previous)
        result = self.demo.process(run['code'], run['task']['source'], folder=run['task']['input_directory'])
        self.check_selection(run['task'], previous)
        if [digest(Path(result[k])) for k in ('processing_file', 'invalid_file')] != [f['sha256'] for f in previous['files']]:
            raise ValueError('比对使用的输入与已确认版本不同。')
        result['summary'] = (f'匹配 {result["matched_codes"]} 个商品编码、{result["matched_rows"]} 条作废记录。'
                             '完成的是清单交集核查，未执行实际作废。')
        if result['source'] == 'database_products':
            result['summary'] += '当前比对表为商品表，不能据此判断加工单是否阻止作废。'
        return result

    def verify(self, run):
        evidence = run['evidence']
        self.check_selection(run['task'], evidence['employee1'])
        report = evidence['employee2']
        result = Path(report['result']).resolve()
        if not result.is_relative_to(self.demo.output.resolve()) or digest(result) != report['result_sha256']:
            raise ValueError('排查成果不在指定目录或已变化。')
        if report['source'] == 'database_products':
            products, checksum = read_database_products()
            if checksum != report['database_snapshot_sha256']:
                raise ValueError('数据库商品快照已变化，请重新发起核查。')
        else:
            products = list(read_rows(Path(report['processing_file'])))
        invalid = [r for r in read_rows(Path(report['invalid_file'])) if r['status'] == '作废']
        lookup = {r['code'] for r in products}
        matches = [r for r in invalid if r['code'] in lookup]
        with result.open(encoding='utf-8-sig', newline='') as stream:
            records = list(csv.DictReader(stream))
        expected_rows = len(invalid) if report['source'] == 'database_products' else len(matches)
        if (len(records) != expected_rows or report['processing_rows'] != len(products)
                or report['invalid_rows'] != len(invalid) or report['matched_rows'] != len(matches)
                or report['matched_codes'] != len({r['code'] for r in matches})
                or sum(r.get('比对结果') == '匹配' for r in records) != len(matches)):
            raise ValueError('最终行数或匹配统计校验未通过。')
        self.check_selection(run['task'], evidence['employee1'])
        return {'passed': True, 'summary': report['summary'], 'result_sha256': report['result_sha256']}

    def validate(self, run, role):
        if role == 'employee1':
            self.check_selection(run['task'], run['evidence']['employee1'])
        else:
            self.verify(run)

    def attachments(self, run):
        report = run['evidence'].get('employee2')
        if not report:
            return []
        path = Path(report['result']).resolve()
        if not path.is_relative_to(self.demo.output.resolve()) or digest(path) != report['result_sha256']:
            raise ValueError('待发送的成果文件校验未通过。')
        return [path]
