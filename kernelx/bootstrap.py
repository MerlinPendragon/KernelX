"""Stable pull/install/session boundary with durable rollback, no pip or SSH."""
import json
import os
import shutil
import ssl
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .agent import Agent, Center
from .agent.policy import read_policy, read_plan, windows, timestamp
from .agent.storage import atomic_json, fsync_dir, database
from .device_lock import DeviceLock
from .probe import probe
from .protocol import digest, validate, case_key
from .release import verify_manifest, compatible, compatibility, extract_payload, check_installed
from .supervisor import run_owned, terminate_recorded


def https(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment: raise ValueError('HTTPS URL without embedded credentials required')
    return url


def download(url,target,limit,ca_file=None):
    https(url)
    class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):
            https(newurl)
            return super().redirect_request(request,fp,code,msg,headers,newurl)
    opener=urllib.request.build_opener(HTTPSRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    with opener.open(url,timeout=15) as response:
        https(response.geturl()); total=0
        with Path(target).open('xb') as output:
            while True:
                chunk=response.read(min(1024*1024,limit-total+1))
                if not chunk: break
                total+=len(chunk)
                if total>limit: raise ValueError('download exceeds signed/configured limit')
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())


class Bootstrap:
    def __init__(self,config,host_probe=probe,executor=run_owned,clock=time.time,fault_hook=None):
        self.config=dict(config); self.clock=clock; self.host_probe=host_probe; self.executor=executor; self.fault_hook=fault_hook
        required={'schema_version','root','trusted_key','server_id','device_uid','policy','plan','center_dir','source','smoke','run_agent','max_package_bytes','disk_reserve_bytes'}
        if not required<=set(config) or set(config)-required-{'ca_file'} or config['schema_version']!=1: raise ValueError('invalid bootstrap configuration')
        uuid.UUID(config['server_id'])
        if type(config['smoke']) is not bool or type(config['run_agent']) is not bool: raise ValueError('boolean smoke/run_agent required')
        for key in ('max_package_bytes','disk_reserve_bytes'):
            if type(config[key]) is not int or config[key]<=0: raise ValueError('positive byte limits required')
        self.root=Path(config['root']).resolve(); self.root.mkdir(parents=True,exist_ok=True); self.root.chmod(0o700)
        for name in ('releases','staging','sessions','runtime'): (self.root/name).mkdir(exist_ok=True)
        self.db=database(self.root/'bootstrap.db')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS releases(release_id TEXT PRIMARY KEY, state TEXT, reason TEXT, manifest TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, state TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, release_id TEXT, state TEXT, started REAL, ended REAL, result TEXT);
        CREATE TABLE IF NOT EXISTS status(key TEXT PRIMARY KEY, payload TEXT);
        ''')

    def close(self): self.db.close()
    def hook(self,stage):
        if self.fault_hook: self.fault_hook(stage)
    def event(self,state,detail):
        with self.db: self.db.execute('INSERT INTO events(at,state,detail) VALUES(?,?,?)',(self.clock(),state,detail))
    def status(self):
        return dict(current=self.current(),releases=[dict(r) for r in self.db.execute('SELECT * FROM releases')],sessions=[dict(r) for r in self.db.execute('SELECT * FROM sessions')],health={r['key']:json.loads(r['payload']) for r in self.db.execute('SELECT * FROM status')},events=[dict(r) for r in self.db.execute('SELECT * FROM events')])
    def health(self,key,value):
        with self.db: self.db.execute('INSERT OR REPLACE INTO status VALUES(?,?)',(key,json.dumps(value)))
    def current(self):
        pointer=self.root/'current'
        if not pointer.is_symlink(): return None
        resolved=pointer.resolve()
        if resolved.parent!=self.root/'releases': raise ValueError('current pointer escapes release root')
        return resolved.name
    def switch(self,identity):
        temporary=self.root/'current.next'
        if temporary.is_symlink(): temporary.unlink()
        if identity:
            os.symlink('releases/'+identity,temporary); os.replace(temporary,self.root/'current')
        elif (self.root/'current').is_symlink(): (self.root/'current').unlink()
        fsync_dir(self.root)
    def bad(self,manifest,reason):
        with self.db: self.db.execute('INSERT OR REPLACE INTO releases VALUES(?,?,?,?)',(manifest['release_id'],'QUARANTINED',reason,json.dumps(manifest)))
        self.event('QUARANTINED',reason)
    def recover(self):
        for row in self.db.execute('SELECT * FROM sessions WHERE state=?',('RUNNING',)).fetchall():
            ownership=self.root/'sessions'/row['session_id']/'ownership.json'
            if ownership.exists() and terminate_recorded(ownership)!='RELEASED': raise ValueError('previous application ownership unconfirmed; update blocked')
            with self.db: self.db.execute('UPDATE sessions SET state=?,ended=? WHERE session_id=?',('INTERRUPTED',self.clock(),row['session_id']))
            self.health('last_failure',dict(session_id=row['session_id'],reason='launcher restart during session'))
        self.recover_runtime()
        journal=self.root/'transition.json'
        if journal.exists():
            transition=json.loads(journal.read_text())
            if transition['state']!='COMMITTED':
                self.switch(transition['previous'])
                self.bad(transition['manifest'],'interrupted update before health commit')
            journal.unlink(); fsync_dir(self.root)

    def recover_runtime(self):
        # Also called synchronously after a launcher failure, before rollback or
        # returning to the caller. Native groups do not belong to its PGID.
        center=Center(self.config['center_dir']); pending=acked=0; devices=[]
        try:
            for directory in (self.root/'runtime').glob('*/agent'):
                agent=Agent(directory,self.config['policy'],self.config['plan'],resource_root=self.root/'runtime/server-resources',cache_root=self.root/'runtime')
                try:
                    agent.recover(); agent.upload(center.import_bundle)
                    status=agent.status(); devices.extend(status['devices']); outbox=status['outbox']
                    pending+=sum(row['state']!='ACKED' for row in outbox)
                    acked+=sum(row['state']=='ACKED' for row in outbox)
                finally: agent.close()
        finally: center.close()
        self.health('upload',dict(pending=pending,acked=acked))
        return dict(state='QUARANTINED' if any(d['state']=='QUARANTINED' for d in devices) else 'RECOVERED',devices=devices,upload=dict(pending=pending,acked=acked))

    def clear_device(self,uid):
        with DeviceLock('bootstrap',self.root/'.locks'):
            self.recover()
            agent=Agent(self.root/'runtime/maintenance/agent',self.config['policy'],self.config['plan'],resource_root=self.root/'runtime/server-resources',cache_root=self.root/'runtime')
            try: agent.clear_device(uid)
            finally: agent.close()

    def acquire(self):
        directory=self.root/'staging'/'download'
        if directory.exists(): shutil.rmtree(directory)
        directory.mkdir()
        source=self.config['source']
        if source.startswith('https://'):
            feed=directory/'feed.json'; download(source,feed,65536,self.config.get('ca_file'))
            base=https(json.loads(feed.read_text())['release_url']).rstrip('/')
            for name,limit in (('manifest.json',1024*1024),('manifest.sig',65536)):
                download(base+'/'+name,directory/name,limit,self.config.get('ca_file'))
            local=None
        else:
            if '://' in source: raise ValueError('HTTPS or offline repository directory required')
            local=Path(source)
            # Offline latest.json uses a content-addressed directory name.
            identity=json.loads((local/'latest.json').read_text())['release_id']
            if not isinstance(identity,str) or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity): raise ValueError('invalid offline release ID')
            local=local/identity
            for name in ('manifest.json','manifest.sig'): shutil.copyfile(local/name,directory/name)
        manifest=verify_manifest(directory,self.config['trusted_key'])
        row=self.db.execute('SELECT state FROM releases WHERE release_id=?',(manifest['release_id'],)).fetchone()
        if row and row['state']=='QUARANTINED': return directory,manifest,'QUARANTINED'
        self.environment=self.host_probe(self.config['server_id'])
        host=compatibility(self.environment,self.config['device_uid'])
        compatible(manifest,host)
        floor=self.db.execute('SELECT payload FROM status WHERE key=?',('release_floor',)).fetchone()
        if floor:
            previous=json.loads(floor['payload'])
            if manifest['sequence']<previous['sequence'] or (manifest['sequence']==previous['sequence'] and manifest['release_id']!=previous['release_id']): raise ValueError('signed release sequence rollback/conflict')
        size=manifest['artifact']['bytes']; needed=size+manifest['artifact']['unpacked_bytes']+self.config['disk_reserve_bytes']
        if size>self.config['max_package_bytes'] or shutil.disk_usage(self.root).free<needed: raise ValueError('package limit or disk reserve exceeded')
        installed=self.root/'releases'/manifest['release_id']
        if installed.exists():
            check_installed(installed,manifest); return directory,manifest,'INSTALLED'
        try:
            if local:
                if (local/'payload.tar.gz').stat().st_size!=size: raise ValueError('offline payload size mismatch')
                shutil.copyfile(local/'payload.tar.gz',directory/'payload.tar.gz')
            else: download(base+'/payload.tar.gz',directory/'payload.tar.gz',size,self.config.get('ca_file'))
            candidate=self.root/'staging'/manifest['release_id']
            if candidate.exists(): shutil.rmtree(candidate)
            extract_payload(directory/'payload.tar.gz',candidate,manifest)
            shutil.copyfile(directory/'manifest.json',candidate/'manifest.json'); shutil.copyfile(directory/'manifest.sig',candidate/'manifest.sig')
            for path in candidate.iterdir():
                if path.is_file():
                    with path.open('rb') as stream: os.fsync(stream.fileno())
            fsync_dir(candidate); os.replace(candidate,installed); fsync_dir(installed.parent)
            with self.db: self.db.execute('INSERT OR REPLACE INTO releases VALUES(?,?,?,?)',(manifest['release_id'],'STAGED',None,json.dumps(manifest)))
            self.hook('AFTER_STAGE')
        except (OSError,ValueError) as exc:
            self.bad(manifest,str(exc)); raise
        return directory,manifest,'INSTALLED'

    def env(self,manifest):
        return dict(os.environ,PYTHONPATH=str(self.root/'releases'/manifest['release_id']),PYTHONDONTWRITEBYTECODE='1',KERNELX_RELEASE_ID=manifest['release_id'],KERNELX_GIT_COMMIT=manifest['git_commit'])

    def plan(self,manifest,policy):
        installed=self.root/'releases'/manifest['release_id']
        check_installed(installed,manifest)
        path='kernelx/manifests/cann_add.json'
        if path not in manifest['case_manifests']: raise ValueError('candidate case manifest missing')
        case_manifest=json.loads((installed/path).read_text())
        validate('case',case_manifest['case'])
        if case_key(case_manifest['case'])!=case_manifest['case_key']: raise ValueError('candidate case key mismatch')
        return read_plan(json.loads(Path(self.config['plan']).read_text()),policy,case_manifest)

    def session(self,manifest,smoke=False):
        policy=read_policy(json.loads(Path(self.config['policy']).read_text()))
        if policy['server_id']!=self.config['server_id'] or not any(d['device_uid']==self.config['device_uid'] for d in policy['allowed_devices']): raise ValueError('bootstrap policy binding mismatch')
        plan=self.plan(manifest,policy)
        sid=str(uuid.uuid4()); directory=self.root/'sessions'/sid; directory.mkdir()
        atomic_json(directory/'environment.json',self.environment)
        atomic_json(directory/'plan.json',plan); atomic_json(directory/'policy-snapshot.json',policy)
        role='smoke-'+manifest['release_id'] if smoke else 'main'
        state=self.root/'runtime'/role/'agent'
        argv=[sys.executable,'-B','-m','kernelx','agent-tick','--state',str(state),'--policy',self.config['policy'],'--plan',str(directory/'plan.json'),'--center-dir',self.config['center_dir'],
              '--resource-root',str(self.root/'runtime/server-resources'),'--cache-root',str(self.root/'runtime')]
        with self.db: self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)',(sid,manifest['release_id'],'RUNNING',self.clock(),None,None))
        # Runner's NPU deadline is the authorized window. Allow that whole
        # window plus per-task CPU preparation/export and final import overhead;
        # a multi-task session must not inherit a single benchmark timeout.
        remaining=max([0]+[min(w['end'],timestamp(plan['valid_until']))-self.clock()
                          for w in windows(policy,self.clock()) if w['start']<=self.clock()<w['end']])
        timeout=remaining+sum(max(t['pilot_upper_seconds'],policy['task_timeout_seconds'])+360 for t in plan['tasks'])+120
        try:
            execution=self.executor(argv,directory/'application.log',timeout,env=self.env(manifest),grace=10,pass_fds=(self.lock.fd,),ownership_path=directory/'ownership.json',cwd=self.root/'releases'/manifest['release_id'])
            if execution['exit_code']!=0 or execution.get('reason'): raise ValueError('application session failed; see '+str(directory/'application.log'))
            result=json.loads((directory/'application.log').read_text().strip().splitlines()[-1])
            agent=Agent(state,self.config['policy'],directory/'plan.json',resource_root=self.root/'runtime/server-resources',cache_root=self.root/'runtime')
            try: local=agent.status()
            finally: agent.close()
            success=result.get('terminal')=='COMPLETED'
            failed=result.get('state')=='CONFIG_INVALID' or result.get('terminal') in ('FAILED','PARTIAL')
            with self.db: self.db.execute('UPDATE sessions SET state=?,ended=?,result=? WHERE session_id=?',('SUCCEEDED' if success else ('FAILED' if failed else 'IDLE'),self.clock(),json.dumps(result),sid))
            self.health('heartbeat',dict(at=self.clock(),release_id=manifest['release_id']))
            # Preserve the server-wide count; include this just-finished role.
            pending=acked=0
            for directory in (self.root/'runtime').glob('*/agent'):
                ledger=database(directory/'state.db')
                try:
                    for row in ledger.execute('SELECT state FROM outbox'):
                        pending+=row['state']!='ACKED'; acked+=row['state']=='ACKED'
                finally: ledger.close()
            self.health('upload',dict(pending=pending,acked=acked))
            if success: self.health('last_success',dict(session_id=sid,release_id=manifest['release_id'],at=self.clock(),smoke=smoke))
            elif failed or any(a['state'] in ('FAILED','INTERRUPTED','REJECTED') for a in local['attempts']): self.health('last_failure',dict(session_id=sid,result=result))
            if smoke and not success: raise ValueError('NPU smoke incomplete: '+str(result))
            return result
        except (OSError,ValueError,KeyError,IndexError,RuntimeError) as exc:
            # Stop the launcher before recovering its independently sessioned
            # native Runner. UNKNOWN remains quarantined across releases.
            ownership=self.root/'sessions'/sid/'ownership.json'
            try:
                if ownership.exists() and terminate_recorded(ownership,grace=10)!='RELEASED':
                    agent=Agent(state,self.config['policy'],self.config['plan'],resource_root=self.root/'runtime/server-resources',cache_root=self.root/'runtime')
                    try:
                        for task in plan['tasks']: agent.quarantine(task['device_uid'],'launcher ownership/release unconfirmed')
                    finally: agent.close()
                    raise ValueError('launcher ownership/release unconfirmed')
                recovery=self.recover_runtime()
                atomic_json(self.root/'sessions'/sid/'recovery.json',recovery)
            except (OSError,ValueError,KeyError,TypeError,RuntimeError) as recovery_error:
                agent=Agent(state,self.config['policy'],self.config['plan'],resource_root=self.root/'runtime/server-resources',cache_root=self.root/'runtime')
                try:
                    for task in plan['tasks']: agent.quarantine(task['device_uid'],'session cleanup failed: '+str(recovery_error))
                finally: agent.close()
                atomic_json(self.root/'sessions'/sid/'recovery.json',dict(state='QUARANTINED',reason=str(recovery_error)))
                exc=ValueError(str(exc)+'; recovery failed: '+str(recovery_error))
            with self.db: self.db.execute('UPDATE sessions SET state=?,ended=?,result=? WHERE session_id=?',('FAILED',self.clock(),json.dumps(dict(reason=str(exc))),sid))
            self.health('last_failure',dict(session_id=sid,release_id=manifest['release_id'],at=self.clock(),reason=str(exc),smoke=smoke))
            raise ValueError(str(exc)) from exc

    def update(self):
        _,manifest,state=self.acquire(); identity=manifest['release_id']
        if state=='QUARANTINED': return dict(state='QUARANTINED',release_id=identity)
        if self.current()==identity: return dict(state='CURRENT',release_id=identity)
        installed=self.root/'releases'/identity
        execution=self.executor([sys.executable,'-B','-m','kernelx','release-self-test'],self.root/'staging/self-test.log',30,env=self.env(manifest),cwd=installed)
        try:
            healthy=json.loads((self.root/'staging/self-test.log').read_text().strip().splitlines()[-1])
        except (OSError,ValueError,IndexError): healthy={}
        if execution['exit_code']!=0 or execution.get('reason') or healthy.get('healthy') is not True or healthy.get('protocol')!=manifest['protocol_version'] or healthy.get('npu_used') is not False:
            self.bad(manifest,'CPU dependency/schema self-test failed'); return dict(state='QUARANTINED',release_id=identity)
        if self.config['smoke']:
            policy=read_policy(json.loads(Path(self.config['policy']).read_text())); plan=self.plan(manifest,policy)
            available=any(w['start']<=self.clock()<w['end'] and w['end']-self.clock()>sum(t['pilot_upper_seconds'] for t in plan['tasks'])+policy['cleanup_reserve_seconds'] for w in windows(policy,self.clock()))
            if not policy['enabled'] or not available or not timestamp(plan['valid_from'])<=self.clock()<timestamp(plan['valid_until']): return dict(state='WAITING_SMOKE_WINDOW',release_id=identity)
        previous=self.current(); transition=dict(state='SWITCHED',previous=previous,manifest=manifest)
        atomic_json(self.root/'transition.json',transition)
        self.switch(identity); self.hook('AFTER_SWITCH')
        try:
            if self.config['smoke']: self.session(manifest,smoke=True)
            with self.db:
                self.db.execute('UPDATE releases SET state=?,reason=NULL WHERE release_id=?',('HEALTHY',identity))
                self.db.execute('INSERT OR REPLACE INTO status VALUES(?,?)',('release_floor',json.dumps(dict(sequence=manifest['sequence'],release_id=identity))))
            transition['state']='COMMITTED'; atomic_json(self.root/'transition.json',transition)
            self.hook('AFTER_COMMIT'); (self.root/'transition.json').unlink(); fsync_dir(self.root)
            self.event('HEALTHY',identity)
            return dict(state='HEALTHY',release_id=identity)
        except (OSError,ValueError) as exc:
            self.switch(previous); self.bad(manifest,str(exc)); (self.root/'transition.json').unlink(); fsync_dir(self.root)
            return dict(state='ROLLED_BACK',release_id=identity,reason=str(exc))

    def tick(self):
        with DeviceLock('bootstrap',self.root/'.locks') as lock:
            self.lock=lock; self.recover()
            try: result=self.update()
            except (OSError,ValueError,KeyError,TypeError) as exc:
                result=dict(state='UPDATE_REJECTED',reason=str(exc)); self.event('UPDATE_REJECTED',str(exc))
            current=self.current(); runnable=False; application_failed=False
            if current:
                try:
                    manifest=verify_manifest(self.root/'releases'/current,self.config['trusted_key']); check_installed(self.root/'releases'/current,manifest)
                    self.environment=self.host_probe(self.config['server_id'])
                    host=compatibility(self.environment,self.config['device_uid']); compatible(manifest,host)
                    row=self.db.execute('SELECT state FROM releases WHERE release_id=?',(current,)).fetchone()
                    if not row or row['state']!='HEALTHY': raise ValueError('current release is not healthy')
                    runnable=True
                    if self.config['run_agent']:
                        result['application']=self.session(manifest)
                        application_failed=(result['application'].get('state')=='CONFIG_INVALID' or result['application'].get('terminal') in ('FAILED','PARTIAL'))
                except (OSError,ValueError,RuntimeError) as exc:
                    application_failed=True; result['application_error']=str(exc)
                    self.health('last_failure',dict(at=self.clock(),reason=str(exc)))
            # Waiting for manual authorization is a successful deferred tick.
            # Failed required smoke is fatal even if rollback retained an old app.
            result['runnable']=runnable
            result['exit_code']=1 if application_failed or result['state']=='ROLLED_BACK' or (not runnable and result['state']!='WAITING_SMOKE_WINDOW') else 0
            self.health('heartbeat',dict(at=self.clock(),release_id=self.current(),update_state=result['state']))
            return result
