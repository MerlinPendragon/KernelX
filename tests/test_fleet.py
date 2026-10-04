"""CPU simulation: two independently scheduled Agents, never NPU evidence."""
import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from kernelx.agent.fleet import Fleet, FleetWorker, LIBRARIES
from kernelx.agent.storage import Center, atomic_json
from kernelx.cann_adapter import CannAddAdapter
from kernelx.protocol import digest
from test_agent import ENV, DEVICE, fixture_run


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.control=self.root/'control'
        self.fleet=Fleet(self.control); self.addCleanup(self.fleet.close)
        self.servers=[]
        for index in range(2):
            server_id=str(uuid.uuid4()); uid=str(uuid.uuid4())
            cap=dict(status='VERIFIED',soc='SIMULATED_910B1',bin='SIMULATED_BIN',cann='fixture',library_version=None,library_commit=None,preset='latency-v1',manifest_sha256=digest(CannAddAdapter().manifest),environment_sha256=digest(ENV),evidence_uri='fixture://CPU_ONLY')
            self.servers.append(dict(server_id=server_id,device_uid=uid,capabilities={'cann-add':cap}))
            policy=dict(schema_version=1,server_id=server_id,reservation_id='simulation',schedule_revision=1,enabled=True,valid_from='2026-01-01T00:00:00Z',valid_until='2030-01-01T00:00:00Z',timezone='UTC',schedules=[dict(id='sim',weekdays=list(range(1,8)),start='00:00',end='23:59')],skip_dates=[],allowed_devices=[dict(device_uid=uid,logical_id=5)],max_workers=1,cleanup_reserve_seconds=3,task_timeout_seconds=10,spool_max_bytes=64000000,spool_high_watermark=.8)
            atomic_json(self.root/('policy%d.json'%index),policy)
        self.request=dict(submission_id='once',libraries='all',scope='core',servers=self.servers,mode='paired',valid_from='2026-01-01T00:00:00Z',valid_until='2030-01-01T00:00:00Z',warmup=20,repeats=10,pilot_upper_seconds=1,estimated_output_bytes=1024)

    def runner(self,root,**kwargs):
        result=fixture_run(root,kwargs,time.time())
        # Independent synthetic machine identities; all protocol references remain
        # consistent and samples keep distinct observation IDs.
        for path in Path(root).rglob('*.json'):
            path.write_text(path.read_text().replace(ENV['server_id'],kwargs['server_id']).replace(DEVICE,kwargs['expected_device_uid']))
        artifacts=json.loads((Path(root)/'artifacts.json').read_text())
        profile=json.loads((Path(root)/'profile.json').read_text())
        for artifact in artifacts:
            path=Path(root)/artifact['uri'].split('artifact://'+profile['profile_id']+'/',1)[1]
            artifact.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)
        (Path(root)/'artifacts.json').write_text(json.dumps(artifacts))
        return result

    def worker(self,index,runner=None,transport=None):
        return FleetWorker(self.root/('worker%d'%index),self.root/('policy%d.json'%index),self.fleet,transport,dict(runner=runner or self.runner))

    def submit_one_server(self,identity):
        request=copy.deepcopy(self.request)
        request.update(submission_id=identity,libraries=['cann-opp'],servers=[self.servers[0]])
        return self.fleet.submit(request)

    def test_catalog_full_scope_enumerates_filters_and_executes_unified_worker(self):
        from kernelx.libraries import CatalogAdapter,FrozenPerformanceAdapter
        adapter=CatalogAdapter('sgl-kernel-npu');request=copy.deepcopy(self.request)
        request.update(submission_id='CPU-catalog-full',libraries=['sgl-kernel-npu'],scope='full',servers=[request['servers'][0]])
        request['servers'][0]['capabilities']['sgl-kernel-npu']=dict(status='VERIFIED',manifest_sha256=digest(adapter.manifest('full')),tuple_sha256='CPU_FIXTURE',evidence=dict(bundle_sha256='CPU_FIXTURE'))
        run=self.fleet.submit(request);dispatches=self.fleet.status(run)['dispatches'];self.assertEqual(len(dispatches),3)
        self.assertEqual({json.loads(d['plan'])['tasks'][0]['case_index'] for d in dispatches},{0,1,2})
        called=[]
        def failure(root,**kwargs):
            called.append((kwargs['adapter_library'],kwargs['case_index']))
            return dict(valid=False,device_release='UNKNOWN',reason='CPU-only rejected launch fixture')
        self.worker(0,runner=failure).tick()
        self.assertEqual(set(called),{('sgl-kernel-npu',i) for i in range(3)})
        self.assertEqual(self.fleet.status(run)['counts']['FAILED'],3)
        request['submission_id']='unsupported-catalog';request['servers'][0]['capabilities']['sgl-kernel-npu']['status']='UNSUPPORTED'
        blocked=self.fleet.submit(request);self.assertEqual(self.fleet.status(blocked)['counts']['UNSUPPORTED'],1)
        self.worker(0,runner=failure).tick();self.assertEqual(len(called),3)

    def test_quarantine_blocks_new_run_and_retry_until_manual_clear(self):
        from unittest.mock import patch
        calls=[]
        def residual(root,**kwargs):
            calls.append(1); Path(root).mkdir()
            atomic_json(Path(root)/'execution.json',dict(process_release='RESIDUAL'))
            return dict(valid=False,device_release='UNKNOWN',reason='simulated residual')
        first=self.submit_one_server('residual'); self.worker(0,residual).tick()
        failed=next(d for d in self.fleet.status(first)['dispatches'] if d['state']=='FAILED')
        second=self.submit_one_server('new-run')
        retry=self.fleet.retry(failed['dispatch_id'],'explicit-retry')
        # Recreate the worker: both shared ledger and pending work survive restart.
        pending=self.worker(0,residual).tick()
        self.assertEqual(calls,[1]); self.assertTrue(all(d['state']=='PENDING' for d in pending))
        self.assertIn(retry,{d['dispatch_id'] for d in pending})
        def release(*args): return dict(status='RELEASED',evidence=dict(stdout='No process in device.'))
        worker=self.worker(0); worker.options['release']=release
        with patch('kernelx.agent.engine.probe',return_value=dict(devices=[])):
            with self.assertRaises(ValueError): worker.clear_device(self.servers[0]['device_uid'])
        worker.options['release']=lambda *args:dict(status='UNKNOWN',evidence=dict(stdout='query failed'))
        with patch('kernelx.agent.engine.probe',return_value=dict(devices=[dict(device_uid=self.servers[0]['device_uid'],logical_id=5)])):
            with self.assertRaises(ValueError): worker.clear_device(self.servers[0]['device_uid'])
        self.worker(0,residual).tick(); self.assertEqual(calls,[1])
        worker.options['release']=release
        with patch('kernelx.agent.engine.probe',return_value=dict(devices=[dict(device_uid=self.servers[0]['device_uid'],logical_id=5)])):
            worker.clear_device(self.servers[0]['device_uid'])
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.worker(0,transport=center.import_bundle).tick()
        self.assertEqual(self.fleet.status(second)['counts']['INGESTED'],1)
        self.assertEqual(next(d for d in self.fleet.status(first)['dispatches'] if d['dispatch_id']==retry)['state'],'INGESTED')
        self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],60)

    def test_restart_recovery_quarantines_device_before_new_dispatch(self):
        from test_agent import Crash
        from kernelx.agent.storage import database
        calls=[]
        def crashed(root,**kwargs):
            calls.append(1); Path(root).mkdir()
            atomic_json(Path(root)/'benchmark-ownership.json',dict(state='RESIDUAL'))
            raise Crash('worker interrupted after launch')
        self.submit_one_server('interrupted')
        worker=self.worker(0,crashed)
        worker.options['release']=lambda *args:dict(status='RELEASED',evidence=dict(stdout='No process in device.'))
        with self.assertRaises(Crash): worker.tick()
        run=self.submit_one_server('after-restart')
        worker.tick()
        self.assertEqual(calls,[1]); self.assertEqual(self.fleet.status(run)['counts']['PENDING'],1)
        db=database(self.root/'worker0/server-resources/resources.db')
        try: self.assertEqual(db.execute('SELECT state FROM devices').fetchone()[0],'QUARANTINED')
        finally: db.close()

    def test_shared_spool_blocks_second_run_after_retained_failure(self):
        policy_path=self.root/'policy0.json'; policy=json.loads(policy_path.read_text())
        policy.update(spool_max_bytes=1000,spool_high_watermark=.8); atomic_json(policy_path,policy)
        self.request['estimated_output_bytes']=600
        calls=[]
        def failed(root,**kwargs):
            calls.append(1); Path(root).mkdir(); (Path(root)/'failure.bin').write_bytes(b'x'*650)
            return dict(valid=False,device_release='RELEASED',reason='retained CPU evidence')
        self.submit_one_server('first'); self.submit_one_server('second')
        result=self.worker(0,failed).tick()
        self.assertEqual(calls,[1]); self.assertEqual([d['state'] for d in result],['FAILED','PENDING'])
        self.worker(0,failed).tick(); self.assertEqual(calls,[1])

    def test_shared_cache_resumes_after_ack_and_upload_archive_cleanup(self):
        calls=[]
        def runner(root,**kwargs): calls.append(1); return self.runner(root,**kwargs)
        self.submit_one_server('captured'); self.worker(0,runner).tick()
        state=self.root/'worker0'
        from kernelx.agent.storage import database
        db=database(next(state.glob('*/agent/state.db')))
        bundle=db.execute('SELECT * FROM outbox').fetchone(); db.close()
        spool=Path(bundle['path']).parent.parent
        archive=spool/'uploads'/(bundle['bundle_id']+'.tar')
        archive.parent.mkdir(); archive.write_bytes(b'x'*65536)
        # Enough capacity for the retained case plus the next estimate, but the
        # upload archive takes the aggregate over the server watermark.
        used=sum(p.stat().st_size for p in spool.rglob('*') if p.is_file())
        policy_path=self.root/'policy0.json'; policy=json.loads(policy_path.read_text())
        policy['spool_max_bytes']=(used-65536+1024+100)/.8; atomic_json(policy_path,policy)
        run=self.submit_one_server('waiting')
        self.assertEqual(self.worker(0,runner).tick()[-1]['state'],'PENDING'); self.assertEqual(calls,[1])
        center=Center(self.root/'center'); self.addCleanup(center.close)
        result=self.worker(0,runner,center.import_bundle).tick()
        self.assertFalse(archive.exists()); self.assertFalse(Path(bundle['path']).exists())
        self.assertEqual(calls,[1,1]); self.assertEqual(self.fleet.status(run)['counts']['INGESTED'],1)

    def test_existing_dispatch_quarantine_migrates_once_and_clear_is_durable(self):
        from kernelx.agent import Agent
        from unittest.mock import patch
        run=self.submit_one_server('legacy'); dispatch=self.fleet.pull(self.servers[0]['server_id'])[0]
        directory=self.root/'worker0'/dispatch['dispatch_id']; directory.mkdir(parents=True)
        atomic_json(directory/'plan.json',json.loads(dispatch['plan']))
        # Simulate a pre-fix persisted, dispatch-local quarantine.
        agent=Agent(directory/'agent',self.root/'policy0.json',directory/'plan.json')
        agent.quarantine(self.servers[0]['device_uid'],'legacy unknown release'); agent.close()
        calls=[]
        def runner(root,**kwargs): calls.append(1); return self.runner(root,**kwargs)
        self.worker(0,runner).tick(); self.assertEqual(calls,[])
        def release(*args): return dict(status='RELEASED',evidence=dict(stdout='No process in device.'))
        worker=self.worker(0,runner); worker.options['release']=release
        with patch('kernelx.agent.engine.probe',return_value=dict(devices=[dict(device_uid=self.servers[0]['device_uid'],logical_id=5)])):
            worker.clear_device(self.servers[0]['device_uid'])
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.worker(0,runner,center.import_bundle).tick()
        self.assertEqual(calls,[1]); self.assertEqual(self.fleet.status(run)['counts']['INGESTED'],1)

    def test_submit_is_immutable_and_gaps_are_never_success(self):
        run=self.fleet.submit(self.request)
        self.assertEqual(run,self.fleet.submit(self.request))
        changed=copy.deepcopy(self.request); changed['warmup']=21
        with self.assertRaises(ValueError): self.fleet.submit(changed)
        status=self.fleet.status(run)
        self.assertEqual(set(status['libraries']),set(LIBRARIES))
        self.assertEqual(status['counts']['PENDING'],2)
        self.assertEqual(status['counts']['ADAPTER_UNCONFIGURED'],5)
        self.assertEqual(status['state'],'PENDING')

    def test_shards_vs_intentional_paired_repeats(self):
        self.request['mode']='shard'; run=self.fleet.submit(self.request)
        self.assertEqual(sum(len(self.fleet.pull(s['server_id'])) for s in self.servers),1)
        self.request.update(mode='paired',submission_id='paired'); run=self.fleet.submit(self.request)
        self.assertEqual(sum(d['state']=='PENDING' for d in self.fleet.status(run)['dispatches']),2)

    def test_capabilities_require_full_tuple_and_block_unsupported(self):
        self.servers[0]['capabilities']['cann-add'].pop('evidence_uri')
        self.servers[1]['capabilities']['cann-add'].update(status='UNSUPPORTED',reason='SoC/BIN mismatch')
        run=self.fleet.submit(self.request); status=self.fleet.status(run)
        self.assertEqual(status['counts']['UNVERIFIED'],9)
        self.assertEqual(status['counts']['UNSUPPORTED'],1)
        self.assertFalse(any(self.fleet.pull(s['server_id']) for s in self.servers))
        self.assertEqual(status['state'],'UNSUPPORTED')

    def test_two_agents_overlap_and_deduplicate_upload(self):
        run=self.fleet.submit(self.request); barrier=threading.Barrier(2)
        intervals={}; errors=[]
        def execute(index):
            control=Fleet(self.control); center=Center(self.root/'center')
            def runner(root,**kwargs):
                barrier.wait(timeout=10); started=time.monotonic()
                time.sleep(.15); result=self.runner(root,**kwargs)
                intervals[index]=(started,time.monotonic()); return result
            worker=FleetWorker(self.root/('worker%d'%index),self.root/('policy%d.json'%index),control,center.import_bundle,dict(runner=runner))
            try:
                worker.tick(); worker.tick()
            except BaseException as exc: errors.append(exc)
            finally: center.close(); control.close()
        # Initialize WAL schema before concurrent connections.
        center=Center(self.root/'center'); center.close()
        threads=[threading.Thread(target=execute,args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads)); self.assertEqual(errors,[])
        self.assertLess(max(v[0] for v in intervals.values()),min(v[1] for v in intervals.values()))
        status=self.fleet.status(run); self.assertEqual(status['counts']['INGESTED'],2)
        self.assertEqual(status['state'],'PARTIAL') # Whole-library gaps stay visible.
        self.parallel_evidence=dict(evidence_type='CPU_SIMULATION_NOT_REAL_MULTI_SERVER',servers=[s['server_id'] for s in self.servers],monotonic_intervals={str(k):list(v) for k,v in intervals.items()},overlap_seconds=min(v[1] for v in intervals.values())-max(v[0] for v in intervals.values()),counts=status['counts'],observations=60,duplicate_upload_observations=60)
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],60)
        self.assertEqual(center.db.execute('SELECT count(*) FROM fleet_links').fetchone()[0],2)
        self.assertEqual(center.entry()['fleet_links'][0]['global_run_id'],run)
        for bundle in (center.root/'bundles').iterdir(): center.import_bundle(bundle)
        self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],60)

    def test_failed_server_does_not_block_other_and_explicit_retry(self):
        run=self.fleet.submit(self.request)
        def failed(root,**kwargs): return dict(valid=False,reason='simulated server failure',device_release='RELEASED')
        self.worker(0,failed).tick()
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.worker(1,transport=center.import_bundle).tick()
        status=self.fleet.status(run); self.assertEqual(status['counts']['FAILED'],1); self.assertEqual(status['counts']['INGESTED'],1)
        old=next(d for d in status['dispatches'] if d['state']=='FAILED')
        retry=self.fleet.retry(old['dispatch_id'],'operator-retry')
        self.assertEqual(retry,self.fleet.retry(old['dispatch_id'],'operator-retry'))
        self.worker(0,transport=center.import_bundle).tick()
        self.assertEqual(self.fleet.status(run)['counts']['INGESTED'],2)
        self.assertEqual(next(d for d in self.fleet.status(run)['dispatches'] if d['dispatch_id']==old['dispatch_id'])['state'],'FAILED')

    def test_restart_upload_retry_never_remeasures_capture(self):
        self.fleet.submit(self.request); calls=[]
        def runner(root,**kwargs): calls.append(1); return self.runner(root,**kwargs)
        def offline(root): raise OSError('network unavailable')
        self.worker(0,runner,offline).tick()
        center=Center(self.root/'center'); self.addCleanup(center.close)
        # Advance Agent retry clock after process/worker restart.
        worker=self.worker(0,runner,center.import_bundle)
        worker.options['clock']=lambda:time.time()+6
        worker.tick(); worker.tick()
        self.assertEqual(calls,[1]); self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],30)

    def test_invalid_policy_still_uploads_completed_dispatch(self):
        self.fleet.submit(self.request); self.worker(0).tick()
        (self.root/'policy0.json').write_text('{broken')
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.assertEqual(self.worker(0,transport=center.import_bundle).tick()[0]['state'],'CONFIG_INVALID')
        self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],30)

    def test_unfinished_dispatch_continues_next_window_without_daily_repeat(self):
        from kernelx.agent.policy import timestamp
        self.fleet.submit(self.request); calls=[]
        now=[timestamp('2026-10-04T23:58:58Z')]
        def runner(root,**kwargs):
            calls.append(1); result=fixture_run(root,kwargs,now[0])
            # This test uses the original fixture identity rather than two servers.
            for path in Path(root).rglob('*.json'):
                path.write_text(path.read_text().replace(ENV['server_id'],kwargs['server_id']).replace(DEVICE,kwargs['expected_device_uid']))
            artifacts=json.loads((Path(root)/'artifacts.json').read_text()); profile=json.loads((Path(root)/'profile.json').read_text())
            for artifact in artifacts:
                path=Path(root)/artifact['uri'].split('artifact://'+profile['profile_id']+'/',1)[1]
                artifact.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)
            (Path(root)/'artifacts.json').write_text(json.dumps(artifacts)); return result
        worker=self.worker(0,runner); worker.options['clock']=lambda:now[0]
        self.assertEqual(worker.tick()[0]['state'],'PENDING'); self.assertEqual(calls,[])
        now[0]=timestamp('2026-10-05T00:10:00Z')
        center=Center(self.root/'center'); self.addCleanup(center.close); worker.transport=center.import_bundle
        self.assertEqual(worker.tick()[0]['state'],'INGESTED')
        now[0]+=86400; self.assertEqual(worker.tick(),[]); self.assertEqual(calls,[1])

    def test_report_binding_sequence_and_terminal_replay(self):
        self.fleet.submit(self.request); first=self.fleet.pull(self.servers[0]['server_id'])[0]
        payload=dict(state='FAILED',reason='fixture')
        with self.assertRaises(ValueError): self.fleet.report(self.servers[1]['server_id'],first['dispatch_id'],1,payload)
        self.fleet.report(self.servers[0]['server_id'],first['dispatch_id'],1,payload)
        self.fleet.report(self.servers[0]['server_id'],first['dispatch_id'],1,payload)
        with self.assertRaises(ValueError): self.fleet.report(self.servers[0]['server_id'],first['dispatch_id'],1,dict(state='PENDING'))
        with self.assertRaises(ValueError): self.fleet.report(self.servers[0]['server_id'],first['dispatch_id'],2,dict(state='RUNNING'))

    def test_control_ack_loss_replays_journal_after_restart(self):
        self.fleet.submit(self.request); original=self.fleet.report; lost=[False]
        def report(*args):
            original(*args)
            if args[-1]['state']=='PENDING_UPLOAD' and not lost[0]:
                lost[0]=True; raise OSError('report ACK lost')
        self.fleet.report=report; calls=[]
        def runner(root,**kwargs): calls.append(1); return self.runner(root,**kwargs)
        with self.assertRaises(OSError): self.worker(0,runner).tick()
        self.fleet.report=original
        center=Center(self.root/'center'); self.addCleanup(center.close)
        self.worker(0,runner,center.import_bundle).tick()
        self.assertEqual(calls,[1]); self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],30)

if __name__=='__main__': unittest.main()
