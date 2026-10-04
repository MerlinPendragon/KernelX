"""Single-worker scheduling, WAL recovery and independent at-least-once outbox."""
import json
import math
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .policy import read_policy, read_plan, timestamp, windows
from .storage import database, atomic_json, complete_case, seal, verify_bundle, fsync_dir
from ..cann_adapter import CannAddAdapter
from ..device_lock import DeviceLock
from ..probe import Collector, probe
from ..protocol import digest
from ..runner import collect, release_check
from ..supervisor import terminate_recorded


def utc(clock): return datetime.fromtimestamp(clock,timezone.utc).isoformat().replace('+00:00','Z')


class Agent:
    def __init__(self,state,policy,plan,clock=time.time,runner=collect,release=release_check,fault_hook=None,dispatch_context=None):
        self.root=Path(state).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        os.chmod(self.root,0o700)
        self.spool=self.root/'spool'; self.spool.mkdir(exist_ok=True)
        self.policy_path=Path(policy); self.plan_path=Path(plan)
        self.clock=clock; self.runner=runner; self.release=release; self.fault_hook=fault_hook
        self.dispatch_context=dispatch_context
        self.db=database(self.root/'state.db')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS windows(window_id TEXT PRIMARY KEY, start REAL, end REAL, state TEXT, terminal TEXT, reason TEXT, policy_sha256 TEXT, policy TEXT, plan_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS tasks(window_id TEXT, task_id TEXT, state TEXT, reason TEXT, PRIMARY KEY(window_id,task_id), FOREIGN KEY(window_id) REFERENCES windows(window_id));
        CREATE TABLE IF NOT EXISTS attempts(attempt_id TEXT PRIMARY KEY, window_id TEXT, task_id TEXT, state TEXT, output TEXT, started REAL, ended REAL, cost REAL, release_status TEXT, summary TEXT, FOREIGN KEY(window_id,task_id) REFERENCES tasks(window_id,task_id));
        CREATE TABLE IF NOT EXISTS outbox(bundle_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE, path TEXT, manifest TEXT, state TEXT, retries INTEGER DEFAULT 0, next_retry REAL DEFAULT 0, reason TEXT, receipt TEXT);
        CREATE TABLE IF NOT EXISTS devices(device_uid TEXT PRIMARY KEY, state TEXT, reason TEXT, checked REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, scope TEXT, identity TEXT, state TEXT, detail TEXT);
        ''')

    def close(self): self.db.close()
    def hook(self,stage):
        if self.fault_hook: self.fault_hook(stage)

    def configuration(self):
        policy=read_policy(json.loads(self.policy_path.read_text()))
        plan=read_plan(json.loads(self.plan_path.read_text()),policy,CannAddAdapter().manifest)
        return policy,plan

    def event(self,scope,identity,state,detail):
        self.db.execute('INSERT INTO events(at,scope,identity,state,detail) VALUES(?,?,?,?,?)',(self.clock(),scope,identity,state,detail))

    def window_state(self,identity,state,terminal=None,reason=None):
        with self.db:
            self.db.execute('UPDATE windows SET state=?,terminal=?,reason=? WHERE window_id=?',(state,terminal,reason,identity))
            self.event('window',identity,state,reason or terminal or '')

    def revoked(self,policy_hash,plan_hash,window=None):
        try:
            policy,plan=self.configuration(); clock=self.clock()
            return (window is not None and not window['start']<=clock<window['end']) or not policy['enabled'] or digest(policy)!=policy_hash or digest(plan)!=plan_hash or not timestamp(policy['valid_from'])<=clock<timestamp(policy['valid_until']) or not timestamp(plan['valid_from'])<=clock<timestamp(plan['valid_until'])
        except (OSError,ValueError,KeyError,TypeError): return True

    def budget(self,task,plan_hash):
        # Historical p95 is a conservative supplement, never reduces pilot bounds.
        rows=self.db.execute('SELECT a.cost FROM attempts a JOIN outbox o ON o.attempt_id=a.attempt_id JOIN windows w ON w.window_id=a.window_id WHERE a.task_id=? AND w.plan_sha256=? AND a.state=? AND a.cost IS NOT NULL',(task['task_id'],plan_hash,'SUCCEEDED')).fetchall()
        values=sorted(row['cost'] for row in rows)
        return max(task['pilot_upper_seconds'], values[math.ceil(.95*len(values))-1]*1.2 if len(values)>=5 else 0)

    def cache_available(self,policy,task):
        used=sum(p.stat().st_size for p in self.spool.rglob('*') if p.is_file())
        estimated=task['estimated_output_bytes']
        return used+estimated<=policy['spool_max_bytes']*policy['spool_high_watermark'] and shutil.disk_usage(self.spool).free>estimated

    def record_complete(self,context,run,manifest):
        entities,observations,artifacts=complete_case(run)
        preparation=json.loads((run/'preparation.json').read_text())
        authorization=json.loads((run/'authorization.json').read_text())
        expected_authorization=context['policy']['reservation_id']+':'+context['window_id']
        if authorization['authorization_id']!=expected_authorization or entities['session']['window_id']!=expected_authorization or authorization['device_uid']!=context['task']['device_uid'] or authorization['device_id']!=context['logical_id'] or timestamp(entities['session']['started_at'])<context['started']:
            raise ValueError('run does not match frozen reservation identity')
        if entities['session']['device_uids']!=[context['task']['device_uid']] or entities['session']['server_id']!=context['policy']['server_id'] or preparation['warmup']!=context['task']['warmup'] or preparation['repeats']!=context['task']['repeats']:
            raise ValueError('run does not match frozen device/repeat policy')
        self.hook('BEFORE_REGISTER')
        summary=dict(result=json.loads((run/'result.json').read_text()),session=entities['session'],attempt=entities['attempt'],profile=entities['profile'],artifact_index=artifacts,observation_ids=[o['observation_id'] for o in observations])
        with self.db:
            self.db.execute('UPDATE attempts SET state=?,ended=?,cost=?,release_status=?,summary=? WHERE attempt_id=?',('SUCCEEDED',context.get('finished', (run/'result.json').stat().st_mtime),max(0,context.get('finished',(run/'result.json').stat().st_mtime)-context['started']), 'RELEASED',json.dumps(summary),context['attempt_id']))
            self.db.execute('UPDATE tasks SET state=?,reason=NULL WHERE window_id=? AND task_id=?',('SUCCEEDED',context['window_id'],context['task']['task_id']))
            self.db.execute('INSERT OR IGNORE INTO outbox(bundle_id,attempt_id,path,manifest,state) VALUES(?,?,?,?,?)',(manifest['bundle_id'],context['attempt_id'],str(run),json.dumps(manifest),'PENDING'))
            self.event('attempt',context['attempt_id'],'SUCCEEDED','fsynced and checksum verified')
        self.hook('AFTER_REGISTER')

    def quarantine(self,uid,reason):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?)',(uid,'QUARANTINED',reason,self.clock()))
            self.event('device',uid,'QUARANTINED',reason)

    def recover(self):
        for outer in sorted(self.spool.iterdir()):
            if not outer.is_dir() or not (outer/'context.json').is_file(): continue
            context=json.loads((outer/'context.json').read_text()); run=outer/'run'
            row=self.db.execute('SELECT state FROM attempts WHERE attempt_id=?',(context['attempt_id'],)).fetchone()
            if not row or row['state']!='RUNNING': continue
            try:
                if run.is_dir() and not (run/'sealed.json').exists(): atomic_json(run/'agent-context.json',context)
                if (run/'sealed.json').exists(): manifest=verify_bundle(run)
                else: manifest=seal(run)
                self.record_complete(context,run,manifest)
                continue
            except (ValueError,OSError,KeyError,TypeError):
                pass
            # Interrupted workers never turn partial samples into success or retry
            # the same task. Record release evidence without signaling other users.
            ownership=run/'benchmark-ownership.json'
            process_status='UNKNOWN'
            if ownership.exists():
                try: process_status=terminate_recorded(ownership)
                except (ValueError,OSError,KeyError,TypeError): pass
            collector=Collector(context['policy']['server_id'])
            status=self.release(context['logical_id'],[],collector)
            clear=process_status=='RELEASED' and status['status']=='RELEASED' and 'no process' in status['evidence']['stdout'].lower()
            if not clear: self.quarantine(context['task']['device_uid'],'restart: ownership/release not confirmed')
            with self.db:
                self.db.execute('UPDATE attempts SET state=?,ended=?,release_status=?,summary=? WHERE attempt_id=?',('INTERRUPTED',self.clock(),'RELEASED' if clear else 'UNKNOWN',json.dumps(status),context['attempt_id']))
                self.db.execute('UPDATE tasks SET state=?,reason=? WHERE window_id=? AND task_id=?',('INTERRUPTED','restart incomplete case',context['window_id'],context['task']['task_id']))
                self.event('attempt',context['attempt_id'],'INTERRUPTED','incomplete case retained')
        # An interruption before context publication still has a durable DB row.
        with self.db:
            rows=self.db.execute('SELECT * FROM attempts WHERE state=?',('RUNNING',)).fetchall()
            for row in rows:
                self.db.execute('UPDATE attempts SET state=?,ended=?,release_status=? WHERE attempt_id=?',('INTERRUPTED',self.clock(),'UNKNOWN',row['attempt_id']))
                self.db.execute('UPDATE tasks SET state=?,reason=? WHERE window_id=? AND task_id=?',('INTERRUPTED','restart before context publication',row['window_id'],row['task_id']))
                self.event('attempt',row['attempt_id'],'INTERRUPTED','no published context; no automatic retry')

    def upload(self,transport,limit=4):
        if transport is None: return
        for row in self.db.execute('SELECT * FROM outbox WHERE state!=? AND next_retry<=? ORDER BY rowid LIMIT ?',('ACKED',self.clock(),limit)).fetchall():
            try:
                manifest=verify_bundle(row['path']); receipt=transport(Path(row['path']))
                if not isinstance(receipt,dict): raise ValueError('receipt must be an object')
                if receipt.get('bundle_id')!=row['bundle_id'] or receipt.get('manifest_sha256')!=digest(manifest) or receipt.get('durable') is not True or receipt.get('observations')!=len(json.loads((Path(row['path'])/'observations.json').read_text())): raise ValueError('invalid durable import acknowledgment')
                with self.db:
                    self.db.execute('UPDATE outbox SET state=?,receipt=?,reason=NULL WHERE bundle_id=?',('ACKED',json.dumps(receipt),row['bundle_id']))
                    self.event('upload',row['bundle_id'],'ACKED','durable central import acknowledged')
                self.hook('AFTER_ACK')
            except (OSError,ValueError,TimeoutError) as exc:
                retries=row['retries']+1; delay=min(3600,5*2**min(retries-1,10))
                with self.db:
                    self.db.execute('UPDATE outbox SET state=?,retries=?,next_retry=?,reason=? WHERE bundle_id=?',('RETRY',retries,self.clock()+delay,str(exc),row['bundle_id']))
                    self.event('upload',row['bundle_id'],'RETRY',str(exc))
        self.cleanup()

    def cleanup(self):
        for row in self.db.execute('SELECT path FROM outbox WHERE state=?',('ACKED',)).fetchall():
            path=Path(row['path'])
            if path.exists():
                shutil.rmtree(path); fsync_dir(path.parent)
            manifest_row=self.db.execute('SELECT bundle_id FROM outbox WHERE path=?',(str(path),)).fetchone()
            archive=self.spool/'uploads'/(manifest_row['bundle_id']+'.tar')
            if archive.exists(): archive.unlink(); fsync_dir(archive.parent)

    def tick(self,transport=None):
        with DeviceLock('agent-state',self.root/'.locks'):
            self.recover(); self.cleanup()
            # Upload/CPU work can happen outside reservations, NPU tasks cannot.
            self.upload(transport)
            try:
                policy,plan=self.configuration()
            except (OSError,ValueError,KeyError,TypeError) as exc:
                with self.db: self.event('configuration','local','INVALID',str(exc))
                return dict(state='CONFIG_INVALID',reason=str(exc))
            ph=digest(policy); plan_hash=digest(plan); clock=self.clock()
            for row in self.db.execute('SELECT window_id FROM windows WHERE end<=? AND state!=?',(clock,'CLOSED')).fetchall():
                states=[task['state'] for task in self.db.execute('SELECT state FROM tasks WHERE window_id=?',(row['window_id'],))]
                terminal='COMPLETED' if states and all(state=='SUCCEEDED' for state in states) else ('PARTIAL' if 'SUCCEEDED' in states else ('FAILED' if any(state in ('FAILED','INTERRUPTED','REJECTED') for state in states) else 'SKIPPED'))
                self.window_state(row['window_id'],'CLOSED',terminal,'window ended; recovered task states; no catch-up')
            active=None
            for window in windows(policy,clock):
                row=self.db.execute('SELECT * FROM windows WHERE window_id=?',(window['window_id'],)).fetchone()
                if row is None:
                    with self.db:
                        self.db.execute('INSERT INTO windows VALUES(?,?,?,?,?,?,?,?,?)',(window['window_id'],window['start'],window['end'],'WAITING_WINDOW',None,None,ph,json.dumps(policy),plan_hash))
                        self.event('window',window['window_id'],'WAITING_WINDOW','manual reservation')
                # A future reservation is an editable draft. Freeze only when
                # entering preflight; never replace a snapshot already used.
                if row and row['state']=='WAITING_WINDOW' and not self.db.execute('SELECT 1 FROM attempts WHERE window_id=?',(window['window_id'],)).fetchone():
                    with self.db:
                        self.db.execute('UPDATE windows SET start=?,end=?,policy_sha256=?,policy=?,plan_sha256=? WHERE window_id=?',(window['start'],window['end'],ph,json.dumps(policy),plan_hash,window['window_id']))
                if window['end']<=clock:
                    if not row or row['state']!='CLOSED': self.window_state(window['window_id'],'CLOSED','SKIPPED','missed window')
                    continue
                if window['start']<=clock<window['end']: active=window
            if not active:
                self.upload(transport); return dict(state='WAITING_WINDOW',reason='no current reservation; missed windows are skipped')
            wid=active['window_id']; row=self.db.execute('SELECT * FROM windows WHERE window_id=?',(wid,)).fetchone()
            if row['state']=='CLOSED':
                self.upload(transport); return dict(state='CLOSED',terminal=row['terminal'],reason=row['reason'])
            if row['policy_sha256']!=ph or row['plan_sha256']!=plan_hash or self.revoked(ph,plan_hash,active):
                self.window_state(wid,'CLOSED','SKIPPED','reservation/plan revoked, expired or changed')
                self.upload(transport); return dict(state='CLOSED',terminal='SKIPPED',reason='configuration no longer matches frozen window')
            with self.db:
                for task in plan['tasks']: self.db.execute('INSERT OR IGNORE INTO tasks VALUES(?,?,?,NULL)',(wid,task['task_id'],'PENDING'))
            self.window_state(wid,'PREFLIGHT')
            stopped=None
            for task in plan['tasks']:
                state=self.db.execute('SELECT state FROM tasks WHERE window_id=? AND task_id=?',(wid,task['task_id'])).fetchone()['state']
                if state!='PENDING': continue
                if self.revoked(ph,plan_hash,active): stopped='reservation revoked'; break
                remaining=active['end']-self.clock()
                if remaining<=self.budget(task,plan_hash)+policy['cleanup_reserve_seconds']: stopped='insufficient budget before soft cutoff'; break
                if not self.cache_available(policy,task): stopped='spool high watermark or disk capacity'; break
                device=next(d for d in policy['allowed_devices'] if d['device_uid']==task['device_uid'])
                status=self.db.execute('SELECT state FROM devices WHERE device_uid=?',(device['device_uid'],)).fetchone()
                if status and status['state']=='QUARANTINED':
                    with self.db:
                        self.db.execute('UPDATE tasks SET state=?,reason=? WHERE window_id=? AND task_id=?',('REJECTED','device quarantined',wid,task['task_id']))
                        self.event('task',task['task_id'],'REJECTED','device quarantined')
                    continue
                aid=str(uuid.uuid4()); outer=self.spool/aid; run=outer/'run'; started=self.clock()
                with self.db:
                    self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,wid,task['task_id'],'RUNNING',str(run),started,None,None,'NOT_CHECKED',None))
                    self.db.execute('UPDATE tasks SET state=? WHERE window_id=? AND task_id=?',('RUNNING',wid,task['task_id']))
                    self.event('attempt',aid,'RUNNING','frozen plan and policy')
                outer.mkdir(); fsync_dir(self.spool)
                context=dict(attempt_id=aid,window_id=wid,task=task,logical_id=device['logical_id'],started=started,policy=policy,plan=plan)
                if self.dispatch_context: context['fleet']=self.dispatch_context
                atomic_json(outer/'context.json',context)
                self.window_state(wid,'RUNNING')
                try:
                    result=self.runner(run,server_id=policy['server_id'],device=device['logical_id'],expected_device_uid=device['device_uid'],
                        window_start=utc(active['start']),window_end=utc(active['end']),authorization_id=policy['reservation_id']+':'+wid,
                        warmup=task['warmup'],repeats=task['repeats'],timeout=policy['task_timeout_seconds'],cleanup=policy['cleanup_reserve_seconds'],
                        cancel=lambda:self.revoked(ph,plan_hash,active))
                except (OSError,ValueError,RuntimeError) as exc:
                    # The runner may have launched before its final writes failed.
                    result=dict(valid=False,device_release='UNKNOWN',attempt_state='INTERRUPTED',reason=str(exc))
                    self.quarantine(device['device_uid'],'runner raised; execution/release unknown')
                if result.get('valid'):
                    context['finished']=self.clock()
                    atomic_json(outer/'context.json',context)
                    atomic_json(run/'agent-context.json',context)
                    try:
                        manifest=seal(run); self.hook('AFTER_SEAL'); self.record_complete(context,run,manifest)
                    except (ValueError,OSError,KeyError,TypeError) as exc:
                        result=dict(result,valid=False,reason='seal validation failed: '+str(exc))
                if not result.get('valid'):
                    execution_path=run/'execution.json'
                    ownership=run/'benchmark-ownership.json'
                    started_execution=execution_path.exists() or ownership.exists()
                    if started_execution:
                        clear=False
                        try:
                            if execution_path.exists():
                                execution_data=json.loads(execution_path.read_text())
                                clear=result.get('device_release')=='RELEASED' and execution_data.get('process_release')=='RELEASED'
                            if not clear and ownership.exists():
                                process_status=terminate_recorded(ownership)
                                release=self.release(device['logical_id'],[],Collector(policy['server_id']))
                                clear=process_status=='RELEASED' and release['status']=='RELEASED' and 'no process' in release['evidence']['stdout'].lower()
                                result=dict(result,recovery_process_release=process_status,recovery_device_release=release)
                        except (ValueError,OSError,KeyError,TypeError): pass
                        result=dict(result,device_release='RELEASED' if clear else 'UNKNOWN')
                        if not clear: self.quarantine(device['device_uid'],'process or device release unconfirmed after execution')
                    terminal='INTERRUPTED' if result.get('attempt_state')=='INTERRUPTED' else ('FAILED' if started_execution or (run/'attempt.json').exists() else 'REJECTED')
                    failure_summary=dict(result=result)
                    for name in ('attempt','session','execution','device-release','artifacts'):
                        path=run/(name+'.json')
                        if path.is_file():
                            try: failure_summary[name]=json.loads(path.read_text())
                            except (OSError,ValueError) as exc: failure_summary[name]=dict(unreadable=True,reason=str(exc))
                    with self.db:
                        self.db.execute('UPDATE attempts SET state=?,ended=?,cost=?,release_status=?,summary=? WHERE attempt_id=?',(terminal,self.clock(),self.clock()-started,result.get('device_release','UNKNOWN'),json.dumps(failure_summary),aid))
                        self.db.execute('UPDATE tasks SET state=?,reason=? WHERE window_id=? AND task_id=?',(terminal,result.get('reason') or str(result.get('quality')),wid,task['task_id']))
                        self.event('attempt',aid,terminal,json.dumps(result))
                self.window_state(wid,'DRAINING')
            states=[row['state'] for row in self.db.execute('SELECT state FROM tasks WHERE window_id=?',(wid,))]
            terminal='COMPLETED' if states and all(s=='SUCCEEDED' for s in states) else ('PARTIAL' if 'SUCCEEDED' in states else ('SKIPPED' if stopped else 'FAILED'))
            if stopped=='spool high watermark or disk capacity':
                self.window_state(wid,'DRAINING',reason=stopped)
                self.upload(transport)
                return dict(window_id=wid,state='DRAINING',terminal=None,reason=stopped,tasks=states)
            self.window_state(wid,'CLOSED',terminal,stopped)
            self.upload(transport)
            return dict(window_id=wid,state='CLOSED',terminal=terminal,reason=stopped,tasks=states)

    def status(self):
        return {name:[dict(row) for row in self.db.execute('SELECT * FROM '+name)] for name in ('windows','tasks','attempts','outbox','devices','events')}

    def clear_device(self,uid):
        with DeviceLock('agent-state',self.root/'.locks'):
            policy,_=self.configuration(); env=probe(policy['server_id'])
            device=next((d for d in policy['allowed_devices'] if d['device_uid']==uid),None)
            if not device or not any(d['device_uid']==uid and d['logical_id']==device['logical_id'] for d in env['devices']): raise ValueError('device identity mismatch')
            with DeviceLock(uid):
                evidence=self.release(device['logical_id'],[],Collector(policy['server_id']))
                if evidence['status']!='RELEASED' or 'no process' not in evidence['evidence']['stdout'].lower(): raise ValueError('device is not confirmed idle')
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?)',(uid,'READY','manual clear with idle evidence',self.clock()))
                    self.event('device',uid,'READY',json.dumps(evidence))
