"""Local simulated recipients; copies the approved bytes and writes a receipt."""
import shutil
from zhishi.workflows.files import digest, encoded, scoped


class LocalInbox:
    def __init__(self, project, output):
        self.inbox = project / "模拟收件箱"
        self.output = output

    def send(self, run, recipient, assets):
        target=scoped(self.inbox,recipient['folder'])/run['code']/('V'+str(run['version']))
        target.mkdir(parents=True,exist_ok=True)
        for asset in assets:
            source=scoped(self.output,asset['relative_path']);dest=target/asset['name']
            if not dest.exists() or digest(dest)!=asset['sha256']:
                temp=dest.with_name(dest.name+'.tmp');shutil.copyfile(source,temp);temp.replace(dest)
            if digest(dest)!=asset['sha256']: raise ValueError('模拟收件箱文件校验失败。')
        receipt={'demo':True,'recipient':recipient['name'],'job':run['code'],'version':run['version'],'approved_at':run['approved_at'],'approved_digest':run['approved_digest'],'files':[a['name'] for a in assets]}
        (target/'投递回执.json').write_text(encoded(receipt),encoding='utf-8')
