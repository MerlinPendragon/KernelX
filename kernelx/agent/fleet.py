"""Explicitly bound fleet reference control plane; no SSH or NPU bypass.

The local durable API can be hosted by a deployment service. Only the shipped
CANN Add manifest is executable; other libraries remain adapter-unconfigured
until #5 supplies enumerators/runners and measured capability attestations.
"""
import json
import uuid
from collections import Counter
from pathlib import Path

from .engine import Agent
from .policy import read_policy, timestamp
from .storage import atomic_json, database
from ..cann_adapter import CannAddAdapter,PRESET
from ..device_lock import DeviceLock
from ..protocol import digest

LIBRARIES=('cann-opp','ops-nn','ops-transformer','sgl-kernel-npu',
           'tile-kernels','deepgemm-ascend','deepep-ascend')
BLOCKED={'UNSUPPORTED','UNVERIFIED','ADAPTER_UNCONFIGURED'}
TERMINAL={'INGESTED','FAILED','INTERRUPTED'} | BLOCKED


class Fleet:
    """SQLite reference API; each process/thread opens its own connection."""
    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.db=database(self.root/'fleet.db')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, submission_id TEXT UNIQUE, sha256 TEXT, plan TEXT);
        CREATE TABLE IF NOT EXISTS dispatches(dispatch_id TEXT PRIMARY KEY, run_id TEXT, server_id TEXT, library TEXT, state TEXT, reason TEXT, plan TEXT, report TEXT, parent_id TEXT);
        CREATE TABLE IF NOT EXISTS reports(dispatch_id TEXT, sequence INTEGER, sha256 TEXT, payload TEXT, PRIMARY KEY(dispatch_id,sequence));
        ''')

    def close(self): self.db.close()

    def submit(self,request):
        required={'submission_id','libraries','scope','servers','mode','valid_from','valid_until','warmup','repeats','pilot_upper_seconds','estimated_output_bytes'}
        if set(request)!=required: raise ValueError('invalid fleet submission fields')
        libraries=list(LIBRARIES) if request['libraries']=='all' else request['libraries']
        if not isinstance(libraries,list) or not libraries or len(set(libraries))!=len(libraries) or set(libraries)-set(LIBRARIES): raise ValueError('unknown/duplicate libraries')
        if not request['submission_id'] or request['scope'] not in ('core','full') or request['mode'] not in ('shard','paired'): raise ValueError('submission ID, scope and explicit dispatch mode required')
        if timestamp(request['valid_until'])<=timestamp(request['valid_from']): raise ValueError('empty run validity')
        for name in ('warmup','repeats'):
            if type(request[name]) is not int or not 1<=request[name]<=1000: raise ValueError('invalid repeat policy')
        from .policy import positive
        for name in ('pilot_upper_seconds','estimated_output_bytes'): positive(request[name])
        servers=request['servers']
        if not isinstance(servers,list) or not servers or len({s['server_id'] for s in servers})!=len(servers): raise ValueError('unique registered servers required')
        for server in servers:
            if set(server)!={'server_id','device_uid','capabilities'} or not server['device_uid']: raise ValueError('invalid server binding')
            uuid.UUID(server['server_id'])
        # The submission is immutable and retryable, including server snapshots.
        frozen=dict(request,libraries=libraries); fingerprint=digest(frozen)
        with DeviceLock('fleet-control',self.root/'.locks',timeout=15):
            old=self.db.execute('SELECT * FROM runs WHERE submission_id=?',(request['submission_id'],)).fetchone()
            if old:
                if old['sha256']!=fingerprint: raise ValueError('submission ID reused with different immutable plan')
                return old['run_id']
            run_id=str(uuid.uuid4()); manifest=CannAddAdapter().manifest
            dispatches=[]; cursor=0
            for library in libraries:
                eligible=[]; rejected=[]
                for server in servers:
                    if library in ('sgl-kernel-npu','tile-kernels','deepgemm-ascend','deepep-ascend'):
                        from ..libraries import CatalogAdapter
                        cap=server['capabilities'].get(library,{})
                        state=cap.get('status','UNVERIFIED');reason=cap.get('reason','missing exact measured tuple')
                        expected=CatalogAdapter(library).manifest(request['scope'])
                        if state=='VERIFIED' and (cap.get('manifest_sha256')!=digest(expected) or not cap.get('tuple_sha256') or not cap.get('evidence',{}).get('bundle_sha256')):
                            state,reason='UNVERIFIED','stale or incomplete manifest/provider attestation'
                        if library=='deepep-ascend' and state=='VERIFIED':state,reason='UNVERIFIED','submit an explicit jointly reserved communication-group; never independent shards'
                    elif library!='cann-opp':
                        state,reason='ADAPTER_UNCONFIGURED','independent component performance catalog unavailable'
                    else:
                        cap=server['capabilities'].get('cann-opp',server['capabilities'].get('cann-add',{}))
                        state=cap.get('status','UNVERIFIED'); reason=cap.get('reason','no measured support-matrix attestation')
                        if 'environment_tuple' in cap:
                            binding=cap['environment_tuple'];proof=cap.get('evidence',{})
                            matched=(binding.get('library')=='cann-opp' and binding.get('manifest_sha256')==digest(manifest) and binding.get('preset_sha256')==digest(PRESET) and cap.get('tuple_sha256')==digest(binding) and cap.get('manifest_sha256')==digest(manifest) and proof.get('valid') is True and proof.get('provider_verified') is True and proof.get('bundle_sha256') and proof.get('evidence_uri'))
                            if state=='VERIFIED' and not matched:state,reason='UNVERIFIED','incomplete/stale inventory capability evidence'
                        else:
                            evidence={'soc','bin','cann','library_version','library_commit','preset','manifest_sha256','environment_sha256','evidence_uri'}
                            if state=='VERIFIED' and (not evidence<=set(cap) or cap['manifest_sha256']!=digest(manifest) or cap['preset']!='latency-v1' or not cap['environment_sha256'] or not cap['evidence_uri']):
                                state,reason='UNVERIFIED','incomplete/stale capability tuple or manifest/preset mismatch'

                        if state not in ('VERIFIED','UNSUPPORTED','UNVERIFIED'): raise ValueError('invalid capability status')
                    if state=='VERIFIED': eligible.append(server)
                    else: rejected.append((server,state,reason))
                selected=eligible if request['mode']=='paired' else ([eligible[cursor%len(eligible)]] if eligible else [])
                cursor+=1
                for server,state,reason in rejected:
                    dispatches.append(self._dispatch(run_id,library,server,state,reason,None))
                for server in selected:
                    from ..libraries import CatalogAdapter,FrozenPerformanceAdapter
                    entries=[None] if library=='cann-opp' else CatalogAdapter(library).enumerate_cases(request['scope'])
                    all_cases=[] if library=='cann-opp' else CatalogAdapter(library).enumerate_cases('full')
                    for entry in entries:
                        index=next((i for i,r in enumerate(all_cases) if r['case_key']==entry['case_key']),0) if entry else None
                        bound=manifest if entry is None else FrozenPerformanceAdapter(library,index).manifest
                        task=dict(task_id='add' if entry is None else entry['case_key'],adapter='cann-add' if entry is None else library,device_uid=server['device_uid'],
                                  **{k:request[k] for k in ('warmup','repeats','pilot_upper_seconds','estimated_output_bytes')})
                        if entry is not None:task['case_index']=index
                        plan=dict(schema_version=1,plan_id=run_id+':'+server['server_id']+':'+task['task_id'],valid_from=request['valid_from'],valid_until=request['valid_until'],manifest_sha256=digest(bound),preset='latency-v1',tasks=[task])
                        dispatches.append(self._dispatch(run_id,library,server,'PENDING',None,plan,task['task_id']))
                if library=='cann-opp':
                    # Add is a validated seed, not a whole-library/core manifest.
                    # Keep this gap queryable instead of inflating coverage.
                    dispatches.append(self._dispatch(run_id,library,servers[0],'ADAPTER_UNCONFIGURED',request['scope']+' whole-library enumeration requires #5; shipped seed is Add only',None,'manifest-gap'))
            with self.db:
                self.db.execute('INSERT INTO runs VALUES(?,?,?,?)',(run_id,request['submission_id'],fingerprint,json.dumps(frozen)))
                self.db.executemany('INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?,?)',dispatches)
            return run_id

    def _dispatch(self,run_id,library,server,state,reason,plan,tag='case'):
        identity=digest(dict(run_id=run_id,library=library,server_id=server['server_id'],tag=tag))
        return (identity,run_id,server['server_id'],library,state,reason,json.dumps(plan) if plan else None,None,None)

    def pull(self,server_id):
        return [dict(row) for row in self.db.execute('SELECT * FROM dispatches WHERE server_id=? AND state IN (?,?,?) ORDER BY rowid',(server_id,'PENDING','RUNNING','PENDING_UPLOAD'))]

    def report(self,server_id,dispatch_id,sequence,payload):
        if type(sequence) is not int or sequence<1: raise ValueError('positive report sequence required')
        with DeviceLock('fleet-control',self.root/'.locks',timeout=15):
            row=self.db.execute('SELECT * FROM dispatches WHERE dispatch_id=?',(dispatch_id,)).fetchone()
            if not row or row['server_id']!=server_id: raise ValueError('dispatch server binding mismatch')
            state=payload['state']
            if state not in ('PENDING','RUNNING','PENDING_UPLOAD','INGESTED','FAILED','INTERRUPTED'): raise ValueError('invalid worker state')
            sha=digest(payload)
            old=self.db.execute('SELECT sha256 FROM reports WHERE dispatch_id=? AND sequence=?',(dispatch_id,sequence)).fetchone()
            if old:
                if old['sha256']!=sha: raise ValueError('conflicting report identity')
                return
            latest=self.db.execute('SELECT MAX(sequence) FROM reports WHERE dispatch_id=?',(dispatch_id,)).fetchone()[0] or 0
            if sequence<=latest: raise ValueError('stale report sequence')
            if row['state'] in TERMINAL: raise ValueError('terminal dispatch cannot be replayed; retry explicitly')
            if row['state']=='PENDING_UPLOAD' and state in ('PENDING','RUNNING'): raise ValueError('captured case cannot be remeasured')
            with self.db:
                self.db.execute('INSERT INTO reports VALUES(?,?,?,?)',(dispatch_id,sequence,sha,json.dumps(payload)))
                self.db.execute('UPDATE dispatches SET state=?,reason=?,report=? WHERE dispatch_id=?',(state,payload.get('reason'),json.dumps(payload),dispatch_id))

    def retry(self,dispatch_id,retry_id):
        """Explicit new bound dispatch; preserve failed attempt and old evidence."""
        if not retry_id: raise ValueError('explicit retry ID required')
        with DeviceLock('fleet-control',self.root/'.locks',timeout=15):
            row=self.db.execute('SELECT * FROM dispatches WHERE dispatch_id=?',(dispatch_id,)).fetchone()
            if not row or row['state'] not in ('FAILED','INTERRUPTED'): raise ValueError('only failed/interrupted dispatch can be retried')
            identity=digest(dict(parent=dispatch_id,retry_id=retry_id))
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO dispatches VALUES(?,?,?,?,?,?,?,?,?)',(identity,row['run_id'],row['server_id'],row['library'],'PENDING',None,row['plan'],None,dispatch_id))
            return identity

    def status(self,run_id):
        row=self.db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if not row: raise ValueError('unknown global run')
        dispatches=[dict(item) for item in self.db.execute('SELECT * FROM dispatches WHERE run_id=? ORDER BY rowid',(run_id,))]
        counts=Counter(d['state'] for d in dispatches)
        state='INGESTED' if counts['INGESTED']==len(dispatches) else ('PARTIAL' if counts['INGESTED'] else ('RUNNING' if counts['RUNNING'] else 'PENDING'))
        if all(d['state'] in TERMINAL for d in dispatches) and not counts['INGESTED']: state='UNSUPPORTED' if all(d['state'] in BLOCKED for d in dispatches) else 'FAILED'
        grouped=lambda key:{value:dict(Counter(d['state'] for d in dispatches if d[key]==value)) for value in sorted({d[key] for d in dispatches})}
        return dict(run_id=run_id,state=state,counts=dict(counts),libraries=grouped('library'),servers=grouped('server_id'),dispatches=dispatches,plan=json.loads(row['plan']))


class FleetWorker:
    """Agent actively pulls only its bindings; reservations remain authoritative.

    No service is installed here. Invoke tick from the deployment's local timer.
    A report journal survives unavailable control API and ACK loss. Completed
    local captures remain authoritative even if control progress is stale.
    """
    def __init__(self,state,policy,fleet,transport=None,agent_options=None):
        self.root=Path(state); self.root.mkdir(parents=True,exist_ok=True)
        self.policy=Path(policy); self.fleet=fleet; self.transport=transport
        self.options=agent_options or {}

    def _agent(self,directory,plan,context=None):
        return Agent(directory/'agent',self.policy,plan,dispatch_context=context,
                     resource_root=self.root/'server-resources',cache_root=self.root,**self.options)

    def _policy(self):
        policy=read_policy(json.loads(self.policy.read_text()))
        identity=self.root/'server-identity.json'
        if identity.exists():
            if json.loads(identity.read_text())['server_id']!=policy['server_id']:
                raise ValueError('worker state belongs to another registered server')
        else: atomic_json(identity,dict(server_id=policy['server_id']))
        return policy

    def clear_device(self,uid):
        # The same server lock protects preflight, recovery and manual clearance.
        with DeviceLock('fleet-worker',self.root/'.locks'):
            self._policy()
            for plan_path in self.root.glob('*/plan.json'):
                agent=self._agent(plan_path.parent,plan_path)
                try: agent.recover()
                finally: agent.close()
            agent=self._agent(self.root/'maintenance','')
            try: agent.clear_device(uid)
            finally: agent.close()

    def _send(self,directory,server_id):
        journal=directory/'report.json'
        if not journal.exists(): return
        data=json.loads(journal.read_text())
        self.fleet.report(server_id,data['dispatch_id'],data['sequence'],data['payload'])

    def _report(self,directory,server_id,dispatch,state,reason=None,agent=None):
        journal=directory/'report.json'; old=json.loads(journal.read_text()) if journal.exists() else None
        payload=dict(state=state,reason=reason,global_run_id=dispatch['run_id'],dispatch_id=dispatch['dispatch_id'],server_id=server_id)
        if agent:
            local=agent.status()
            payload.update(attempts=local['attempts'],bundles=local['outbox'])
        sequence=old['sequence']+1 if old else 1
        atomic_json(journal,dict(dispatch_id=dispatch['dispatch_id'],sequence=sequence,payload=payload))
        self._send(directory,server_id)

    def tick(self):
        with DeviceLock('fleet-worker',self.root/'.locks'):
            # Complete captures upload even when policy/plan publication is broken.
            for plan_path in self.root.glob('*/plan.json'):
                agent=self._agent(plan_path.parent,plan_path)
                try: agent.recover(); agent.upload(self.transport)
                finally: agent.close()
            try: policy=self._policy()
            except (ValueError,OSError,KeyError,TypeError) as exc:
                return [dict(state='CONFIG_INVALID',reason=str(exc))]
            server_id=policy['server_id']
            # Reports retry before new pulls. Each journal is immutable per sequence.
            for journal in self.root.glob('*/report.json'): self._send(journal.parent,server_id)
            output=[]
            for dispatch in self.fleet.pull(server_id):
                directory=self.root/dispatch['dispatch_id']; directory.mkdir(exist_ok=True)
                plan=json.loads(dispatch['plan']); plan['plan_id']=dispatch['run_id']+':'+dispatch['dispatch_id']
                plan_path=directory/'plan.json'
                if not plan_path.exists(): atomic_json(plan_path,plan)
                context=dict(global_run_id=dispatch['run_id'],dispatch_id=dispatch['dispatch_id'],mode=self.fleet.status(dispatch['run_id'])['plan']['mode'],library=dispatch['library'])
                agent=self._agent(directory,plan_path,context)
                try:
                    local=agent.status(); captured=any(a['state']=='SUCCEEDED' for a in local['attempts'])
                    failed=any(a['state'] in ('FAILED','INTERRUPTED','REJECTED') for a in local['attempts'])
                    if not captured and not failed:
                        uid=plan['tasks'][0]['device_uid']
                        if any(d['device_uid']==uid and d['state']=='QUARANTINED' for d in local['devices']):
                            self._report(directory,server_id,dispatch,'PENDING','device quarantined; manual clearance required',agent)
                            output.append(dict(dispatch_id=dispatch['dispatch_id'],state='PENDING',reason='device quarantined'))
                            continue
                        self._report(directory,server_id,dispatch,'RUNNING',agent=agent)
                        result=agent.tick(self.transport)
                    else:
                        # Recover/upload only; do not open another daily window.
                        agent.recover(); agent.upload(self.transport); result={}
                    local=agent.status()
                    if local['outbox']:
                        state='INGESTED' if all(o['state']=='ACKED' for o in local['outbox']) else 'PENDING_UPLOAD'
                    elif any(a['state']=='INTERRUPTED' for a in local['attempts']): state='INTERRUPTED'
                    elif any(a['state'] in ('FAILED','REJECTED') for a in local['attempts']): state='FAILED'
                    else: state='PENDING'
                    self._report(directory,server_id,dispatch,state,result.get('reason'),agent)
                    output.append(dict(dispatch_id=dispatch['dispatch_id'],state=state))
                finally: agent.close()
            return output
