import json,time,os
from pathlib import Path
from datetime import datetime,timezone
from kernelx.runner import collect
from kernelx.agent.storage import seal,Center,atomic_json
from kernelx.libraries import Registry,SupportMatrix,environment_tuple
from kernelx.agent.fleet import Fleet,FleetWorker
from kernelx.cann_adapter import CannAddAdapter
from kernelx.protocol import digest
SERVER='55dcc47c-f8a8-4f3f-ab2d-c02bd385c470';UID='1ee8d14b-2e71-5beb-aa56-e58fd4c7b6ae'
root=Path('/home/lxb/kernelx-issues56');root.mkdir(exist_ok=True)
os.environ['KERNELX_NATIVE_CACHE_ROOT']=str(root/'native-cache')
iso=lambda x:datetime.fromtimestamp(x,timezone.utc).isoformat()
center=Center(root/'center');matrix=SupportMatrix(root/'matrix');fleet=Fleet(root/'control')
bundles=[];runs=[];receipts=[]
try:
    for i in range(3):
        start=time.time()-1;end=start+75;window='issues56-pilot-window-'+str(i+1)
        if i==0:
            run=root/'pilot1'
            result=collect(run,server_id=SERVER,device=5,expected_device_uid=UID,window_start=iso(start),window_end=iso(end),authorization_id=window,warmup=20,repeats=10,timeout=50,cleanup=5)
            print(json.dumps(dict(window=window,result=result)),flush=True)
            if not result['valid']:raise RuntimeError('pilot failed')
            sealed=seal(run);receipt=center.import_bundle(run);receipts.append(receipt);bundles.append(run)
            env=json.loads((run/'environment.json').read_text())
            binding=environment_tuple(env,UID,'cann-opp',digest(CannAddAdapter().manifest))
            matrix.record(binding,'VERIFIED','complete actual CANN Add runtime/provider/profile in finite pilot window',dict(valid=True,provider_verified=env['extensions']['runtime_host_providers']['status']=='VERIFIED_HOST_PROVIDER',case_key=result['case_key'],bundle_sha256=digest(sealed),bundle_id=sealed['bundle_id'],evidence_uri=str(run)))
            inventory=Registry().inventory(env,UID,matrix=matrix)
            atomic_json(root/'inventory.json',inventory)
        else:
            policy=dict(schema_version=1,server_id=SERVER,reservation_id=window,schedule_revision=i+1,enabled=True,valid_from=iso(start),valid_until=iso(end),timezone='Asia/Shanghai',schedules=[dict(id=window,weekdays=list(range(1,8)),start='00:00',end='23:59')],skip_dates=[],allowed_devices=[dict(device_uid=UID,logical_id=5)],max_workers=1,cleanup_reserve_seconds=5,task_timeout_seconds=50,spool_max_bytes=200000000,spool_high_watermark=.8)
            policy_path=root/('policy%d.json'%i);atomic_json(policy_path,policy)
            caps={r['library']:r for r in inventory};caps['cann-add']=dict(status='VERIFIED',soc=binding['soc'],bin=binding['hardware_bin'],cann=binding['cann'],library_version=binding['revision']['version'],library_commit=binding['revision']['commit'],preset='latency-v1',manifest_sha256=digest(CannAddAdapter().manifest),environment_sha256=digest(binding),evidence_uri=str(bundles[0]))
            request=dict(submission_id=window,libraries='all',scope='core',servers=[dict(server_id=SERVER,device_uid=UID,capabilities=caps)],mode='paired',valid_from=iso(start),valid_until=iso(end),warmup=20,repeats=10,pilot_upper_seconds=45,estimated_output_bytes=2000000)
            rid=fleet.submit(request);worker=FleetWorker(root/('worker%d'%i),policy_path,fleet,center.import_bundle);status=worker.tick();runs.append(fleet.status(rid));atomic_json(root/('fleet%d.json'%i),runs[-1])
            completed=list((root/('worker%d'%i)).glob('*/agent/spool/*/run/result.json'))
            if len(completed)!=1 or not json.loads(completed[0].read_text())['valid']:raise RuntimeError('fleet pilot failed '+json.dumps(status))
            bundles.append(completed[0].parent)
            print(json.dumps(dict(window=window,fleet=runs[-1]['counts'],result=json.loads(completed[0].read_text()))),flush=True)
        atomic_json(root/'pilot-index.json',dict(bundles=[str(p) for p in bundles],global_runs=runs,first_receipt=receipts,window_ends=[iso(end)]))
        if i<2:
            while time.time()<=end:time.sleep(min(1,max(.01,end-time.time())))
    from kernelx.efficiency import generate
    print(json.dumps(generate(bundles,inventory,root/'report')),flush=True)
finally:center.close();matrix.close();fleet.close()
