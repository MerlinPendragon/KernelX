import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kernelx.agent import Agent, Center
from kernelx.agent.policy import read_policy, windows, timestamp
from kernelx.agent.storage import atomic_json, complete_case, seal, verify_bundle
from kernelx.agent.transport import HTTPTransport
from kernelx.cann_adapter import CannAddAdapter
from kernelx.protocol import digest
from kernelx.supervisor import terminate_recorded, run_owned, process_identity

FIXTURE=Path(__file__).parent/'fixtures/cann_add_warmup20'
ENV=json.loads((FIXTURE/'environment.json').read_text())
DEVICE=ENV['devices'][5]['device_uid']


class Crash(BaseException): pass


def fixture_run(root,kwargs=None,clock=None):
    """Controlled CPU-only case: real export fixture, synthetic raw archive."""
    root=Path(root); root.mkdir(parents=True)
    origin=json.loads((FIXTURE/'fixture-origin.json').read_text())['source_to_fixture_paths']
    for original,source in origin.items():
        target=root/original; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(FIXTURE/source,target)
    # Each remeasurement receives new protocol identities. Server/device stay fixed.
    replacements={}
    for path in root.rglob('*.json'):
        try: data=json.loads(path.read_text())
        except ValueError: continue
        def visit(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key.endswith('_id') and key not in ('server_id','authorization_id','window_id','evidence_id') and isinstance(item,str):
                        try: uuid.UUID(item)
                        except ValueError: pass
                        else: replacements[item]=str(uuid.uuid4())
                    visit(item)
            elif isinstance(value,list):
                for item in value: visit(item)
        visit(data)
    for path in root.rglob('*.json'):
        text=path.read_text()
        for old,new in replacements.items(): text=text.replace(old,new)
        path.write_text(text)
    if kwargs:
        def rewrite(name,changes):
            path=root/(name+'.json'); data=json.loads(path.read_text()); data.update(changes); path.write_text(json.dumps(data))
        iso=datetime.fromtimestamp(clock,timezone.utc).isoformat().replace('+00:00','Z')
        rewrite('authorization',dict(authorization_id=kwargs['authorization_id'],device_id=kwargs['device'],device_uid=kwargs['expected_device_uid'],window_start=kwargs['window_start'],window_end=kwargs['window_end']))
        rewrite('session',dict(window_id=kwargs['authorization_id'],started_at=iso,ended_at=iso))
        rewrite('attempt',dict(started_at=iso,ended_at=iso,released_at=iso))
        rewrite('plan',dict(valid_from=kwargs['window_start'],valid_until=kwargs['window_end']))
    (root/'raw-prof.tar.gz').write_bytes(b'synthetic CPU fixture archive')
    artifacts=json.loads((root/'artifacts.json').read_text())
    profile=json.loads((root/'profile.json').read_text())
    for row in artifacts:
        path=root/row['uri'].split('artifact://'+profile['profile_id']+'/',1)[1]
        row['sha256']=hashlib.sha256(path.read_bytes()).hexdigest(); row['bytes']=path.stat().st_size
    (root/'artifacts.json').write_text(json.dumps(artifacts))
    return json.loads((root/'result.json').read_text())


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.now=timestamp('2026-10-04T02:10:00+08:00'); self.calls=[]
        self.policy=dict(schema_version=1,server_id=ENV['server_id'],reservation_id='human-fixture',schedule_revision=1,enabled=True,
            valid_from='2026-10-01T00:00:00Z',valid_until='2026-10-31T00:00:00Z',timezone='Asia/Shanghai',
            schedules=[dict(id='daily',weekdays=[1,2,3,4,5,6,7],start='02:00',end='03:30')],skip_dates=[],
            allowed_devices=[dict(device_uid=DEVICE,logical_id=5)],max_workers=1,cleanup_reserve_seconds=3,task_timeout_seconds=10,
            spool_max_bytes=64*1024*1024,spool_high_watermark=.8)
        self.task=dict(task_id='add',adapter='cann-add',device_uid=DEVICE,warmup=20,repeats=10,pilot_upper_seconds=45,estimated_output_bytes=1024*1024)
        self.plan=dict(schema_version=1,plan_id='fixture-plan',valid_from=self.policy['valid_from'],valid_until=self.policy['valid_until'],manifest_sha256=digest(CannAddAdapter().manifest),preset='latency-v1',tasks=[self.task])
        self.save_config()
        self.center=Center(self.root/'center'); self.addCleanup(self.center.close)

    def save_config(self):
        atomic_json(self.root/'policy.json',self.policy); atomic_json(self.root/'plan.json',self.plan)

    def release(self,*args): return dict(status='RELEASED',checked_at='2026-10-03T18:10:00Z',evidence=dict(stdout='No process in device.'))

    def runner(self,root,**kwargs):
        self.calls.append(kwargs)
        self.assertEqual(kwargs['expected_device_uid'],DEVICE)
        return fixture_run(root,kwargs,self.now)

    def agent(self,runner=None,hook=None,release=None):
        agent=Agent(self.root/'state',self.root/'policy.json',self.root/'plan.json',clock=lambda:self.now,
            runner=runner or self.runner,release=release or self.release,fault_hook=hook)
        self.addCleanup(agent.close); return agent

    def test_empty_task_plan_fails_closed_without_launch(self):
        self.plan['tasks']=[];self.save_config();agent=self.agent()
        self.assertEqual(agent.tick()['state'],'CONFIG_INVALID');self.assertEqual(self.calls,[])

    def test_complete_capture_must_match_frozen_task_manifest(self):
        def wrong_case(root,**kwargs):
            result=self.runner(root,**kwargs)
            manifest=json.loads((Path(root)/'manifest.json').read_text());manifest['implementation']='unexpected provider declaration'
            atomic_json(Path(root)/'manifest.json',manifest)
            for name in ('plan','session'):
                path=Path(root)/(name+'.json');data=json.loads(path.read_text());data['plan_sha256']=digest(manifest)
                if name=='plan':data['case_manifest_sha256']=digest(manifest)
                atomic_json(path,data)
            artifacts=json.loads((Path(root)/'artifacts.json').read_text())
            for artifact in artifacts:
                path=Path(root)/artifact['uri'].split('/',3)[-1]
                artifact.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)
            atomic_json(Path(root)/'artifacts.json',artifacts)
            return result
        agent=self.agent(runner=wrong_case);result=agent.tick()
        self.assertFalse(agent.status()['outbox']);self.assertNotEqual(result.get('terminal'),'COMPLETED')

    def test_draft_policy_and_plan_can_change_before_window(self):
        self.now=timestamp('2026-10-04T01:00:00+08:00')
        self.policy['enabled']=False; self.save_config(); agent=self.agent()
        self.assertEqual(agent.tick()['state'],'WAITING_WINDOW')
        self.policy['enabled']=True; self.plan['tasks'][0]['pilot_upper_seconds']=60
        self.save_config(); self.now=timestamp('2026-10-04T02:10:00+08:00')
        self.assertEqual(agent.tick()['terminal'],'COMPLETED')
        self.assertEqual(len(self.calls),1)

    def test_invalid_configuration_does_not_block_outbox(self):
        for missing in (True,False):
            with self.subTest(missing=missing):
                self.save_config(); agent=self.agent(); agent.tick()
                if missing: (self.root/'policy.json').unlink()
                else: (self.root/'plan.json').write_text('{corrupt')
                self.assertEqual(agent.tick(self.center.import_bundle)['state'],'CONFIG_INVALID')
                self.assertTrue(all(row['state']=='ACKED' for row in agent.status()['outbox']))
                self.assertFalse(any(Path(row['path']).exists() for row in agent.status()['outbox']))
        self.assertEqual(len(self.calls),1)

    def test_execution_write_failure_uses_ownership_and_quarantines(self):
        from unittest.mock import patch
        def runner(root,**kwargs):
            Path(root).mkdir()
            atomic_json(Path(root)/'benchmark-ownership.json',dict(state='RESIDUAL'))
            return dict(valid=False,device_release='UNKNOWN',reason='execution.json disk full')
        agent=self.agent(runner=runner)
        with patch('kernelx.agent.engine.terminate_recorded',return_value='UNKNOWN'):
            agent.tick()
        self.assertEqual(agent.status()['devices'][0]['state'],'QUARANTINED')
        self.assertEqual(agent.status()['attempts'][0]['state'],'FAILED')
        self.assertEqual(agent.status()['attempts'][0]['release_status'],'UNKNOWN')

    @unittest.skipUnless(sys.platform=='linux','Linux ownership identities required')
    def test_execution_record_failure_releases_actual_owned_process(self):
        children=[]
        def runner(root,**kwargs):
            Path(root).mkdir()
            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
            children.append(child)
            identity=process_identity(child.pid)
            atomic_json(Path(root)/'benchmark-ownership.json',dict(state='RESIDUAL',process_group=child.pid,members=[identity]))
            return dict(valid=False,device_release='UNKNOWN',reason='execution.json write failed after launch')
        try:
            agent=self.agent(runner=runner); agent.tick()
            children[0].wait(timeout=3)
            self.assertEqual(agent.status()['attempts'][0]['release_status'],'RELEASED')
            self.assertEqual(agent.status()['attempts'][0]['state'],'FAILED')
            self.assertEqual(agent.status()['devices'],[])
        finally:
            for child in children:
                if child.poll() is None: os.killpg(child.pid,signal.SIGKILL); child.wait()

    def test_malformed_final_execution_record_still_registers_failure(self):
        def runner(root,**kwargs):
            Path(root).mkdir(); (Path(root)/'execution.json').write_text('{partial')
            return dict(valid=False,device_release='UNKNOWN',reason='torn execution record')
        agent=self.agent(runner=runner); agent.tick()
        attempt=agent.status()['attempts'][0]
        self.assertEqual(attempt['state'],'FAILED')
        self.assertTrue(json.loads(attempt['summary'])['execution']['unreadable'])
        self.assertEqual(agent.status()['devices'][0]['state'],'QUARANTINED')

    def test_execution_write_failure_confirmed_cleanup(self):
        from unittest.mock import patch
        def runner(root,**kwargs):
            Path(root).mkdir(); atomic_json(Path(root)/'benchmark-ownership.json',dict(state='RESIDUAL'))
            raise OSError('execution.json disk full')
        agent=self.agent(runner=runner)
        with patch('kernelx.agent.engine.terminate_recorded',return_value='RELEASED'):
            agent.tick()
        self.assertEqual(agent.status()['attempts'][0]['release_status'],'RELEASED')
        # A thrown runner exception remains quarantined until manual clearance.
        self.assertEqual(agent.status()['devices'][0]['state'],'QUARANTINED')

    def test_60_90_120_minutes_and_duplicate_trigger(self):
        for minutes,end in ((60,'03:00'),(90,'03:30'),(120,'04:00')):
            policy=copy.deepcopy(self.policy); policy['schedules'][0]['end']=end
            active=next(w for w in windows(read_policy(policy),self.now) if w['start']<=self.now<w['end'])
            self.assertEqual(active['end']-active['start'],minutes*60)
        agent=self.agent()
        self.assertEqual(agent.tick()['terminal'],'COMPLETED')
        self.assertEqual(agent.tick()['terminal'],'COMPLETED')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(agent.db.execute('PRAGMA journal_mode').fetchone()[0],'wal')

    def test_outside_window_skip_date_and_expired_plan_no_launch(self):
        for clock in ('2026-10-04T01:59:00+08:00','2026-10-04T10:00:00+08:00'):
            self.now=timestamp(clock); self.agent().tick()
        self.now=timestamp('2026-10-05T02:10:00+08:00'); self.policy['skip_dates']=['2026-10-05']; self.save_config(); self.agent().tick()
        self.policy['skip_dates']=[]; self.plan['valid_until']='2026-10-03T00:00:00Z'; self.save_config(); self.agent().tick()
        self.assertEqual(self.calls,[])

    def test_overnight_window_uses_anchor_day_and_utc(self):
        self.policy['schedules'][0].update(start='23:30',end='00:30'); self.save_config()
        clock=timestamp('2026-10-05T00:10:00+08:00')
        active=next(w for w in windows(self.policy,clock) if w['start']<=clock<w['end'])
        self.assertEqual(active['date'],'2026-10-04'); self.assertEqual(active['end']-active['start'],3600)
        self.assertEqual(active['window_id'],next(w for w in windows(self.policy,clock-1800) if w['start']<=clock-1800<w['end'])['window_id'])

    def test_dst_ambiguous_window_rejected(self):
        self.policy.update(timezone='America/New_York',valid_until='2026-12-01T00:00:00Z')
        self.policy['schedules'][0].update(start='01:30',end='02:30')
        with self.assertRaises(ValueError): windows(self.policy,timestamp('2026-11-01T06:40:00Z'))

    def test_plan_cannot_expand_devices_or_manifest(self):
        self.plan['tasks'][0]['device_uid']='unauthorized'; self.save_config()
        self.assertEqual(self.agent().tick()['state'],'CONFIG_INVALID')
        self.assertEqual(self.calls,[])

    def test_wall_clock_jump_cancels_current_reservation(self):
        def runner(root,**kwargs):
            original=self.now
            self.now=timestamp(kwargs['window_start'])-1; self.assertTrue(kwargs['cancel']())
            self.now=timestamp(kwargs['window_end'])+1; self.assertTrue(kwargs['cancel']())
            self.now=original; Path(root).mkdir(); return dict(valid=False,device_release='RELEASED',reason='clock change fixture')
        self.agent(runner).tick()

    def test_revision_change_does_not_replay_daily_window(self):
        agent=self.agent(); agent.tick()
        self.policy['schedule_revision']=2; self.save_config(); agent.tick()
        self.assertEqual(len(self.calls),1)

    def test_budget_spool_full_and_quarantine_no_launch(self):
        agent=self.agent(); self.now=timestamp('2026-10-04T03:29:30+08:00')
        self.assertIn('budget',agent.tick()['reason']); self.assertEqual(self.calls,[])
        self.now=timestamp('2026-10-05T02:10:00+08:00'); self.policy['spool_max_bytes']=1024; self.save_config()
        self.assertIn('spool',agent.tick()['reason'])
        self.now=timestamp('2026-10-06T02:10:00+08:00'); self.policy['spool_max_bytes']=64*1024*1024; self.save_config()
        agent.quarantine(DEVICE,'fixture residual'); self.assertEqual(agent.tick()['tasks'],['REJECTED'])
        self.assertEqual(self.calls,[])

    def test_revocation_during_task_stops_following_case(self):
        self.plan['tasks'].append(dict(self.task,task_id='second')); self.save_config()
        def runner(root,**kwargs):
            self.calls.append(kwargs); self.policy['enabled']=False; self.save_config()
            self.assertTrue(kwargs['cancel']())
            Path(root).mkdir(); return dict(valid=False,device_release='RELEASED',reason='revoked')
        result=self.agent(runner).tick()
        self.assertEqual(len(self.calls),1); self.assertEqual(result['reason'],'reservation revoked')

    def test_external_occupancy_is_rejection_and_is_not_retried(self):
        def runner(root,**kwargs):
            self.calls.append(kwargs); Path(root).mkdir(); return dict(valid=False,device_release='UNKNOWN',reason='external occupancy')
        agent=self.agent(runner); self.assertEqual(agent.tick()['tasks'],['REJECTED']); agent.tick()
        self.assertEqual(len(self.calls),1); self.assertEqual(agent.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_crash_before_or_after_register_recovers_complete_case_once(self):
        for stage in ('AFTER_SEAL','BEFORE_REGISTER','AFTER_REGISTER'):
            self.root=self.root/stage; self.root.mkdir(); self.save_config(); self.calls=[]
            def hook(current):
                if current==stage: raise Crash(stage)
            agent=self.agent(hook=hook)
            with self.assertRaises(Crash): agent.tick()
            recovered=self.agent(); result=recovered.tick()
            self.assertEqual(result['terminal'],'COMPLETED'); self.assertEqual(len(self.calls),1)
            self.assertEqual(recovered.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_expired_window_preserves_recovered_completed_state(self):
        def hook(stage):
            if stage=='AFTER_REGISTER': raise Crash()
        agent=self.agent(hook=hook)
        with self.assertRaises(Crash): agent.tick()
        self.now=timestamp('2026-10-04T10:00:00+08:00')
        recovered=self.agent(); recovered.tick()
        self.assertEqual(recovered.db.execute('SELECT terminal FROM windows WHERE EXISTS(SELECT 1 FROM tasks WHERE tasks.window_id=windows.window_id)').fetchone()[0],'COMPLETED')
        self.assertEqual(len(self.calls),1)

    def test_cache_pause_resumes_same_window_after_space_recovers(self):
        agent=self.agent()
        filler=agent.spool/'retained-failed-evidence'; filler.write_bytes(b'x'*(52*1024*1024))
        self.assertEqual(agent.tick()['state'],'DRAINING'); self.assertEqual(len(self.calls),0)
        filler.unlink()  # Controlled fixture represents operator-approved reclamation.
        self.assertEqual(agent.tick()['terminal'],'COMPLETED'); self.assertEqual(len(self.calls),1)

    def test_restart_partial_case_interrupted_and_unknown_release_quarantined(self):
        def runner(root,**kwargs):
            self.calls.append(kwargs); Path(root).mkdir(); (Path(root)/'observations.json').write_text('[{"partial":true}]'); raise Crash()
        agent=self.agent(runner)
        with self.assertRaises(Crash): agent.tick()
        def unknown(*args): return dict(status='UNKNOWN',evidence=dict(stdout='unrecognized'))
        recovered=self.agent(release=unknown); recovered.tick()
        self.assertEqual(recovered.db.execute('SELECT state FROM attempts').fetchone()[0],'INTERRUPTED')
        self.assertEqual(recovered.db.execute('SELECT state FROM devices').fetchone()[0],'QUARANTINED')
        self.assertEqual(recovered.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        self.assertEqual(len(self.calls),1)

    def test_missing_process_ownership_is_quarantined_even_when_npu_idle(self):
        def runner(root,**kwargs):
            Path(root).mkdir(); raise Crash()
        agent=self.agent(runner)
        with self.assertRaises(Crash): agent.tick()
        recovered=self.agent(); recovered.tick()
        self.assertEqual(recovered.db.execute('SELECT state FROM devices').fetchone()[0],'QUARANTINED')
        self.assertEqual(recovered.db.execute('SELECT release_status FROM attempts').fetchone()[0],'UNKNOWN')

    def test_lost_ack_network_retry_dedup_and_cleanup_after_durable_import(self):
        agent=self.agent()
        def lost_ack(path):
            self.center.import_bundle(path); raise OSError('connection lost after durable commit')
        agent.tick(lost_ack)
        row=agent.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['state'],'RETRY'); self.assertTrue(Path(row['path']).exists())
        self.assertEqual(self.center.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],30)
        agent.tick(self.center.import_bundle); self.assertTrue(Path(row['path']).exists())
        self.now+=6; agent.tick(self.center.import_bundle)
        self.assertEqual(agent.db.execute('SELECT state FROM outbox').fetchone()[0],'ACKED')
        self.assertFalse(Path(row['path']).exists()); self.assertEqual(len(self.calls),1)
        self.assertEqual(self.center.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],30)
        self.assertEqual(self.center.db.execute('SELECT COUNT(*) FROM imports').fetchone()[0],1)

    def test_restart_after_ack_finishes_cleanup_without_remeasurement(self):
        def hook(stage):
            if stage=='AFTER_ACK': raise Crash()
        agent=self.agent(hook=hook)
        with self.assertRaises(Crash): agent.tick(self.center.import_bundle)
        row=agent.db.execute('SELECT * FROM outbox').fetchone(); self.assertEqual(row['state'],'ACKED'); self.assertTrue(Path(row['path']).exists())
        self.agent().tick(self.center.import_bundle); self.assertFalse(Path(row['path']).exists()); self.assertEqual(len(self.calls),1)

    def test_bad_ack_retains_spool(self):
        agent=self.agent(); agent.tick(lambda path:dict(durable=True,bundle_id='wrong',manifest_sha256='wrong'))
        row=agent.db.execute('SELECT * FROM outbox').fetchone(); self.assertEqual(row['state'],'RETRY'); self.assertTrue(Path(row['path']).exists())

    def test_center_corrupt_bundle_rejected_and_duplicate_ids_conflict(self):
        source=self.root/'source'; fixture_run(source); seal(source)
        receipt=self.center.import_bundle(source); self.assertEqual(self.center.import_bundle(source),receipt)
        data=json.loads((source/'observations.json').read_text()); data[0]['raw_samples']=[99.0]; (source/'observations.json').write_text(json.dumps(data)); seal(source)
        with self.assertRaises(ValueError): self.center.import_bundle(source)
        self.assertEqual(self.center.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],30)
        (source/'observations.json').write_text('corrupt')
        with self.assertRaises(ValueError): verify_bundle(source)

    def test_historical_p95_does_not_cross_repeat_policy_or_reduce_pilot(self):
        agent=self.agent()
        for day,cost in zip(range(4,9),(10,11,13,17,100)):
            self.now=timestamp('2026-10-%02dT02:10:00+08:00' % day)
            agent.tick()
            with agent.db:
                agent.db.execute('UPDATE attempts SET cost=? WHERE window_id=(SELECT window_id FROM windows ORDER BY start DESC LIMIT 1)',(cost,))
        self.assertEqual(agent.budget(self.task,digest(self.plan)),120)
        changed=copy.deepcopy(self.plan); changed['tasks'][0]['warmup']=21
        self.assertEqual(agent.budget(changed['tasks'][0],digest(changed)),45)

    def test_database_entry_retains_each_library_version_and_git_fields(self):
        source=self.root/'query-source'; fixture_run(source); seal(source); self.center.import_bundle(source)
        entry=self.center.entry()
        self.assertEqual(entry['case']['operator'],'Add')
        self.assertEqual(entry['hardware']['device']['hardware_bin']['value'],'Ascend 910B1')
        libraries={item['name']:item for item in entry['software']['operator_libraries']}
        for name in ('ops-transformer','sgl-kernel-npu','tile-kernels'):
            self.assertIn('git_commit',libraries[name]); self.assertIn('status',libraries[name]['version'])
        self.assertEqual(self.center.entry(entry['observation_id']),entry)
        self.assertEqual(len(entry['artifact_bundle_ids']),1)
        for artifact in json.loads((source/'artifacts.json').read_text()):
            self.assertTrue(self.center.artifact_path(artifact['artifact_id']).is_file())
        with self.assertRaises(ValueError): self.center.entry('nonexistent')

    def test_https_transport_rejects_plaintext_or_embedded_credentials(self):
        for url in ('http://example.com/import','https://user:secret@example.com/import'):
            with self.assertRaises(ValueError): HTTPTransport(url)

    def test_runtime_revocation_cleans_owned_process(self):
        started=time.monotonic()
        result=run_owned([sys.executable,'-c','import time;time.sleep(30)'],self.root/'cancel.log',10,.1,cancel=lambda:time.monotonic()-started>.1)
        self.assertEqual(result['reason'],'CANCELLED'); self.assertEqual(result['process_release'],'RELEASED')

    @unittest.skipUnless(Path('/proc').is_dir(),'Linux PID reuse guard')
    def test_unmatched_start_time_never_signals_external_group(self):
        external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            for key in ('start_ticks','boot_id'):
                identity=process_identity(external.pid); identity[key]='forged'
                ownership=self.root/'wrong-owner.json'; atomic_json(ownership,dict(process_group=external.pid,state='ACTIVE',members=[identity]))
                self.assertEqual(terminate_recorded(ownership,.1),'UNKNOWN'); self.assertIsNone(external.poll())
        finally: external.kill(); external.wait()

    @unittest.skipUnless(Path('/proc').is_dir(),'Linux PID/start-time recovery')
    def test_crashed_supervisor_cleanup_uses_owned_group_and_preserves_external(self):
        ownership=self.root/'ownership.json'
        code="from kernelx.supervisor import run_owned; import sys; run_owned([sys.executable,'-c','import time;time.sleep(60)'],sys.argv[2],60,.1,ownership_path=sys.argv[1])"
        parent=subprocess.Popen([sys.executable,'-c',code,str(ownership),str(self.root/'owned.log')])
        external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
        try:
            deadline=time.monotonic()+10
            while not ownership.exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(ownership.exists()); parent.kill(); parent.wait(timeout=5)
            self.assertEqual(terminate_recorded(ownership,.1),'RELEASED'); self.assertIsNone(external.poll())
        finally:
            if parent.poll() is None: parent.kill(); parent.wait()
            external.kill(); external.wait()


if __name__=='__main__': unittest.main()
