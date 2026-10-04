"""One frozen CANN case, explicit manual window, owned process group and artifacts."""
import hashlib
import json
import os
import re
import shutil
import signal
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cann_adapter import CannAddAdapter, PRESET
from .probe import Collector, fact, now, probe
from .profile_parser import PARSER_VERSION, parse_add
from .protocol import digest, validate
from .supervisor import run_owned
from .device_lock import DeviceLock


def utc(value):
    date=datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None: raise ValueError('window timestamps need timezone')
    return date.astimezone(timezone.utc).timestamp()


def window_budget(start,end,cleanup,clock=None):
    clock=time.time() if clock is None else clock
    if cleanup < 3 or end <= start: raise ValueError('window requires at least three seconds cleanup reserve')
    if clock < start: raise ValueError('window not yet open; no early run')
    if clock >= end-cleanup: raise ValueError('soft cutoff reached; no new case')
    return end-clock-cleanup


def save(path,value,entity=None):
    if entity: validate(entity,value)
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def envelope(**fields):
    return dict(schema_version=1,protocol_version='latency-v1',extensions={},**fields)


def release_check(device, owned_pids, collector):
    record=collector.run(['npu-smi','info','-t','proc-mem','-i',str(device),'-c', '0'])
    if record['execution_status'] != 'KNOWN':
        return dict(status='UNKNOWN',checked_at=now(),evidence=record)
    output=record['stdout']
    if re.search(r'no process|no running process',output,re.I):
        status='RELEASED'
    else:
        # Unknown formats never count as proof of release. Do not reset the NPU.
        ids=[int(v) for v in re.findall(r'(?im)^\s*(?:Process\s*(?:ID|id|Id)|PID)\s*:\s*(\d+)',output)]
        status='RESIDUAL' if set(ids)&set(owned_pids) else ('RELEASED' if ids else 'UNKNOWN')
    return dict(status=status,checked_at=now(),evidence=record)


def _collect(output,server_id,device,window_start,window_end,authorization_id,repeats=10,warmup=20,timeout=90,cleanup=3,cancel=None,expected_device_uid=None):
    benchmark_env = dict(os.environ)
    if 'ASCEND_RT_VISIBLE_DEVICES' in benchmark_env:
        raise ValueError('ASCEND_RT_VISIBLE_DEVICES is unsupported; unset it before collecting with direct device IDs')
    if cancel is not None and cancel(): raise ValueError('reservation revoked before preparation')
    if not authorization_id: raise ValueError('manual authorization ID required')
    if device < 0 or timeout <= 0: raise ValueError('invalid device or timeout')
    start,end=utc(window_start),utc(window_end)
    window_budget(start,end,cleanup)
    root=Path(output).resolve()
    root.mkdir(parents=True,exist_ok=False)  # never overwrite or combine profiles
    os.chmod(root,0o700)
    adapter=CannAddAdapter()
    manifest=adapter.manifest; case=manifest['case']; key=manifest['case_key']
    save(root/'manifest.json',manifest); save(root/'preset.json',PRESET)
    collector=Collector(server_id)
    env=probe(server_id)
    target=[d for d in env['devices'] if d['logical_id']==device]
    if len(target)!=1: raise ValueError('logical device mapping missing or ambiguous')
    if expected_device_uid is not None and target[0]['device_uid']!=expected_device_uid:
        raise ValueError('registered device identity does not match logical ID')
    if target[0]['chip_id'] != 0 or target[0]['npu_id'] != device:
        raise ValueError('first adapter requires direct device/NPU ID and chip 0 mapping')
    capabilities=adapter.capabilities(); save(root/'capabilities.json',capabilities)
    if capabilities['status']=='UNSUPPORTED': raise RuntimeError(capabilities['reason'])
    binary=adapter.build(root/'build')
    raw=root/'raw'; raw.mkdir(mode=0o700)
    sidecar=root/'sidecar.jsonl'
    session_id,attempt_id,task_id,profile_id,plan_id,policy_id=[str(uuid.uuid4()) for _ in range(6)]
    save(root/'authorization.json',dict(authorization_id=authorization_id,device_id=device,device_uid=target[0]['device_uid'],
                                        window_start=window_start,window_end=window_end,cleanup_reserve_seconds=cleanup))
    plan=envelope(plan_id=plan_id,plan_sha256=digest(manifest),case_manifest_sha256=digest(manifest),policy_id=policy_id,policy_version=1,
                  valid_from=datetime.fromtimestamp(start,timezone.utc).isoformat().replace('+00:00','Z'),
                  valid_until=datetime.fromtimestamp(end,timezone.utc).isoformat().replace('+00:00','Z'),
                  case_keys=[key],preset='latency-v1',budget_seconds=end-start,cleanup_reserve_seconds=cleanup,priorities={key:1})
    save(root/'plan.json',plan,'plan')
    save(root/'preparation.json',adapter.prepare(device,warmup,repeats,raw,sidecar))
    remaining=window_budget(start,end,cleanup) # compile/probe can consume the window
    argv=adapter.benchmark_command(binary,device,warmup,repeats,raw,sidecar)
    if cancel is not None and cancel(): raise ValueError('reservation revoked before device preflight')
    with DeviceLock(target[0]['device_uid']) as device_lock:
        save(root/'device-lock.json',dict(device_uid=target[0]['device_uid'],path=str(device_lock.path),status='ACQUIRED'))
        before=release_check(device,[],collector)
        save(root/'device-before.json',before)
        # An existing process is a conflict even in a manually authorized window.
        if before['status']!='RELEASED' or not re.search(r'no process|no running process',before['evidence']['stdout'],re.I):
            raise RuntimeError('device preflight has external occupancy or unknown status')
        if cancel is not None and cancel(): raise ValueError('reservation revoked before benchmark')
        remaining=window_budget(start,end,cleanup)
        wall_deadline=time.monotonic()+end-time.time()
        began_ns=time.monotonic_ns()
        execution=run_owned(argv,root/'benchmark.log',min(timeout,remaining),grace=min(2,cleanup),env=benchmark_env,pass_fds=(device_lock.fd,),cancel=cancel,ownership_path=root/'benchmark-ownership.json')
        save(root/'execution.json',execution)
        # Query actual NPU process ownership independently of process-group exit.
        release=release_check(device,execution['owned_pids'],collector)
        for _ in range(3):
            if release['status']=='RELEASED' or time.monotonic()>=wall_deadline: break
            time.sleep(.1); release=release_check(device,execution['owned_pids'],collector)
        release['by_hard_cutoff']=release['status']=='RELEASED' and utc(release['checked_at'])<=end
        released_ns=time.monotonic_ns()
        save(root/'device-release.json',release)
    reason=execution['reason']
    succeeded=execution['exit_code']==0 and not reason and execution['process_release']=='RELEASED' and release['by_hard_cutoff']
    state='SUCCEEDED' if succeeded else ('INTERRUPTED' if reason=='INTERRUPTED' else 'FAILED')
    attempt=envelope(attempt_id=attempt_id,session_id=session_id,task_id=task_id,case_key=key,ordinal=0,started_at=execution['started_at'],ended_at=execution['ended_at'],
                     state=state,exit_code=execution['exit_code'],process_group=execution['process_group'],release_status=release['status'],
                     released_at=release['checked_at'] if release['status']=='RELEASED' else None,evidence_ids=[release['evidence']['evidence_id']],reason=reason)
    save(root/'attempt.json',attempt,'attempt')
    versions=None
    providers=Path(str(sidecar)+'.providers.json')
    if succeeded and providers.is_file():
        versions=adapter.resolve_versions(providers); save(root/'runtime-versions.json',versions)
        evidence=collector.record(['runtime-provider-fingerprints'],json.dumps(versions)); evidence['parse_status']='KNOWN'
        # Evidence IDs from the initial probe and this collector occupy separate namespaces.
        evidence['evidence_id']='runtime-'+evidence['evidence_id']
        env['evidence'].append(evidence)
        env['software']['operator_libraries'].append(dict(name='cann-aclnn-api',role='api_provider',
             version=fact(status='UNKNOWN',reason='loaded API fingerprint observed; semantic version not established',source=[evidence['evidence_id']]),
             resolved_path=versions['api_path'],package_id=None,repository_url=None,git_commit=None,dirty_tree_sha256=None,
             artifact_sha256=versions['api_sha256'],load_status='VERIFIED' if versions['status']=='VERIFIED_HOST_PROVIDER' else 'DECLARED_ONLY',used_by_case=[key]))
        env['extensions']['runtime_host_providers']=versions
    env['extensions']['manual_authorization_id']=authorization_id
    save(root/'environment.json',env,'environment')
    profile_dirs=list(raw.glob('PROF_*'))
    export=None
    if succeeded and len(profile_dirs)==1:
        export=run_owned([adapter.msprof,'--export=on','--output='+str(profile_dirs[0])],root/'export.log',120)
        save(root/'export.json',export)
    exports=list(raw.glob('PROF_*/mindstudio_profiler_output'))
    def select(pattern):
        paths=list(exports[0].glob(pattern)) if len(exports)==1 else []
        return paths[0] if len(paths)==1 else root/'MISSING'
    parsed=parse_add(select('op_summary_*.csv'),select('msprof_[0-9]*.json'),select('msprof_tx_*.json'),sidecar,device,repeats,warmup)
    if not succeeded or not export or export['exit_code']!=0 or export['reason']:
        parsed.update(valid=False,quality=['INSUFFICIENT_DATA'],reasons=['benchmark/release/export incomplete'],invocations=[])
    save(root/'parsed.json',parsed)
    valid=parsed['valid']
    if valid and versions and versions['status']=='VERIFIED_HOST_PROVIDER':
        for entry in env['support_matrix']:
            if entry['library']=='cann-opp' and entry['hardware_bin']==target[0]['hardware_bin']['value']:
                # A separate result, not a mutation of the frozen environment snapshot.
                support=dict(entry,revision='sha256:'+versions['api_sha256'],cann='loaded-runtime-sha256:'+versions['loaded_libraries_sha256'],status='VERIFIED',verified_at=now(),
                             reason='only frozen Add case '+key+' with this runtime fingerprint and preset was verified')
                save(root/'verified-support.json',support)
    artifacts=[]
    if profile_dirs:
        with tarfile.open(root/'raw-prof.tar.gz','w:gz') as tar:
            for path in profile_dirs: tar.add(path,arcname=path.name)
    files=[(root/'raw-prof.tar.gz','RAW_PROF'),(sidecar,'SIDECAR'),(root/'environment.json','ENVIRONMENT'),(root/'benchmark.log','LOG')]
    for directory in exports:
        files.extend((path,'CSV' if path.suffix=='.csv' else 'TRACE') for path in directory.iterdir() if path.suffix in ('.csv','.json'))
    indexed={path for path,_ in files}
    for path in list(root.glob('*.json')) + [root/'build/build.json',root/'build/build.log',root/'export.log']:
        if path not in indexed:
            files.append((path,'LOG')); indexed.add(path)
    for path,kind in files:
        if not path.is_file(): continue
        artifact=envelope(artifact_id=str(uuid.uuid4()),uri='artifact://'+profile_id+'/'+str(path.relative_to(root)),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size,
                          kind=kind,created_at=now(),redacted=kind=='ENVIRONMENT',media_type='application/gzip' if kind=='RAW_PROF' else 'text/plain')
        validate('artifact',artifact); artifacts.append(artifact)
    save(root/'artifacts.json',artifacts)
    version=fact(status='UNKNOWN',reason='msprof API version unavailable; executable fingerprint in environment',source=['environment:'+env['environment_id']])
    profile=envelope(profile_id=profile_id,attempt_id=attempt_id,case_key=key,preset='latency-v1',preset_sha256=digest(PRESET),collector_version=version,exporter_version=version,parser_version=PARSER_VERSION,
                     artifact_ids=[a['artifact_id'] for a in artifacts],final_argv=argv,completeness='COMPLETE' if valid else 'PARTIAL',
                     export_status='KNOWN' if export and export['exit_code']==0 else 'COMMAND_FAILED',attribution_status='KNOWN' if valid else 'PARSE_ERROR',
                     expected_task_count=repeats,actual_task_count=parsed['actual_task_count'],task_mapping=[dict(iteration=i['iteration'],rank=0,task_ids=i['task_ids']) for i in parsed['invocations']],
                     quality=parsed['quality'],reason='; '.join(parsed['reasons']) or None)
    save(root/'profile.json',profile,'profile')
    observations=[]
    for invocation in parsed['invocations']:
        for metric,samples,boundary in [('task_duration_us',invocation['task_duration_us'],'single trace-matched Add task'),
                                        ('device_span_us',[invocation['device_span_us']],'matched device task end minus start'),
                                        ('host_elapsed_us',[invocation['host_elapsed_us']],'monotonic immediately before rangeStart through rangeStop including stream synchronize under profiling')]:
            observation=envelope(observation_id=str(uuid.uuid4()),profile_id=profile_id,attempt_id=attempt_id,session_id=session_id,environment_id=env['environment_id'],device_uid=target[0]['device_uid'],
                 task_id=task_id,case_key=key,input_sha256=case['input_generation']['input_sha256'],seed=0,round=0,iteration=invocation['iteration'],rank=0,phase='MEASURE',task_ids=invocation['task_ids'],
                 metric=dict(name=metric,unit='us',boundary=boundary,definition_version='1'),raw_samples=samples,completeness='COMPLETE',quality=['VALID'])
            validate('observation',observation); observations.append(observation)
    save(root/'observations.json',observations)
    build=json.loads((root/'build/build.json').read_text())
    session=envelope(session_id=session_id,server_id=server_id,device_uids=[target[0]['device_uid']],environment_id=env['environment_id'],plan_id=plan_id,plan_sha256=plan['plan_sha256'],release_id='source:'+digest(dict(manifest=manifest,source_sha256=build['source_sha256'],binary_sha256=build['binary_sha256'])),
                     policy_id=policy_id,policy_version=1,window_id=authorization_id,started_at=execution['started_at'],ended_at=execution['ended_at'],state='COMPLETED' if valid else 'FAILED',
                     resource_ledger=[dict(device_uid=target[0]['device_uid'],start_monotonic_ns=began_ns,end_monotonic_ns=released_ns if release['status']=='RELEASED' else None)],reason=profile['reason'])
    save(root/'session.json',session,'session')
    result=dict(valid=valid,output=str(root),attempt_state=state,quality=parsed['quality'],device_release=release['status'],
                released_by_hard_cutoff=release['by_hard_cutoff'],observations=len(observations),case_key=key,authorization_id=authorization_id)
    save(root/'result.json',result)
    return result


def collect(output, **kwargs):
    """Persist preflight/startup failures without inventing a successful attempt."""
    root=Path(output)
    if root.exists(): raise FileExistsError('run output must not already exist')
    previous=signal.getsignal(signal.SIGTERM)
    def interrupt(signum,frame): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupt)
    try:
        return _collect(output, **kwargs)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt) as exc:
        root.mkdir(parents=True,exist_ok=True)
        result=dict(valid=False,output=str(root),quality=['INSUFFICIENT_DATA'],reason=str(exc),observations=0,
                    phase='STARTUP_OR_PIPELINE_FAILURE',device_release='UNKNOWN')
        save(root/'failure.json',result)
        return result
    finally:
        signal.signal(signal.SIGTERM,previous)
