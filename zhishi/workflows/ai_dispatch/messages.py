"""Human-facing task briefs: work, assistance, collaboration and the next decision."""
from pathlib import Path

NAMES = {'manager':'负责人','employee1':'员工1','employee2':'员工2'}


def header(role):
    return f'【商品排查｜{NAMES[role]}】\n'


def result_summary(report, fallback=''):
    if 'matched_codes' in report and 'matched_rows' in report:
        return f'匹配 {report["matched_codes"]} 个商品编码、{report["matched_rows"]} 条作废记录。'
    return report.get('summary',fallback)


def stage_message(state, task, evidence):
    database = task.get('source') == 'database_products'
    source = '数据库商品表' if database else '商品加工单'
    boundary = ('本轮使用的是演示商品表，不能据此判断加工单是否阻止作废。' if database else
                '本轮只核查清单交集，不会执行商品作废或修改原始数据。')
    folder = Path(task.get('input_directory', '项目输入目录'))
    if state == 'manager_wait':
        return ('准备开展一轮商品排查，找出作废清单中同时出现在'+source+'里的商品。\n\n'
                '安排：员工1准备并核对输入；我协助检查文件，交给员工2进行商品编码比对；员工2核对结果后，我再做最终检查并向你汇报。\n'
                '你现在只需决定是否开始。'+boundary+'\n\n'
                '可以直接说“开始吧”“可以”，有需要调整的安排也可以直接告诉我。')
    if state == 'employee1_wait':
        reference = ('本轮加工单文件仅作参考，后续比对使用数据库商品表。' if database else '')
        next_reply = ('你已接单，接下来把文件准备好，导出好后说“好了”“导好了”都可以，需要帮助也可以直接说。'
                      if evidence.get('employee1_acknowledged') else
                      '先回“收到”即可记为接单；导出好后说“好了”“导好了”都可以，需要帮助也可以直接说。')
        return ('请你准备本轮商品加工单导出文件。\n\n'
                f'你来做：从业务系统导出 Excel，放到“{folder / "员工1"}”，该目录保留一份本轮文件。'
                f'作废清单使用“{folder / "员工2"}”中的现有文件，缺少或不是本轮数据时请告诉我。\n'
                '我来做：等你准备好后，检查两份输入的表头、数据和完整性，给你返回检查摘要；目前不会替你操作业务系统导出。\n'
                '接着协作：你确认输入可用后，我把同一版输入交给员工2比对。'+reference+'\n\n'
                +next_reply)
    if state == 'employee1_review':
        report = evidence.get('employee1', {})
        files = '、'.join(Path(f['path']).name for f in report.get('files', []))
        return ('输入检查已完成。'+report.get('summary', '') + ('\n本轮文件：'+files if files else '')+'\n\n'
                '请你确认：这些确实是本轮要使用的文件，数据范围也正确。\n'
                '确认后，我会把这版输入交给员工2进行比对；此时还没有完成商品排查。\n\n'
                '可以继续交给员工2吗？自然回复“没问题”“核对过了”“可以继续”即可；有问题直接说明。只说“收到”时，我会等你看完再交接。')
    if state == 'employee2_wait':
        upstream = evidence.get('employee1', {}).get('summary', '')
        return ('员工1已核对本轮输入，现在请你接手商品交集核查。'+('\n上游情况：'+upstream if upstream else '')+'\n\n'
                '我来做：读取员工1确认的同一版输入，以商品编码精确比对'+source+'与作废清单，生成带商品名称和来源行的核查 CSV。\n'
                '你来做：接收并核对结果，检查商品和来源行是否对应，有疑问告诉我具体位置；不用再手工重复计算。\n'
                '接着协作：你确认结果后，我会复核文件和匹配统计，再向负责人汇报。'+boundary+'\n\n'
                '确认接手后我就开始比对，直接回“收到”“开始吧”或“帮我查一下”都可以。')
    if state == 'employee2_review':
        report = evidence.get('employee2', {})
        return ('核查结果已生成，请查看随附的 CSV。\n'+result_summary(report)+'\n\n'
                '请你核对：匹配商品的编码、名称、来源行与原表是否对应。\n'
                '你确认后，我会再检查输入和成果是否变化、匹配数量是否一致，再给负责人汇报。'+boundary+'\n\n'
                '这份结果可以通过吗？直接说“没问题”“核对过了”“可以继续”即可，有问题请说明哪一项。只说“收到”会保留为待你核对。')
    if state == 'report_pending':
        report = evidence.get('employee2', {})
        return ('本轮商品排查已完成，核查结果见附件。\n\n'
                '协作记录：员工1已确认输入，员工2已确认比对结果，系统最终检查已通过。\n'
                '结果：'+result_summary(report,evidence.get('final', {}).get('summary', ''))+'\n'
                +boundary+'\n\n本轮已结束，不需要再重复确认；如有新的安排可以另行提出。')
    raise ValueError('当前阶段没有对应的任务说明。')


def acknowledgement(state):
    if state == 'employee1_wait':
        return '收到，已记下你接单。我等你准备文件，导好后说“好了”就开始检查；暂时不会把任务交给员工2。'
    if state.endswith('_review'):
        next_step = '交给员工2比对' if state=='employee1_review' else '做最终校验，再汇报负责人'
        return '收到，我先等你核对文件。看完没问题时，直接说“没问题”或“可以继续”，我再'+next_step+'。'
    return '收到，我先不启动。确定开展时说“可以”或“开始吧”即可；有疑问也可以直接问。'


def task_question(text):
    value = text.strip().rstrip('？?。！!').replace(' ', '')
    return value in {'要我做什么','我要做什么','我需要做什么','需要我做什么','我该做什么','这要怎么做',
                     '你会做什么','你接下来做什么','下一步做什么','接下来呢','谁接着做','任务是什么','这是什么任务'}
