import copy
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kernelx.bootstrap import Bootstrap, https
from kernelx.release import build_release, compatibility, verify_manifest, check_installed, sha
from kernelx.agent.storage import atomic_json
from kernelx.supervisor import run_owned, group_members, terminate_recorded
from test_agent import ENV, DEVICE, Crash


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys=tempfile.TemporaryDirectory(); root=Path(cls.keys.name)
        cls.key=root/'private.pem'; cls.pub=root/'trusted.pem'
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',str(cls.key)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['openssl','pkey','-in',str(cls.key),'-pubout','-out',str(cls.pub)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    @classmethod
    def tearDownClass(cls): cls.keys.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.environment=copy.deepcopy(ENV); self.environment['host']['architecture']['value']=platform.machine()
        for name in ('opapi','nnopbase','ascendcl','msprofiler'):
            self.environment['extensions']['fingerprints']['cann-'+name]=dict(sha256='1'*64,path='SIMULATED_CPU_FIXTURE')
        self.group=compatibility(self.environment,DEVICE); self.repo=self.root/'repository'
        self.config=dict(schema_version=1,root=str(self.root/'installed'),trusted_key=str(self.pub),server_id=ENV['server_id'],device_uid=DEVICE,policy=str(self.root/'policy.json'),plan=str(self.root/'plan.json'),center_dir=str(self.root/'center'),source=str(self.repo),smoke=False,run_agent=False,max_package_bytes=10**8,disk_reserve_bytes=10**6)
        from test_agent import AgentTests
        template=AgentTests(); template.setUp()
        try:
            atomic_json(self.root/'policy.json',template.policy); atomic_json(self.root/'plan.json',template.plan)
        finally: template.doCleanups()
        self.source=Path(__file__).resolve().parents[1]

    def release(self,commit='a'*40,group=None):
        path=build_release(self.source,self.repo,commit,group or self.group,self.key,1 if commit=='a'*40 else 2)
        atomic_json(self.repo/'latest.json',dict(release_id=path.name)); return path

    def bootstrap(self,**kwargs):
        boot=Bootstrap(self.config,host_probe=lambda _:self.environment,**kwargs); self.addCleanup(boot.close); return boot


    def test_candidate_case_manifest_upgrade_with_stable_bootstrap(self):
        self.release(); boot=self.bootstrap(); boot.tick()
        source=self.root/'new-case'
        shutil.copytree(self.source/'kernelx',source/'kernelx',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        from kernelx.protocol import case_key,digest
        case_path=source/'kernelx/manifests/cann_add.json'
        manifest=json.loads(case_path.read_text()); manifest['case']['semantic_version']='2'
        manifest['case_key']=case_key(manifest['case']); atomic_json(case_path,manifest)
        plan=json.loads(Path(self.config['plan']).read_text()); plan['manifest_sha256']=digest(manifest)
        atomic_json(Path(self.config['plan']),plan)
        # A disabled policy exercises the real new Agent's plan validation, but
        # cannot initialize NPU even if the scheduling implementation regresses.
        policy=json.loads(Path(self.config['policy']).read_text()); policy['enabled']=False
        atomic_json(Path(self.config['policy']),policy)
        candidate=build_release(source,self.repo,'b'*40,self.group,self.key,2)
        atomic_json(self.repo/'latest.json',dict(release_id=candidate.name))
        boot.config['run_agent']=True
        result=boot.tick()
        self.assertEqual(result['state'],'HEALTHY'); self.assertEqual(result['exit_code'],0)
        self.assertNotEqual(result['application']['state'],'CONFIG_INVALID')
        self.assertEqual(boot.current(),candidate.name)

    def test_cli_exit_codes_for_first_failure_wait_idle_fallback_and_app_failure(self):
        source=self.root/'cpu-cli'
        shutil.copytree(self.source/'kernelx',source/'kernelx',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        with (source/'kernelx/probe.py').open('a') as f:
            f.write('\ndef probe(*args, **kwargs):\n    return '+repr(self.environment)+'\n')
        config=self.root/'config.json'
        def cli():
            atomic_json(config,self.config)
            completed=subprocess.run([sys.executable,'-B','-m','kernelx','bootstrap-tick','--config',str(config)],cwd=source,env=dict(os.environ,PYTHONPATH=str(source)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
            return completed.returncode,json.loads(completed.stdout)
        code,result=cli(); self.assertEqual(code,1); self.assertFalse(result['runnable'])
        release=build_release(source,self.repo,'a'*40,self.group,self.key)
        atomic_json(self.repo/'latest.json',dict(release_id=release.name))
        self.config['smoke']=True
        policy=json.loads(Path(self.config['policy']).read_text()); policy['enabled']=False
        atomic_json(Path(self.config['policy']),policy)
        code,result=cli(); self.assertEqual(code,0); self.assertEqual(result['state'],'WAITING_SMOKE_WINDOW')
        self.config.update(smoke=False,run_agent=True)
        code,result=cli(); self.assertEqual(code,0); self.assertTrue(result['runnable'])
        code,result=cli(); self.assertEqual(code,0); self.assertEqual(result['state'],'CURRENT')
        candidate=build_release(source,self.repo,'b'*40,self.group,self.key,2)
        (candidate/'manifest.sig').write_bytes(b'invalid')
        atomic_json(self.repo/'latest.json',dict(release_id=candidate.name))
        code,result=cli(); self.assertEqual(code,0); self.assertEqual(result['state'],'UPDATE_REJECTED')
        Path(self.config['policy']).write_text('{broken')
        code,result=cli(); self.assertEqual(code,1); self.assertIn('application_error',result)

    @unittest.skipUnless(Path('/proc').is_dir(),'Linux nested process ownership requires /proc')
    def test_outer_timeout_recovers_independent_sigterm_ignoring_native_group(self):
        from functools import partial
        from kernelx.agent import Agent
        self.release(); boot=self.bootstrap(); boot.tick(); boot.config['run_agent']=True
        state=boot.root/'runtime/main/agent'
        agent=Agent(state,self.config['policy'],self.config['plan'],resource_root=boot.root/'runtime/server-resources',cache_root=boot.root/'runtime')
        policy=json.loads(Path(self.config['policy']).read_text()); plan=json.loads(Path(self.config['plan']).read_text())
        aid='nested'; outer=agent.spool/aid; outer.mkdir(); run=outer/'run'; run.mkdir()
        agent.close()
        ownership=run/'benchmark-ownership.json'
        child_code="import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        launcher="from kernelx.supervisor import run_owned; import sys; run_owned([sys.executable,'-c',sys.argv[3]],sys.argv[2],60,ownership_path=sys.argv[1])"
        pgid=[]
        def executor(argv,log,timeout,**kwargs):
            self.assertGreater(timeout,policy['task_timeout_seconds']+120)
            agent=Agent(state,self.config['policy'],self.config['plan'],resource_root=boot.root/'runtime/server-resources',cache_root=boot.root/'runtime')
            with agent.db:
                agent.db.execute('INSERT INTO windows VALUES(?,?,?,?,?,?,?,?,?)',('w',0,1,'RUNNING',None,None,'p',json.dumps(policy),'p'))
                agent.db.execute('INSERT INTO tasks VALUES(?,?,?,NULL)',('w',plan['tasks'][0]['task_id'],'RUNNING'))
                agent.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,'w',plan['tasks'][0]['task_id'],'RUNNING',str(run),0,None,None,'NOT_CHECKED',None))
            atomic_json(outer/'context.json',dict(attempt_id=aid,window_id='w',task=plan['tasks'][0],logical_id=5,started=0,policy=policy,plan=plan)); agent.close()
            result=run_owned([sys.executable,'-c',launcher,str(ownership),str(run/'native.log'),child_code],log,.6,grace=.1,ownership_path=kwargs['ownership_path'])
            record=json.loads(ownership.read_text()); pgid.append(record['process_group'])
            self.assertTrue(group_members(pgid[0]),'reproduce a separate live native group')
            return result
        boot.executor=executor
        clear=lambda *args:dict(status='RELEASED',evidence=dict(stdout='No process in device.'))
        try:
            with patch('kernelx.bootstrap.Agent',partial(Agent,release=clear)):
                result=boot.tick()
            self.assertEqual(result['exit_code'],1)
            self.assertEqual(group_members(pgid[0]),[])
            agent=Agent(state,self.config['policy'],self.config['plan'],resource_root=boot.root/'runtime/server-resources',cache_root=boot.root/'runtime')
            try:
                self.assertEqual(agent.status()['attempts'][0]['release_status'],'RELEASED')
                self.assertEqual(agent.status()['attempts'][0]['state'],'INTERRUPTED')
            finally: agent.close()
        finally:
            if ownership.exists(): terminate_recorded(ownership,grace=.1)

    def test_session_budget_covers_multi_task_plan(self):
        self.release(); boot=self.bootstrap(); boot.tick(); boot.config['run_agent']=True
        plan=json.loads(Path(self.config['plan']).read_text())
        plan['tasks'].append(dict(plan['tasks'][0],task_id='second'))
        atomic_json(Path(self.config['plan']),plan)
        from kernelx.agent.policy import timestamp
        boot.clock=lambda:timestamp('2026-10-04T02:10:00+08:00')
        def executor(argv,log,timeout,**kwargs):
            self.assertGreater(timeout,2*90+2*360+120)
            self.assertGreater(kwargs['grace'],2)
            Path(log).write_text(json.dumps(dict(state='WAITING_WINDOW')))
            return dict(exit_code=0,reason=None)
        boot.executor=executor; self.assertEqual(boot.tick()['exit_code'],0)

    def test_signed_install_upgrade_and_retained_old_release(self):
        first=self.release(); boot=self.bootstrap()
        self.assertEqual(boot.tick()['state'],'HEALTHY'); self.assertEqual(boot.current(),first.name)
        second=self.release('b'*40)
        self.assertEqual(boot.tick()['state'],'HEALTHY'); self.assertEqual(boot.current(),second.name)
        self.assertTrue((boot.root/'releases'/first.name).is_dir()); self.assertEqual(boot.tick()['state'],'CURRENT')
        check_installed(boot.root/'releases'/second.name,verify_manifest(second,self.pub))

    def test_invalid_policy_still_recovers_and_uploads_prior_session(self):
        from kernelx.agent import Agent
        from kernelx.agent.policy import timestamp
        from test_agent import fixture_run
        now=timestamp('2026-10-04T02:10:00+08:00')
        boot=self.bootstrap(); self.release()
        agent=Agent(boot.root/'runtime/main/agent',self.config['policy'],self.config['plan'],clock=lambda:now,
                    runner=lambda root,**kwargs:fixture_run(root,kwargs,now),resource_root=boot.root/'runtime/server-resources',cache_root=boot.root/'runtime')
        try: self.assertEqual(agent.tick()['terminal'],'COMPLETED')
        finally: agent.close()
        Path(self.config['policy']).write_text('{corrupt')
        boot.tick()
        from kernelx.agent.storage import Center
        center=Center(self.config['center_dir'])
        try: self.assertEqual(center.db.execute('SELECT count(*) FROM observations').fetchone()[0],30)
        finally: center.close()
        self.assertEqual(boot.status()['health']['upload'],dict(pending=0,acked=1))

    def test_new_release_cannot_bypass_previous_device_quarantine(self):
        from kernelx.agent import Agent
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current()
        agent=Agent(boot.root/'runtime/main/agent',self.config['policy'],self.config['plan'],resource_root=boot.root/'runtime/server-resources',cache_root=boot.root/'runtime')
        agent.quarantine(DEVICE,'prior release UNKNOWN'); agent.close()
        # Signed CPU-only candidate uses a Runner guard: even a regression must
        # never launch a real benchmark on the development/910B1 test host.
        source=self.root/'guarded-source'
        shutil.copytree(self.source/'kernelx',source/'kernelx',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        runner=source/'kernelx/runner.py'
        with runner.open('a') as output: output.write('\ndef collect(*args, **kwargs):\n    raise AssertionError("quarantine bypassed")\n')
        candidate=build_release(source,self.repo,'b'*40,self.group,self.key,2)
        atomic_json(self.repo/'latest.json',dict(release_id=candidate.name))
        policy=json.loads(Path(self.config['policy']).read_text())
        policy.update(timezone='UTC',valid_from='2000-01-01T00:00:00Z',valid_until='2099-01-01T00:00:00Z')
        policy['schedules'][0].update(start='00:00',end='23:59'); atomic_json(Path(self.config['policy']),policy)
        plan=json.loads(Path(self.config['plan']).read_text()); plan.update(valid_from=policy['valid_from'],valid_until=policy['valid_until']); atomic_json(Path(self.config['plan']),plan)
        self.config['smoke']=True; trial=self.bootstrap()
        result=trial.tick(); self.assertEqual(result['state'],'ROLLED_BACK'); self.assertEqual(result['exit_code'],1); self.assertEqual(trial.current(),previous)
        agent=Agent(trial.root/('runtime/smoke-'+candidate.name+'/agent'),self.config['policy'],self.config['plan'],resource_root=trial.root/'runtime/server-resources',cache_root=trial.root/'runtime')
        try:
            self.assertEqual(agent.status()['attempts'],[])
            self.assertEqual(agent.status()['devices'][0]['state'],'QUARANTINED')
        finally: agent.close()

    def test_update_excludes_active_session_and_freezes_plan(self):
        self.release(); boot=self.bootstrap(); boot.tick(); candidate=self.release('b'*40)
        self.config['smoke']=True
        from kernelx.agent.policy import timestamp
        def executor(argv,log,*args,**kwargs):
            if 'agent-tick' not in argv: return run_owned(argv,log,*args,**kwargs)
            frozen=Path(argv[argv.index('--plan')+1])
            original=json.loads(Path(self.config['plan']).read_text())
            original['tasks'][0]['warmup']=40; atomic_json(Path(self.config['plan']),original)
            self.assertEqual(json.loads(frozen.read_text())['tasks'][0]['warmup'],20)
            self.assertEqual(argv[argv.index('--policy')+1],self.config['policy'])
            self.assertEqual(kwargs['env']['KERNELX_RELEASE_ID'],candidate.name)
            other=self.bootstrap()
            with self.assertRaises(RuntimeError): other.tick()
            Path(log).write_text(json.dumps(dict(state='CLOSED',terminal='COMPLETED')))
            return dict(exit_code=0,reason=None)
        trial=self.bootstrap(clock=lambda:timestamp('2026-10-04T02:10:00+08:00'),executor=executor)
        self.assertEqual(trial.tick()['state'],'HEALTHY')
        self.assertTrue(trial.status()['health']['last_success']['smoke'])

    def test_rejected_update_keeps_old_application_and_separate_health(self):
        first=self.release(); boot=self.bootstrap(); boot.tick(); bad=self.release('b'*40)
        (bad/'manifest.sig').write_bytes(b'invalid'); boot.config['run_agent']=True
        state=[dict(state='WAITING_WINDOW')]
        def executor(argv,log,*args,**kwargs):
            self.assertEqual(kwargs['env']['KERNELX_RELEASE_ID'],first.name)
            Path(log).write_text(json.dumps(state[0])); return dict(exit_code=0,reason=None)
        boot.executor=executor
        result=boot.tick(); self.assertEqual(result['state'],'UPDATE_REJECTED'); self.assertEqual(result['application']['state'],'WAITING_WINDOW')
        self.assertIn('heartbeat',boot.status()['health']); self.assertIn('upload',boot.status()['health']); self.assertNotIn('last_success',boot.status()['health'])
        state[0]=dict(state='CLOSED',terminal='FAILED'); boot.tick()
        self.assertIn('last_failure',boot.status()['health']); self.assertNotIn('last_success',boot.status()['health'])

    def test_signed_feed_cannot_roll_back_accepted_sequence(self):
        first=self.release(); boot=self.bootstrap(); boot.tick()
        second=self.release('b'*40); boot.tick()
        atomic_json(self.repo/'latest.json',dict(release_id=first.name))
        self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED'); self.assertEqual(boot.current(),second.name)

    def test_cpu_exit_zero_without_health_contract_is_quarantined(self):
        self.release()
        def executor(argv,log,*args,**kwargs): Path(log).write_text('{}'); return dict(exit_code=0,reason=None)
        boot=self.bootstrap(executor=executor)
        self.assertEqual(boot.tick()['state'],'QUARANTINED'); self.assertIsNone(boot.current())

    def test_wrong_signature_preserves_healthy_release(self):
        first=self.release(); boot=self.bootstrap(); boot.tick(); candidate=self.release('b'*40)
        signature=candidate/'manifest.sig'; signature.write_bytes(b'wrong signer')
        self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED'); self.assertEqual(boot.current(),first.name)

    def test_corrupt_payload_quarantined_and_not_reinstalled(self):
        first=self.release(); boot=self.bootstrap(); boot.tick(); bad=self.release('b'*40)
        payload=bad/'payload.tar.gz'; data=bytearray(payload.read_bytes()); data[20]^=1; payload.write_bytes(data)
        self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED'); self.assertEqual(boot.current(),first.name)
        self.assertEqual(boot.tick()['state'],'QUARANTINED')

    def test_incompatible_bin_arch_or_fingerprint_rejected(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current()
        for field in ('cpu_arch','hardware_bin','fingerprints'):
            group=copy.deepcopy(self.group)
            if field=='fingerprints': group[field]['msprof']='0'*64
            else: group[field]='different'
            self.release('b'*40,group)
            self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED'); self.assertEqual(boot.current(),previous)

    def test_disk_space_failure_never_switches(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current(); self.release('b'*40)
        with patch('kernelx.bootstrap.shutil.disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)):
            self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED')
        self.assertEqual(boot.current(),previous)

    def test_cpu_smoke_failure_quarantines_preserving_current(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current(); self.release('b'*40)
        def failed(*args,**kwargs): return dict(exit_code=1,reason='import failed')
        boot.executor=failed
        self.assertEqual(boot.tick()['state'],'QUARANTINED'); self.assertEqual(boot.current(),previous)
        self.assertEqual(boot.tick()['state'],'QUARANTINED')

    def test_crash_after_stage_reuses_complete_package(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current(); candidate=self.release('b'*40)
        def fault(stage):
            if stage=='AFTER_STAGE': raise Crash()
        boot.fault_hook=fault
        with self.assertRaises(Crash): boot.tick()
        self.assertEqual(boot.current(),previous)
        restarted=self.bootstrap(); self.assertEqual(restarted.tick()['state'],'HEALTHY'); self.assertEqual(restarted.current(),candidate.name)

    def test_crash_after_switch_rolls_back_and_quarantines(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current(); candidate=self.release('b'*40)
        def fault(stage):
            if stage=='AFTER_SWITCH': raise Crash()
        boot.fault_hook=fault
        with self.assertRaises(Crash): boot.tick()
        self.assertEqual(boot.current(),candidate.name)
        restarted=self.bootstrap(); self.assertEqual(restarted.tick()['state'],'QUARANTINED'); self.assertEqual(restarted.current(),previous)

    def test_crash_after_health_commit_keeps_new_release(self):
        self.release(); boot=self.bootstrap(); boot.tick(); candidate=self.release('b'*40)
        def fault(stage):
            if stage=='AFTER_COMMIT': raise Crash()
        boot.fault_hook=fault
        with self.assertRaises(Crash): boot.tick()
        restarted=self.bootstrap(); self.assertEqual(restarted.tick()['state'],'CURRENT'); self.assertEqual(restarted.current(),candidate.name)

    def test_npu_smoke_deferred_outside_authorized_window(self):
        self.release(); self.config['smoke']=True; calls=[]
        def executor(*args,**kwargs): calls.append(args[0]); return run_owned(*args,**kwargs)
        from kernelx.agent.policy import timestamp
        boot=self.bootstrap(clock=lambda:timestamp('2026-10-04T10:00:00+08:00'),executor=executor)
        self.assertEqual(boot.tick()['state'],'WAITING_SMOKE_WINDOW'); self.assertIsNone(boot.current())
        self.assertEqual(len(calls),1); self.assertIn('release-self-test',calls[0])

    def test_npu_smoke_failure_rolls_back_and_keeps_reason(self):
        self.release(); boot=self.bootstrap(); boot.tick(); previous=boot.current(); self.release('b'*40)
        self.config['smoke']=True
        from kernelx.agent.policy import timestamp
        def executor(argv,log,*args,**kwargs):
            if 'agent-tick' in argv: Path(log).write_text(json.dumps(dict(state='CLOSED',terminal='FAILED'))); return dict(exit_code=0,reason=None)
            return run_owned(argv,log,*args,**kwargs)
        trial=self.bootstrap(clock=lambda:timestamp('2026-10-04T02:10:00+08:00'),executor=executor)
        result=trial.tick(); self.assertEqual(result['state'],'ROLLED_BACK'); self.assertEqual(result['exit_code'],1); self.assertEqual(trial.current(),previous)
        self.assertIn('last_failure',trial.status()['health'])
        self.assertEqual(trial.status()['sessions'][-1]['state'],'FAILED')
        self.assertEqual(trial.tick()['state'],'QUARANTINED')

    def test_pinned_candidate_subprocess_and_source_git_commit(self):
        release=self.release(); captured=[]
        def executor(argv,log,*args,**kwargs):
            captured.append(kwargs); return run_owned(argv,log,*args,**kwargs)
        boot=self.bootstrap(executor=executor); boot.tick()
        self.assertEqual(captured[0]['cwd'],boot.root/'releases'/release.name)
        self.assertEqual(captured[0]['env']['KERNELX_GIT_COMMIT'],'a'*40)
        self.assertEqual(captured[0]['env']['KERNELX_RELEASE_ID'],release.name)

    def test_installed_extra_file_or_drift_blocks_launch(self):
        release=self.release(); boot=self.bootstrap(); boot.tick()
        extra=boot.root/'releases'/release.name/'sitecustomize.py'; extra.write_text('raise RuntimeError("unindexed")')
        self.assertEqual(boot.tick()['state'],'UPDATE_REJECTED')

    def test_bad_signature_and_payload_do_not_execute_untrusted_code(self):
        release=self.release(); (release/'manifest.sig').write_bytes(b'untrusted'); calls=[]
        def executor(*args,**kwargs): calls.append(1); raise AssertionError('unverified code executed')
        self.assertEqual(self.bootstrap(executor=executor).tick()['state'],'UPDATE_REJECTED'); self.assertEqual(calls,[])

    def test_https_and_offline_feed_cannot_escape(self):
        for url in ('http://example.com/feed','https://user:secret@example.com/feed','file:///tmp/feed'):
            with self.assertRaises(ValueError): https(url)
        self.repo.mkdir(); atomic_json(self.repo/'latest.json',dict(release_id='../outside'))
        self.assertEqual(self.bootstrap().tick()['state'],'UPDATE_REJECTED')

if __name__=='__main__': unittest.main()
