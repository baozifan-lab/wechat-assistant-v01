"""Run real local skills with simulated approvals in an isolated temporary database."""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from zhishi.service import Pilot
from zhishi.workflows.two_stage import TwoStageDemo


def main():
    print('自动自检：使用临时数据库与模拟 A/B 确认，不修改工作台中的人工验收记录，不发送消息。',flush=True)
    with tempfile.TemporaryDirectory(prefix='zhishi-two-stage-') as directory:
        pilot = Pilot(Path(directory)/'selfcheck.db')
        demo = TwoStageDemo(pilot,ROOT)
        demo.start()
        demo.tick()
        run = demo.state()['runs'][0]
        if run['state']!='a_review':
            raise RuntimeError(run['error'] or '第一段未完成')
        assert run['b_attempts']==0 and run['events']==[]
        demo.approve(run['code'],run['version'],'A','A')
        # Exercise recovery from a saved handoff before running the actual second skill.
        demo = TwoStageDemo(pilot,ROOT)
        demo.tick()
        run = demo.state()['runs'][0]
        if run['state']!='b_review':
            raise RuntimeError(run['error'] or '第二段未完成')
        summary = run['b_report']
        assert summary['rows']==6 and summary['total_amount']==5000.75 and summary['zero_amount_rows']==1
        groups = {g['department']:(g['rows'],g['total_amount']) for g in summary['departments']}
        assert groups=={'销售一部':(3,1850.75),'销售二部':(3,3150)}
        assert run['events'][0]['state']=='consumed'
        demo.approve(run['code'],run['version'],'B','B')
        assert demo.state()['runs'][0]['state']=='completed'
        result = {'demo':True,'automated_selfcheck':True,'code':run['code'],'upstream_rows':run['a_report']['input_rows'],
                  'retained_rows':summary['rows'],'total_amount':summary['total_amount'],
                  'departments':summary['departments'],'handoff_consumed':True,'final_state':'completed',
                  'model_used':False,'real_recipients_contacted':False}
        folder = demo.output/run['code']/('V'+str(run['version']))
        (folder/'自动自检结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        print('自检材料：'+str(folder),flush=True)


if __name__=='__main__':
    main()
