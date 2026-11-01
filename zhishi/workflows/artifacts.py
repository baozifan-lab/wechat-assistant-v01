"""Version-bound artifact access, content verification and approval digest."""
import hashlib
import json
import secrets
from .files import digest, encoded, scoped


class WorkflowArtifacts:
    def __init__(self, store, output):
        self.pilot = store
        self.output = output

    def row(self,code,version):
        with self.pilot.connect() as db:
            value=db.execute('SELECT * FROM workflow_runs WHERE code=? AND version=?',(code,int(version))).fetchone()
            if not value: raise ValueError('任务或版本不存在。')
            return dict(value)

    def assets(self,run):
        assets=json.loads(run['assets_json'])
        if not assets: raise ValueError('尚未生成验收文件。')
        for asset in assets:
            path=scoped(self.output,asset['relative_path'])
            if not path.is_file() or digest(path)!=asset['sha256']: raise ValueError('验收文件已改变，原确认失效，请重新制作。')
        return assets

    def approval_digest(self,run):
        return hashlib.sha256(encoded({'uid':run['uid'],'version':run['version'],'assets':self.assets(run),
            'recipients':json.loads(run['plan_json'])['recipients_snapshot']}).encode()).hexdigest()

    def asset(self,uid,name,token):
        with self.pilot.connect() as db:
            run=db.execute('SELECT * FROM workflow_runs WHERE uid=?',(uid,)).fetchone()
        if not run or not secrets.compare_digest(run['review_token'],token): raise ValueError('验收文件访问凭证无效。')
        for asset in self.assets(dict(run)):
            if asset['name']==name:return scoped(self.output,asset['relative_path'])
        raise ValueError('找不到这份验收文件。')
