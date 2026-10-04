"""Durable all-rank reservation/barrier/cancellation reference API.

Communication groups are distinct from independent fleet shards/paired repeats.
Every host rank must retain its local device lock, actively report readiness and
observe cancellation. Missing ranks expire; partial profiles never succeed.
"""
import json,time,uuid
from pathlib import Path
from .agent.policy import read_policy,timestamp,windows,positive
from .agent.storage import database
from .protocol import digest
from .device_lock import DeviceLock

TERMINAL={'SUCCEEDED','FAILED','UNSUPPORTED'}


class ResourceGroup:
    def __init__(self,root,clock=time.time):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.clock=clock;self.db=database(self.root/'groups.db')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS groups(group_id TEXT PRIMARY KEY,state TEXT,deadline REAL,ready_deadline REAL,reason TEXT,spec TEXT);
        CREATE TABLE IF NOT EXISTS ranks(group_id TEXT,rank INTEGER,state TEXT,release TEXT,report TEXT,PRIMARY KEY(group_id,rank));
        CREATE TABLE IF NOT EXISTS leases(server_id TEXT,device_uid TEXT,group_id TEXT,PRIMARY KEY(server_id,device_uid));''')
    def close(self):self.db.close()
    def submit(self,spec,policies,ready_timeout=30):
        if spec.get('mode')!='communication-group' or spec.get('library')!='deepep-ascend':raise ValueError('explicit deepep communication-group required')
        positive(ready_timeout);positive(spec['pilot_upper_seconds'])
        if not timestamp(spec['valid_from'])<=self.clock()<timestamp(spec['valid_until']):raise ValueError('group validity does not cover current window')
        ranks=sorted(spec['ranks'],key=lambda r:r['rank'])
        if len(ranks)<2 or len(ranks)%2 or sorted(r['rank'] for r in ranks)!=list(range(len(ranks))):raise ValueError('even complete contiguous rank set required')
        if spec.get('evidence_type')!='CPU_FAULT_INJECTION':
            from .libraries import CatalogAdapter
            cases=CatalogAdapter('deepep-ascend').enumerate_cases('full');index=spec.get('case_index',0)
            if type(index) is not int or not 0<=index<len(cases) or len(ranks)!=cases[index]['case']['attributes']['world_size']:raise ValueError('rank count must match frozen case world size')
        if len({(r['server_id'],r['device_uid']) for r in ranks})!=len(ranks):raise ValueError('rank resources must be unique')
        if len({r['environment_tuple_sha256'] for r in ranks})!=1 or not ranks[0]['environment_tuple_sha256']:raise ValueError('matched frozen environment group required')
        ends=[];clock=self.clock();frozen=[]
        for rank in ranks:
            policy=read_policy(policies[rank['rank']]);allowed={d['device_uid']:d['logical_id'] for d in policy['allowed_devices']}
            if policy['server_id']!=rank['server_id'] or allowed.get(rank['device_uid'])!=rank['logical_id'] or not policy['enabled']:raise ValueError('all rank devices/servers must be manually reserved')
            active=[w for w in windows(policy,clock) if w['start']<=clock<w['end']]
            if len(active)!=1:raise ValueError('no common active reservation')
            deadline=min(active[0]['end'],timestamp(spec['valid_until']))
            if deadline-clock<=spec['pilot_upper_seconds']+policy['cleanup_reserve_seconds']:raise ValueError('insufficient shared group budget')
            ends.append(deadline);frozen.append(dict(rank=rank['rank'],policy_sha256=digest(policy),policy=policy))
        group_id=str(uuid.uuid4());deadline=min(ends)
        frozen_spec=dict(spec,ranks=ranks,reservations=frozen,evidence_type=spec.get('evidence_type','NPU_COMMUNICATION_GROUP'))
        with DeviceLock('group-control',self.root/'.locks',timeout=5):
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                for rank in ranks:
                    if self.db.execute('SELECT 1 FROM leases WHERE server_id=? AND device_uid=?',(rank['server_id'],rank['device_uid'])).fetchone():raise ValueError('group resource already leased')
                    self.db.execute('INSERT INTO leases VALUES(?,?,?)',(rank['server_id'],rank['device_uid'],group_id))
                    self.db.execute('INSERT INTO ranks VALUES(?,?,?,?,?)',(group_id,rank['rank'],'WAITING_READY','NOT_CHECKED',None))
                self.db.execute('INSERT INTO groups VALUES(?,?,?,?,?,?)',(group_id,'WAITING_READY',deadline,min(clock+ready_timeout,deadline-max(p['cleanup_reserve_seconds'] for p in policies)),None,json.dumps(frozen_spec)))
        return group_id
    def status(self,group_id):
        row=self.db.execute('SELECT * FROM groups WHERE group_id=?',(group_id,)).fetchone()
        if not row:raise ValueError('unknown group')
        return dict(row,ranks=[dict(r) for r in self.db.execute('SELECT * FROM ranks WHERE group_id=? ORDER BY rank',(group_id,))])
    def metrics(self,group_id):
        data=self.status(group_id)
        if data['state']!='SUCCEEDED':return dict(complete=False,group_latency_us=None,reason='all ranks must have validated complete profiles and release evidence')
        samples={};resources=[];cases=set();presets=set()
        for rank in data['ranks']:
            report=json.loads(rank['report']);root=Path(report['bundle_path'])
            from .agent.storage import verify_bundle
            verify_bundle(root)
            observations=json.loads((root/'observations.json').read_text())
            profile=json.loads((root/'profile.json').read_text());presets.add(profile['preset_sha256'])
            rank_samples={}
            for observation in observations:
                cases.add(observation['case_key'])
                if observation['metric']['name']=='host_elapsed_us':rank_samples[observation['iteration']]=observation['raw_samples'][0]
            if not rank_samples:raise ValueError('missing rank host-latency samples')
            samples[rank['rank']]=rank_samples
            session=json.loads((root/'session.json').read_text())
            resources.extend(session['resource_ledger'])
        if len(cases)!=1 or len(presets)!=1 or len({tuple(sorted(v)) for v in samples.values()})!=1:raise ValueError('inconsistent all-rank case/preset/iterations')
        return dict(complete=True,group_latency_us=[dict(iteration=i,value=max(v[i] for v in samples.values()),rank_samples={str(k):v[i] for k,v in samples.items()}) for i in sorted(next(iter(samples.values())))],definition='maximum synchronized rank-local host range duration; no cross-host timestamp subtraction',logical_chip_hours=sum((r['end_monotonic_ns']-r['start_monotonic_ns'])/1e9 for r in resources)/3600,resource_ledger=resources,wall_clock=None,wall_clock_reason='requires measured central-control interval and clock agreement')

    def cancel(self,group_id,reason):
        with self.db:self.db.execute('UPDATE groups SET state=?,reason=? WHERE group_id=? AND state NOT IN (?,?,?)',('CANCELLING',reason,group_id,'SUCCEEDED','FAILED','UNSUPPORTED'))
    def poll(self,group_id,policies=None):
        data=self.status(group_id);spec=json.loads(data['spec'])
        if data['state'] in TERMINAL:return data
        if policies is not None:
            if len(policies)!=len(spec['reservations']) or any(digest(read_policy(p))!=r['policy_sha256'] or not p['enabled'] for p,r in zip(policies,spec['reservations'])):self.cancel(group_id,'rank reservation revoked or changed')
        if self.clock()>=data['deadline']:self.cancel(group_id,'earliest rank hard deadline reached')
        elif data['state']=='WAITING_READY' and self.clock()>=data['ready_deadline']:self.cancel(group_id,'partial rank readiness expired')
        data=self.status(group_id)
        if data['state']=='CANCELLING' and all(r['release']=='RELEASED' for r in data['ranks']):
            with self.db:
                self.db.execute('UPDATE groups SET state=? WHERE group_id=?',('FAILED',group_id));self.db.execute('DELETE FROM leases WHERE group_id=?',(group_id,))
        return self.status(group_id)
    def ready(self,group_id,rank,ownership):
        data=self.poll(group_id)
        if data['state']!='WAITING_READY':raise ValueError('group not accepting readiness')
        if not ownership.get('device_lock_held') or not ownership.get('environment_verified'):raise ValueError('rank must hold verified local resource before ready')
        with DeviceLock('group-control',self.root/'.locks',timeout=5),self.db:
            if self.status(group_id)['state']!='WAITING_READY':raise ValueError('group readiness cancelled')
            changed=self.db.execute('UPDATE ranks SET state=?,report=? WHERE group_id=? AND rank=? AND state=?',('READY',json.dumps(ownership),group_id,rank,'WAITING_READY')).rowcount
            if changed!=1:raise ValueError('unknown or duplicate rank readiness')
            if all(r['state']=='READY' for r in self.status(group_id)['ranks']):self.db.execute('UPDATE groups SET state=? WHERE group_id=? AND state=?',('RUNNING',group_id,'WAITING_READY'))
    def finish(self,group_id,rank,state,release,report):
        if state not in ('SUCCEEDED','FAILED','CANCELLED') or release not in ('RELEASED','UNKNOWN','RESIDUAL'):raise ValueError('invalid rank outcome')
        data=self.poll(group_id)
        if state=='SUCCEEDED' and not (data['state']=='RUNNING' and report.get('complete_profile') is True and report.get('durable_artifacts') is True and release=='RELEASED'):raise ValueError('partial/unreleased rank cannot succeed')
        if state=='SUCCEEDED':
            if json.loads(data['spec'])['evidence_type']=='CPU_FAULT_INJECTION':raise ValueError('CPU fixture cannot produce successful NPU group')
            from .agent.storage import verify_bundle
            bundle=Path(report['bundle_path']);verify_bundle(bundle)
            observations=json.loads((bundle/'observations.json').read_text())
            from .libraries import CatalogAdapter
            group_spec=json.loads(data['spec']);expected=CatalogAdapter('deepep-ascend').enumerate_cases('full')[group_spec.get('case_index',0)]
            measured=json.loads((bundle/'manifest.json').read_text())
            if measured['library_id']!='deepep-ascend' or measured['case_key']!=expected['case_key']:raise ValueError('rank bundle outside frozen group case')
            if not observations or any(o['rank']!=rank for o in observations):raise ValueError('rank profile mismatch')
        with DeviceLock('group-control',self.root/'.locks',timeout=5),self.db:
            if state=='SUCCEEDED' and self.status(group_id)['state']!='RUNNING':raise ValueError('group cancelled before rank completion')
            row=self.db.execute('SELECT state FROM ranks WHERE group_id=? AND rank=?',(group_id,rank)).fetchone()
            if not row:raise ValueError('unknown rank')
            if row['state'] in ('SUCCEEDED','FAILED','CANCELLED'):raise ValueError('terminal rank cannot be replayed')
            self.db.execute('UPDATE ranks SET state=?,release=?,report=? WHERE group_id=? AND rank=?',(state,release,json.dumps(report),group_id,rank))
        if state!='SUCCEEDED' or release!='RELEASED':self.cancel(group_id,'rank failure or unconfirmed release')
        elif all(r['state']=='SUCCEEDED' for r in self.status(group_id)['ranks']):
            with self.db:
                changed=self.db.execute('UPDATE groups SET state=? WHERE group_id=? AND state=?',('SUCCEEDED',group_id,'RUNNING')).rowcount
                if changed:self.db.execute('DELETE FROM leases WHERE group_id=?',(group_id,))
        return self.poll(group_id)


class GroupRankWorker:
    """Local execution lease; remote control is the same ready/poll/finish API.

    CPU fault injection supplies an explicit command and release checker. Actual
    NPU execution additionally requires the installed adapter capability tuple.
    """
    def __init__(self,group_root,state,rank,policy,command=None,release_check=None,matrix=None):
        self.group_root=Path(group_root);self.root=Path(state);self.root.mkdir(parents=True,exist_ok=True);self.rank=rank;self.policy=Path(policy);self.command=command;self.release=release_check;self.matrix=matrix
    def run(self,group_id):
        from .supervisor import run_owned,terminate_recorded
        from .agent.storage import atomic_json
        from .agent.policy import read_policy
        control=ResourceGroup(self.group_root);data=control.status(group_id);spec=json.loads(data['spec']);rank=next(r for r in spec['ranks'] if r['rank']==self.rank)
        injection=spec['evidence_type']=='CPU_FAULT_INJECTION'
        try:
            if not injection:
                return self._production(control,group_id,spec,rank)
            if not self.command or not self.release:raise ValueError('explicit CPU-only command and release fixture required')
            argv=self.command
            lock_root=self.root/'.device-locks' if injection else '/tmp/kernelx-device-locks'
            with DeviceLock(rank['device_uid'],lock_root) as lock:
                def cancelled():
                    try:
                        current=read_policy(json.loads(self.policy.read_text()))
                        frozen=spec['reservations'][self.rank]
                        if digest(current)!=frozen['policy_sha256'] or not current['enabled']:control.cancel(group_id,'local rank reservation revoked')
                    except (OSError,ValueError,KeyError,TypeError):control.cancel(group_id,'local rank reservation invalid')
                    return control.poll(group_id)['state'] in ('CANCELLING','FAILED','UNSUPPORTED')
                control.ready(group_id,self.rank,dict(device_lock_held=True,environment_verified=True,evidence_type=spec['evidence_type']))
                while control.poll(group_id)['state']=='WAITING_READY':
                    if cancelled():break
                    time.sleep(.02)
                if cancelled():
                    release=self.release() if injection else 'UNKNOWN'
                    return control.finish(group_id,self.rank,'CANCELLED',release,dict(reason='all-rank barrier cancelled'))
                deadline=control.status(group_id)['deadline'];policy=read_policy(json.loads(self.policy.read_text()))
                timeout=max(.01,deadline-time.time()-policy['cleanup_reserve_seconds'])
                result=run_owned(argv,self.root/'rank.log',timeout,pass_fds=(lock.fd,),cancel=cancelled,ownership_path=self.root/'ownership.json')
                if result['process_release']!='RELEASED':terminate_recorded(self.root/'ownership.json')
                if injection:released=self.release()
                else:
                    from .runner import release_check
                    from .probe import Collector
                    released=release_check(rank['logical_id'],result['owned_pids'],Collector(rank['server_id']))['status']
                # CPU tests assert cancellation/ownership only. They can never
                # synthesize successful complete NPU profiles or group latency.
                succeeded=not injection and result['exit_code']==0 and not result['reason'] and released=='RELEASED'
                report=dict(execution=result,evidence_type=spec['evidence_type'],complete_profile=False,durable_artifacts=False)
                atomic_json(self.root/'rank-result.json',report)
                return control.finish(group_id,self.rank,'SUCCEEDED' if succeeded else ('CANCELLED' if result['reason']=='CANCELLED' else 'FAILED'),released,report)
        except (OSError,ValueError,RuntimeError):
            control.cancel(group_id,'rank worker failure');raise
        finally:control.close()

    def _production(self,control,group_id,spec,rank):
        from .probe import probe,Collector
        from .libraries import CatalogAdapter,SupportMatrix
        from .runner import collect,release_check
        from .agent.storage import seal,atomic_json
        environment=probe(rank['server_id']);adapter=CatalogAdapter('deepep-ascend')
        matrix=SupportMatrix(self.matrix) if self.matrix else None
        try:cap=adapter.capabilities(environment,rank['device_uid'],matrix=matrix)
        finally:
            if matrix:matrix.close()
        if cap['status']!='VERIFIED' or cap['tuple_sha256']!=rank['environment_tuple_sha256']:
            control.cancel(group_id,'rank adapter unsupported/unverified or tuple mismatch')
            # No child/context was initialized; this rank owns no NPU process.
            return control.finish(group_id,self.rank,'CANCELLED','RELEASED',dict(reason=cap['reason'],launched=False,evidence=cap))
        rendezvous=spec['rendezvous']
        if not rendezvous['master_addr'] or not 1<=rendezvous['master_port']<=65535:raise ValueError('explicit rendezvous required')
        def cancelled():
            try:
                current=read_policy(json.loads(self.policy.read_text()))
                if digest(current)!=spec['reservations'][self.rank]['policy_sha256']:control.cancel(group_id,'local reservation changed')
            except (OSError,ValueError):control.cancel(group_id,'local reservation invalid')
            return control.poll(group_id)['state'] in ('CANCELLING','FAILED','UNSUPPORTED')
        def ready():
            control.ready(group_id,self.rank,dict(device_lock_held=True,environment_verified=True,tuple_sha256=cap['tuple_sha256']))
            while control.poll(group_id)['state']=='WAITING_READY':
                if cancelled():raise RuntimeError('all-rank barrier cancelled')
                time.sleep(.02)
            if cancelled():raise RuntimeError('all-rank barrier cancelled')
        from datetime import datetime,timezone
        policy=read_policy(json.loads(self.policy.read_text()));run=self.root/'run'
        result=collect(run,server_id=rank['server_id'],device=rank['logical_id'],expected_device_uid=rank['device_uid'],window_start=spec['valid_from'],window_end=datetime.fromtimestamp(control.status(group_id)['deadline'],timezone.utc).isoformat(),authorization_id=policy['reservation_id']+':'+group_id,warmup=spec['warmup'],repeats=spec['repeats'],timeout=spec['pilot_upper_seconds'],cleanup=policy['cleanup_reserve_seconds'],cancel=cancelled,adapter_library='deepep-ascend',case_index=spec.get('case_index',0),rank=self.rank,before_launch=ready,child_environment=dict(RANK=str(self.rank),WORLD_SIZE=str(len(spec['ranks'])),MASTER_ADDR=rendezvous['master_addr'],MASTER_PORT=str(rendezvous['master_port'])))
        report=dict(result,evidence_type='NPU_COMMUNICATION_GROUP',complete_profile=False,durable_artifacts=False)
        if result['valid']:
            seal(run);report.update(complete_profile=True,durable_artifacts=True,bundle_path=str(run.resolve()))
        atomic_json(self.root/'rank-result.json',report)
        return control.finish(group_id,self.rank,'SUCCEEDED' if result['valid'] else 'FAILED',result['device_release'],report)
