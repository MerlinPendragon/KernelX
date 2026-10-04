"""Finite 910B1 release acceptance; no persistent timer or host updates."""
import functools
import http.server
import json
import os
import ssl
import subprocess
import threading
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from kernelx.bootstrap import Bootstrap
from kernelx.agent.storage import atomic_json, Center
from kernelx.cann_adapter import CannAddAdapter
from kernelx.protocol import digest

base=Path('/home/lxb/kernelx-issue4')/('acceptance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
base.mkdir(mode=0o700); repository=Path('/home/lxb/kernelx-issue4/repository')
first='f4b864164243cba4f220d59806d6c87b50304585a6b19d6e385565df0206edef'
second='819682511cb6e4aabe33f0f8d194e7ae8e5c086ac1a50faf3503606bd6bc02d5'
server='55dcc47c-f8a8-4f3f-ab2d-c02bd385c470'; uid='1ee8d14b-2e71-5beb-aa56-e58fd4c7b6ae'
now=datetime.now(timezone.utc); local=now.astimezone(ZoneInfo('Asia/Shanghai'))
policy=dict(schema_version=1,server_id=server,reservation_id='user-all-devices-finite-issue4-smoke',schedule_revision=1,enabled=True,
 valid_from=(now-timedelta(seconds=5)).isoformat(),valid_until=(now+timedelta(seconds=300)).isoformat(),timezone='Asia/Shanghai',
 schedules=[dict(id='finite',weekdays=list(range(1,8)),start=(local-timedelta(minutes=1)).strftime('%H:%M'),end=(local+timedelta(minutes=10)).strftime('%H:%M'))],skip_dates=[],allowed_devices=[dict(device_uid=uid,logical_id=5)],max_workers=1,cleanup_reserve_seconds=3,task_timeout_seconds=90,spool_max_bytes=512*1024**2,spool_high_watermark=.8)
plan=dict(schema_version=1,plan_id='issue4-smoke',valid_from=policy['valid_from'],valid_until=policy['valid_until'],manifest_sha256=digest(CannAddAdapter().manifest),preset='latency-v1',tasks=[dict(task_id='add',adapter='cann-add',device_uid=uid,warmup=20,repeats=10,pilot_upper_seconds=90,estimated_output_bytes=16*1024**2)])
atomic_json(base/'policy.json',policy); atomic_json(base/'plan.json',plan)
config=dict(schema_version=1,root=str(base/'bootstrap'),trusted_key='/home/lxb/kernelx-issue4/kernelx-issue4-release.pub',server_id=server,device_uid=uid,policy=str(base/'policy.json'),plan=str(base/'plan.json'),center_dir=str(base/'center'),source=str(repository),smoke=False,run_agent=False,max_package_bytes=128*1024**2,disk_reserve_bytes=64*1024**2)
atomic_json(repository/'latest.json',dict(release_id=first)); atomic_json(base/'cpu-config.json',config)
boot=Bootstrap(config)
try:
    offline=boot.tick(); assert offline['state']=='HEALTHY' and boot.current()==first,offline
finally: boot.close()
print('OFFLINE INSTALL + RSA-PSS VERIFY: PASS',flush=True)
# TLS feed is a local finite test server. Signing private key stays off target.
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(base/'tls.key'),'-out',str(base/'tls.crt'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
http=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(repository)))
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(base/'tls.crt',base/'tls.key'); http.socket=context.wrap_socket(http.socket,server_side=True)
thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
url='https://localhost:%d'%http.server_port
atomic_json(repository/'feed.json',dict(release_url=url+'/'+second))
config.update(source=url+'/feed.json',ca_file=str(base/'tls.crt'),smoke=True)
atomic_json(base/'https-smoke-config.json',config)
try:
    boot=Bootstrap(config)
    try:
        upgrade=boot.tick(); assert upgrade['state']=='HEALTHY' and boot.current()==second,upgrade
        status=boot.status(); assert status['health']['last_success']['smoke'] is True,status
        repeated=boot.tick(); assert repeated['state']=='CURRENT'
        assert len(boot.status()['sessions'])==1,'no repeated NPU smoke'
        atomic_json(base/'status.json',status)
    finally: boot.close()
finally: http.shutdown(); http.server_close(); thread.join(timeout=5)
center=Center(base/'center')
try:
    count=center.db.execute('SELECT count(*) FROM observations').fetchone()[0]; assert count==30,count
    row=center.entry(); assert row['release']['release_id']==second,row['release']
    bundle=next((base/'center/bundles').iterdir()); release=json.loads((bundle/'device-release.json').read_text())
    assert release['status']=='RELEASED'
    atomic_json(base/'database-row.json',row)
finally: center.close()
# CPU-only transient systemd entry; no timer enabled, no reboot, no NPU.
unit='kernelx-issue4-acceptance-'+datetime.now(timezone.utc).strftime('%H%M%S')
command=['sudo','-n','systemd-run','--unit='+unit,'--collect','--wait','--property=Type=oneshot','--property=User=lxb','--property=Group=lxb','--property=WorkingDirectory=/home/lxb/kernelx-issue4','/bin/bash','-lc','source /usr/local/Ascend/ascend-toolkit/set_env.sh && python3 -B -m kernelx bootstrap-tick --config '+str(base/'cpu-config.json')]
completed=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=120)
(base/'systemd.log').write_text(completed.stdout); assert completed.returncode==0,completed.stdout
# CPU config points at original offline release, intentionally rejected by signed
# sequence floor while current healthy release remains runnable.
summary=dict(root=str(base),evidence_type='910B1_REAL_SINGLE_SERVER',offline_install=offline,https_upgrade=upgrade,repeat_tick=repeated,release_ids=[first,second],source_commit=json.loads((repository/second/'manifest.json').read_text())['git_commit'],sequences=[3,4],npu_device=5,warmup=20,repeats=10,observations=count,device_release=release['status'],release_checked_at=release['checked_at'],systemd_transient_unit=unit,systemd_exit=completed.returncode,persistent_timer_installed=False,shared_host_rebooted=False)
atomic_json(Path('/home/lxb/kernelx-issue4/acceptance.json'),summary)
print(json.dumps(summary),flush=True)
