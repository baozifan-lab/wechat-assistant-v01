"""Read and validate the registered skill, inputs and recipient snapshot."""
import json
import re
import zipfile
from datetime import datetime
from zhishi.core.clock import CN
from .files import digest, scoped


class DocumentPlan:
    def __init__(self, root, project):
        self.root = root
        self.project = project

    def plan(self):
        document=self.project/'项目文档.md'
        text=document.read_text('utf-8')
        match=re.search(r'```workflow\s*\n(.*?)\n```',text,re.S)
        if not match: raise ValueError('项目文档缺少可执行任务定义。')
        plan=json.loads(match[1])
        if plan.get('skill')!='excel.merge.v1' or plan.get('demo') is not True:
            raise ValueError('首版只执行已注册的 Excel 示例技能。')
        expected={'required_fields':['业务编号','日期','部门','客户','金额'],'duplicate_policy':'identical_only','invalid_policy':'quarantine','conflict_policy':'block'}
        if plan.get('acceptance')!=expected: raise ValueError('验收规则与技能版本不一致，请先更新技能。')
        pattern=plan.get('input_pattern')
        if pattern!='*.xlsx': raise ValueError('首版只接受项目目录中的 .xlsx 明细。')
        folder=scoped(self.project,plan['input_directory'])
        files=sorted(p for p in folder.glob(pattern) if not p.name.startswith('~$'))
        if not files or len(files)>20: raise ValueError('输入文件数量须为 1–20 份。')
        inputs=[]
        for file in files:
            resolved=file.resolve()
            if not resolved.is_relative_to(self.project.resolve()) or file.stat().st_size>10*1024*1024:
                raise ValueError('输入文件超出项目范围或超过 10 MB。')
            with zipfile.ZipFile(file) as archive:
                if sum(i.file_size for i in archive.infolist())>100*1024*1024: raise ValueError('输入工作簿解压后过大。')
                workbook=archive.read('xl/workbook.xml')
                if re.search(rb'date1904\s*=\s*[\"\'](?:1|true)[\"\']',workbook): raise ValueError('首版不处理 1904 日期系统。')
            inputs.append({'name':file.name,'path':str(resolved),'sha256':digest(file)})
        registry=json.loads((self.project/'收件人.json').read_text('utf-8'))
        recipients=[]
        for ident in plan.get('recipients',[]):
            recipient=registry[ident]
            if recipient.get('channel')!='local_demo': raise ValueError('当前跑流程仅允许模拟投递。')
            scoped(self.project/'模拟收件箱',recipient['folder'])
            recipients.append({'id':ident,**recipient})
        if not recipients or len({r['id'] for r in recipients})!=len(recipients): raise ValueError('收件人为空或重复。')
        skill=self.project/'技能'/'Excel整合.md'
        date=datetime.now(CN).date().isoformat()
        if plan.get('work_date') not in ('today',date): raise ValueError('这项工作不属于今天。')
        plan.update({'inputs':inputs,'recipients_snapshot':recipients,'document_sha256':digest(document),
                     'document_text':text,'skill_sha256':digest(skill),'skill_text':skill.read_text('utf-8'),
                     'processor_sha256':digest(self.root/'workflows'/'excel_worker.mjs'),'work_date_resolved':date})
        return plan
