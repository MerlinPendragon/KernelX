"""Finite root-only acceptance of the submitted launcher/service; restores host changes."""
import hashlib,json,os,pwd,shutil,subprocess,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sys
sys.path.insert(0,'/home/lxb/kernelx-issue4')
from kernelx.agent.storage import atomic_json,Center
from kernelx.cann_adapter import CannAddAdapter
from kernelx.protocol import digest
assert os.geteuid()==0
src=Path('/home/lxb/kernelx-issue4'); release_id=sys.argv[1]
static=Path('/opt/kernelx'); configdir=Path('/etc/kernelx'); state=Path('/var/lib/kernelx-bootstrap'); centerpath=Path('/var/lib/kernelx-center')
unit=Path('/run/systemd/system/kernelx-bootstrap.service'); dropin=Path('/run/systemd/system/kernelx-bootstrap.service.d')
for path in [static,configdir,state,centerpath,unit,dropin,Path('/etc/systemd/system/kernelx-bootstrap.service')]:
    assert not path.exists(),str(path)+' already exists; will not overwrite'
try: pwd.getpwnam('kernelx'); raise AssertionError('kernelx account already exists')
except KeyError: pass
base=src/('formal-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'));base.mkdir(mode=0o700)
lxb=pwd.getpwnam('lxb'); os.chown(base,lxb.pw_uid,lxb.pw_gid)
uid='1ee8d14b-2e71-5beb-aa56-e58fd4c7b6ae'; server='55dcc47c-f8a8-4f3f-ab2d-c02bd385c470'
locks=Path('/tmp/kernelx-device-locks'); lock=locks/(hashlib.sha256(uid.encode()).hexdigest()+'.lock')
assert locks.is_dir() and lock.is_file()
acl=subprocess.check_output(['getfacl','-p',str(locks),str(lock)],text=True);(base/'original-lock-acl.txt').write_text(acl)
created=False;acl_changed=False
summary=dict(evidence_type='910B1_FORMAL_OFFLINE_SERVICE',release_id=release_id,warmup=20,repeats=10,device=5,persistent_timer_installed=False,shared_host_rebooted=False)
def call(argv,**kwargs): return subprocess.run(argv,check=True,**kwargs)
def own_tree(root,user):
    for p in [root]+list(root.rglob('*')): os.chown(p,user.pw_uid,user.pw_gid,follow_symlinks=False)
def run_service(label,expected):
    subprocess.run(['systemctl','reset-failed','kernelx-bootstrap.service'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    started=time.time();result=subprocess.run(['systemctl','start','kernelx-bootstrap.service'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=240)
    detail=subprocess.check_output(['systemctl','show','kernelx-bootstrap.service','-p','Result','-p','ExecMainStatus','-p','User','-p','Group','-p','ExecStart','-p','PrivateNetwork'],text=True)
    journal=subprocess.check_output(['journalctl','-u','kernelx-bootstrap.service','--since=@'+str(int(started)),'--no-pager','-o','short-iso'],text=True)
    (base/(label+'.log')).write_text(result.stdout+detail+journal)
    assert ('ExecMainStatus='+str(expected)+'\n') in detail,detail
    assert (result.returncode==0)==(expected==0),result.stdout
    summary[label]=dict(service_exit=expected,systemctl_exit=result.returncode,details=detail)
    print(label+': PASS',flush=True)
try:
    call(['useradd','--system','--user-group','--home-dir',str(state),'--shell','/usr/sbin/nologin','kernelx']);created=True
    serviceuser=pwd.getpwnam('kernelx')
    for p in [static/'bootstrap',configdir,state,centerpath,dropin]:p.mkdir(parents=True,mode=0o755)
    for name in ['kernelx','deploy']:shutil.copytree(src/name,static/'bootstrap'/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for p in (static/'bootstrap').rglob('*'):
        p.chmod(0o755 if p.is_dir() else 0o644)
    (static/'bootstrap/deploy/kernelx-bootstrap').chmod(0o755)
    shutil.copytree(src/'repository'/release_id,state/'repository'/release_id)
    atomic_json(state/'repository/latest.json',dict(release_id=release_id))
    shutil.copyfile(src/'kernelx-issue4-release.pub',configdir/'release.pub')
    (configdir/'ascend-env.sh').write_text('. /usr/local/Ascend/ascend-toolkit/set_env.sh\n')
    now=datetime.now(timezone.utc);local=now.astimezone(ZoneInfo('Asia/Shanghai'))
    policy=dict(schema_version=1,server_id=server,reservation_id='user-all-devices-finite-formal-review',schedule_revision=1,enabled=True,valid_from=(now-timedelta(seconds=5)).isoformat(),valid_until=(now+timedelta(seconds=450)).isoformat(),timezone='Asia/Shanghai',schedules=[dict(id='finite',weekdays=list(range(1,8)),start=(local-timedelta(minutes=1)).strftime('%H:%M'),end=(local+timedelta(minutes=15)).strftime('%H:%M'))],skip_dates=[],allowed_devices=[dict(device_uid=uid,logical_id=5)],max_workers=1,cleanup_reserve_seconds=3,task_timeout_seconds=90,spool_max_bytes=512*1024**2,spool_high_watermark=.8)
    plan=dict(schema_version=1,plan_id='formal-review-smoke',valid_from=policy['valid_from'],valid_until=policy['valid_until'],manifest_sha256=digest(CannAddAdapter().manifest),preset='latency-v1',tasks=[dict(task_id='add',adapter='cann-add',device_uid=uid,warmup=20,repeats=10,pilot_upper_seconds=90,estimated_output_bytes=16*1024**2)])
    atomic_json(configdir/'server-policy.json',policy);atomic_json(configdir/'agent-plan.json',plan)
    config=dict(schema_version=1,root=str(state/'failed-first'),trusted_key=str(configdir/'release.pub'),server_id=server,device_uid=uid,policy=str(configdir/'server-policy.json'),plan=str(configdir/'agent-plan.json'),center_dir=str(centerpath),source=str(state/'missing-repository'),smoke=True,run_agent=False,max_package_bytes=128*1024**2,disk_reserve_bytes=64*1024**2)
    atomic_json(configdir/'bootstrap.json',config)
    own_tree(state,serviceuser);own_tree(centerpath,serviceuser)
    call(['setfacl','-m','u:kernelx:rwx',str(locks)]);call(['setfacl','-m','u:kernelx:rw',str(lock)]);acl_changed=True
    shutil.copyfile(src/'deploy/systemd/kernelx-bootstrap.service',unit)
    # Exact submitted service and launcher. The test-only drop-in isolates all
    # network interfaces; no host firewall or public endpoint is changed.
    checker=static/'bootstrap/deploy/verify-offline.py'
    checker.write_text("import json,socket,os\nfrom pathlib import Path\ns=socket.socket();s.settimeout(1)\ntry:\n s.connect(('1.1.1.1',443));raise AssertionError('external network reachable')\nexcept OSError as e:\n result=dict(interfaces=socket.if_nameindex(),connect_error=str(e),net_namespace=os.readlink('/proc/self/ns/net'))\nfinally:s.close()\nassert all(name=='lo' for _,name in result['interfaces'])\nPath('/var/lib/kernelx-bootstrap/network-isolation.json').write_text(json.dumps(result))\n")
    (dropin/'acceptance.conf').write_text('[Service]\nPrivateNetwork=yes\nExecStartPre=/usr/bin/python3 /opt/kernelx/bootstrap/deploy/verify-offline.py\n')
    call(['systemctl','daemon-reload'])
    run_service('first_install_failure',1)
    config.update(root=str(state/'installed'),source=str(state/'repository'));atomic_json(configdir/'bootstrap.json',config)
    run_service('first_install_smoke',0)
    run_service('repeat_tick',0)
    import sqlite3
    db=sqlite3.connect(state/'installed/bootstrap.db');db.row_factory=sqlite3.Row
    sessions=[dict(r) for r in db.execute('SELECT * FROM sessions')]
    health={r['key']:json.loads(r['payload']) for r in db.execute('SELECT * FROM status')};db.close()
    assert len(sessions)==1 and sessions[0]['state']=='SUCCEEDED',sessions
    center=Center(centerpath)
    try:
        observations=center.db.execute('SELECT count(*) FROM observations').fetchone()[0];assert observations==30
        row=center.entry();assert row['release']['release_id']==release_id
        atomic_json(base/'database-row.json',row)
    finally:center.close()
    bundle=next((centerpath/'bundles').iterdir());release=json.loads((bundle/'device-release.json').read_text());assert release['status']=='RELEASED'
    summary.update(observations=observations,sessions=sessions,health=health,network_isolation=json.loads((state/'network-isolation.json').read_text()),device_release=release,source_commit=json.loads((state/'repository'/release_id/'manifest.json').read_text())['git_commit'])
    (base/'submitted-launcher').write_bytes((static/'bootstrap/deploy/kernelx-bootstrap').read_bytes())
    (base/'submitted-service').write_bytes(unit.read_bytes());(base/'network-only-dropin.conf').write_bytes((dropin/'acceptance.conf').read_bytes())
    shutil.copytree(configdir,base/'frozen-config')
finally:
    subprocess.run(['systemctl','stop','kernelx-bootstrap.service'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if unit.exists():unit.unlink()
    if dropin.exists():shutil.rmtree(dropin)
    subprocess.run(['systemctl','daemon-reload'],check=False)
    subprocess.run(['systemctl','reset-failed','kernelx-bootstrap.service'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if acl_changed:
        call(['setfacl','--restore='+str(base/'original-lock-acl.txt')]); summary['lock_acl_restored']=True
    for p in [state,centerpath]:
        if p.exists():shutil.move(str(p),str(base/p.name))
    for p in [static,configdir]:
        if p.exists():shutil.rmtree(p)
    if created:
        call(['userdel','kernelx'])
        subprocess.run(['groupdel','kernelx'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        summary['temporary_service_account_removed']=True
    atomic_json(base/'acceptance.json',summary);own_tree(base,lxb)
    atomic_json(src/'formal-acceptance.json',dict(root=str(base),summary=summary))
print(json.dumps(summary),flush=True)
