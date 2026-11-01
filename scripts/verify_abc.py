"""Real skills, isolated DB, simulated confirmations; never sends to WeChat."""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from zhishi.runtime import build_pilot


class TestChannel:
    enabled = True

    def __init__(self):self.sent = []
    def route(self):return {'account_id':'selfcheck','peer_id':'selfcheck'}
    def send_package(self,text,media,*,expected_route=None):
        assert expected_route==self.route()
        assert all(Path(p).is_file() for p in media)
        self.sent.append(text)
        return 'submitted_unverified','isolated selfcheck; no real transport'


def main():
    with tempfile.TemporaryDirectory() as folder:
        channel = TestChannel()
        pilot = build_pilot(Path(folder)/'test.db',transport=channel)
        demo = pilot.abc
        demo.start(delay=0);demo.tick();demo.notify_tick()
        run = demo.state()['runs'][0]
        assert run['state']=='a_waiting' and len(channel.sent)==1
        demo.complete(run['code'],1,'A');demo.tick();demo.notify_tick()
        run = demo.state()['runs'][0]
        assert run['state']=='b_waiting' and len(channel.sent)==2,run
        demo.complete(run['code'],1,'B');demo.tick();demo.notify_tick()
        run = demo.state()['runs'][0]
        assert run['state']=='completed' and len(channel.sent)==3,run
        assert run['b_report']['rows']==6 and run['b_report']['total_amount']==5000.75
        assert run['events'][-1]['kind']=='C_report_submitted'
        output = demo.output/run['code']/'V1'/'自动自检结果.json'
        output.write_text(json.dumps({'isolated_selfcheck':True,'confirmations_simulated':True,
                                      'real_wechat_sent':False,'code':run['code'],'events':run['events'],
                                      'result':run['b_report']},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'success':True,'code':run['code'],'rows':6,'total_amount':5000.75,
                          'notifications_simulated':len(channel.sent),'real_wechat_sent':False},ensure_ascii=False))


if __name__=='__main__':main()
