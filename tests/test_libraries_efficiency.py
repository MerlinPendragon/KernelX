import copy,json,tempfile,time,unittest,uuid,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from kernelx.libraries import Registry,CatalogAdapter,SupportMatrix,environment_tuple
from kernelx.resource_group import ResourceGroup,GroupRankWorker
from kernelx.efficiency import union_seconds,schedule,generate
from kernelx.protocol import digest
from test_agent import ENV,DEVICE,fixture_run
from kernelx.agent.storage import atomic_json,seal

class LibraryTests(unittest.TestCase):
    def test_frozen_catalogs_validate_and_enumerate(self):
        for name,adapter in Registry().adapters.items():
            self.assertEqual(len(adapter.enumerate_cases('core')),1)
            self.assertEqual(len(adapter.enumerate_cases('full')),3)
            self.assertEqual(len({r['case_key'] for r in adapter.enumerate_cases('full')}),3)
            self.assertEqual(len(adapter.catalog['upstream']['commit']),40)
            self.assertIn('not',adapter.catalog['scope_definition'].lower())
    def test_readonly_support_does_not_claim_imported_provider(self):
        with tempfile.TemporaryDirectory() as root:
            matrix=SupportMatrix(root)
            for cap in Registry().inventory(ENV,DEVICE,matrix=matrix):
                self.assertNotEqual(cap['status'],'VERIFIED')
            self.assertEqual(len(matrix.rows()),4);matrix.close()
    def test_tuple_changes_with_manifest_bin_package_and_commit(self):
        a=CatalogAdapter('sgl-kernel-npu');env=copy.deepcopy(ENV);key=environment_tuple(env,DEVICE,a.library,digest(a.manifest()))
        for mutation in ('bin','package','manifest'):
            e=copy.deepcopy(env);manifest=digest(a.manifest())
            if mutation=='bin':next(d for d in e['devices'] if d['device_uid']==DEVICE)['hardware_bin']['value']='other'
            if mutation=='package':e['software']['packages'][0]['version']['value']='other';e['software']['packages'][0]['version']['status']='KNOWN'
            if mutation=='manifest':manifest='other'
            self.assertNotEqual(digest(key),digest(environment_tuple(e,DEVICE,a.library,manifest)))
    def test_verified_requires_actual_profile_and_provider(self):
        with tempfile.TemporaryDirectory() as root:
            matrix=SupportMatrix(root)
            with self.assertRaises(ValueError):matrix.record({'library':'sgl-kernel-npu'},'VERIFIED','imported',{'environment_id':'only'})
            matrix.close()

class EfficiencyTests(unittest.TestCase):
    def test_parallel_union_and_unknown_are_not_resource_sum(self):
        self.assertEqual(union_seconds([(0,10),(5,15),(16,20)]),19)
        self.assertIsNone(union_seconds([(0,None)]))
        with self.assertRaises(ValueError):union_seconds([(10,0)])
    def test_anchor_and_pairs_fit_budget_without_extension(self):
        tasks=[dict(case_key='z',anchor=True,upper_seconds=20),dict(case_key='a',pair_id='p',upper_seconds=30),dict(case_key='b',pair_id='p',upper_seconds=30),dict(case_key='unknown',upper_seconds=None)]
        result=schedule(tasks,100,10,.8)
        self.assertEqual([t['case_key'] for t in result['tasks']],['z','a','b'])
        self.assertFalse(result['automatic_reservation_change'])
        self.assertEqual([t['case_key'] for t in schedule(tasks,70,10,1)['tasks']],['z'])
    def test_three_windows_same_day_are_not_crossday_or_new_cases(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);bundles=[]
            for i in range(3):
                run=root/str(i);fixture_run(run);session=json.loads((run/'session.json').read_text());session['window_id']='window'+str(i);(run/'session.json').write_text(json.dumps(session));atomic_json(run/'cost.json',dict(wall_start=1780000000+i*20,wall_end=1780000010+i*20,total_wall_seconds=10,cache_state='MISS' if i==0 else 'HIT',cache_key='one',phases=[],cpu_seconds=None));seal(run);bundles.append(run)
            inventory=Registry().inventory(ENV,DEVICE)
            generate(bundles,inventory,root/'report');report=json.loads((root/'report/report.json').read_text())
            self.assertEqual(report['stability'][0]['independent_windows'],3)
            self.assertEqual(report['stability'][0]['independent_dates'],1)
            self.assertEqual(report['stability'][0]['cross_day'],'PENDING')
            self.assertIsNone(report['resources']['cpu_seconds'])
            self.assertTrue(all(c['conservative_effective_repeat_capacity'] is None for c in report['capacity'] if c['library']!='cann-opp'))

class GroupTests(unittest.TestCase):
    def configuration(self,root):
        start=time.time()-1;end=start+120
        from datetime import datetime,timezone
        iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
        ranks=[];policies=[]
        for rank in range(2):
            server=str(uuid.uuid4());uid=str(uuid.uuid4());ranks.append(dict(rank=rank,server_id=server,device_uid=uid,logical_id=5,environment_tuple_sha256='CPU_FIXTURE'))
            policy=dict(schema_version=1,server_id=server,reservation_id='CPU_FIXTURE',schedule_revision=1,enabled=True,valid_from=iso(start),valid_until=iso(end),timezone='UTC',schedules=[dict(id='test',weekdays=list(range(1,8)),start='00:00',end='23:59')],skip_dates=[],allowed_devices=[dict(device_uid=uid,logical_id=5)],max_workers=1,cleanup_reserve_seconds=3,task_timeout_seconds=10,spool_max_bytes=64000000,spool_high_watermark=.8)
            policies.append(policy);atomic_json(root/('policy%d.json'%rank),policy)
        spec=dict(mode='communication-group',library='deepep-ascend',valid_from=iso(start),valid_until=iso(end),pilot_upper_seconds=10,warmup=20,repeats=10,ranks=ranks,evidence_type='CPU_FAULT_INJECTION')
        return spec,policies
    def test_rank_failure_cancels_other_owned_processes_and_releases_leases(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,policies=self.configuration(root);control=ResourceGroup(root/'control');group=control.submit(spec,policies)
            commands=[[sys.executable,'-c','import time;time.sleep(.3);raise SystemExit(1)'],[sys.executable,'-c','import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);time.sleep(30)']]
            workers=[GroupRankWorker(root/'control',root/('rank%d'%i),i,root/('policy%d.json'%i),commands[i],lambda:'RELEASED') for i in range(2)]
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda w:w.run(group),workers))
            state=control.status(group);self.assertEqual(state['state'],'FAILED');self.assertTrue(all(r['release']=='RELEASED' for r in state['ranks']));self.assertEqual(control.db.execute('SELECT count(*) FROM leases').fetchone()[0],0)
            self.assertLess(time.time(),__import__('datetime').datetime.fromisoformat(spec['valid_until']).timestamp());control.close()
    def test_live_reservation_revocation_cancels_all_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,policies=self.configuration(root);control=ResourceGroup(root/'control');group=control.submit(spec,policies)
            command=[sys.executable,'-c','import time;time.sleep(30)']
            workers=[GroupRankWorker(root/'control',root/('rank%d'%i),i,root/('policy%d.json'%i),command,lambda:'RELEASED') for i in range(2)]
            with ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(w.run,group) for w in workers]
                deadline=time.time()+5
                while control.status(group)['state']!='RUNNING' and time.time()<deadline:time.sleep(.01)
                self.assertEqual(control.status(group)['state'],'RUNNING')
                policies[1]['enabled']=False;atomic_json(root/'policy1.json',policies[1])
                for future in futures:future.result(timeout=5)
            self.assertEqual(control.status(group)['state'],'FAILED')
            for i in range(2):
                path=root/('rank%d'%i)/'rank-result.json'
                if path.exists():self.assertEqual(json.loads(path.read_text())['execution']['process_release'],'RELEASED')
            control.close()

    def test_owned_timeout_propagates_and_all_ranks_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,policies=self.configuration(root)
            from datetime import datetime,timezone
            spec['valid_until']=datetime.fromtimestamp(time.time()+4,timezone.utc).isoformat();spec['pilot_upper_seconds']=.1
            control=ResourceGroup(root/'control');group=control.submit(spec,policies)
            command=[sys.executable,'-c','import time;time.sleep(30)']
            workers=[GroupRankWorker(root/'control',root/('rank%d'%i),i,root/('policy%d.json'%i),command,lambda:'RELEASED') for i in range(2)]
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda w:w.run(group),workers))
            self.assertEqual(control.status(group)['state'],'FAILED');self.assertTrue(all(r['release']=='RELEASED' for r in control.status(group)['ranks']));control.close()

    def test_partial_readiness_expiry_keeps_leases_until_every_rank_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,policies=self.configuration(root);control=ResourceGroup(root/'control');group=control.submit(spec,policies,ready_timeout=.03)
            control.ready(group,0,dict(device_lock_held=True,environment_verified=True));time.sleep(.04)
            self.assertEqual(control.poll(group)['state'],'CANCELLING')
            control.finish(group,0,'CANCELLED','RELEASED',{})
            self.assertEqual(control.status(group)['state'],'CANCELLING')
            control.finish(group,1,'CANCELLED','RELEASED',{})
            self.assertEqual(control.status(group)['state'],'FAILED');control.close()
    def test_reservation_revocation_and_partial_profile_cannot_succeed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spec,policies=self.configuration(root);control=ResourceGroup(root/'control');group=control.submit(spec,policies)
            for i in range(2):control.ready(group,i,dict(device_lock_held=True,environment_verified=True))
            with self.assertRaises(ValueError):control.finish(group,0,'SUCCEEDED','RELEASED',dict(complete_profile=False,durable_artifacts=True))
            policies[1]['enabled']=False
            self.assertEqual(control.poll(group,policies)['state'],'CANCELLING');control.close()

class CacheTests(unittest.TestCase):
    def test_roundtrip_hit_header_invalidation_and_artifact_drift(self):
        from types import SimpleNamespace
        from kernelx.build_cache import cached_build,native_key
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);home=root/'toolkit';(home/'include').mkdir(parents=True);header=home/'include/transitive.h';header.write_text('v1')
            base=root/'source';(base/'native').mkdir(parents=True);(base/'native/cann_add.cpp').write_text('source')
            adapter=SimpleNamespace(home=home,base=base,manifest={'case':'frozen'});calls=[]
            def compile(output):
                calls.append(1);binary=output/'cann_add';binary.write_bytes(b'compiled');atomic_json(output/'build.json',dict(elapsed_seconds=1));return binary
            one=root/'one';one.mkdir();two=root/'two';two.mkdir();three=root/'three';three.mkdir()
            cached_build(adapter,one,ENV,root/'cache',compile)
            # Reading persisted JSON changes tuples to lists; binding must already
            # be canonical JSON types so a new process can validate a cache hit.
            cached_build(adapter,two,json.loads(json.dumps(ENV)),root/'cache',compile)
            self.assertEqual(len(calls),1);self.assertEqual(json.loads((two/'build.json').read_text())['cache_state'],'HIT')
            key,_=native_key(adapter,ENV);(root/'cache'/key/'cann_add').chmod(0o600);(root/'cache'/key/'cann_add').write_bytes(b'drift')
            with self.assertRaisesRegex(ValueError,'drift'):cached_build(adapter,three,ENV,root/'cache',compile)
            header.write_text('v2');cached_build(adapter,three,ENV,root/'cache',compile);self.assertEqual(len(calls),2)

class ActualPilotEvidenceTests(unittest.TestCase):
    def test_sealed_real_profiles_database_and_independent_windows(self):
        import hashlib,tarfile
        from kernelx.agent.storage import Center,verify_bundle
        from kernelx.profile_parser import parse_add
        from pathlib import PurePosixPath
        fixture=Path(__file__).parent/'fixtures/libraries_910b1';acceptance=json.loads((fixture/'acceptance.json').read_text());archive=fixture/'acceptance-evidence.tar.gz'
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),acceptance['archive_sha256'])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with tarfile.open(archive) as source:
                for member in source.getmembers():
                    path=PurePosixPath(member.name)
                    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):raise ValueError('unsafe fixture member')
                source.extractall(root)
            center=Center(root/'center')
            try:self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],90)
            finally:center.close()
            bundles=list((root/'center/bundles').iterdir());self.assertEqual(len(bundles),3);windows=[];dates=[];cache_keys=set()
            for bundle in bundles:
                verify_bundle(bundle)
                exported=next(bundle.glob('raw/PROF_*/mindstudio_profiler_output'))
                parsed=parse_add(next(exported.glob('op_summary_*.csv')),next(exported.glob('msprof_[0-9]*.json')),next(exported.glob('msprof_tx_*.json')),bundle/'sidecar.jsonl',5,10,20)
                self.assertTrue(parsed['valid'],parsed['reasons']);self.assertEqual(parsed['actual_task_count'],10)
                result=json.loads((bundle/'result.json').read_text());self.assertTrue(result['released_by_hard_cutoff']);self.assertEqual(result['device_release'],'RELEASED')
                session=json.loads((bundle/'session.json').read_text());windows.append(session['window_id']);dates.append(session['started_at'][:10]);cache_keys.add(result['cost']['cache_key'])
            self.assertEqual(len(set(windows)),3);self.assertEqual(len(set(dates)),1);self.assertEqual(len(cache_keys),1)
