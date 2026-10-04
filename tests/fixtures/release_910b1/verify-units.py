from pathlib import Path
import json,subprocess
base=Path(json.loads(Path('/home/lxb/kernelx-issue4/acceptance.json').read_text())['root'])
entry=base/'service-entry.sh'
entry.write_text('#!/bin/sh\nset -eu\n. /usr/local/Ascend/ascend-toolkit/set_env.sh\ncd /home/lxb/kernelx-issue4\nexec /usr/bin/python3 -B -m kernelx bootstrap-tick --config '+str(base/'cpu-config.json')+'\n'); entry.chmod(0o755)
units=base/'units'; units.mkdir(exist_ok=True)
service=Path('deploy/systemd/kernelx-bootstrap.service').read_text().replace('/opt/kernelx/bootstrap/deploy/kernelx-bootstrap',str(entry)).replace('User=kernelx','User=lxb').replace('Group=kernelx','Group=lxb')
(units/'kernelx-bootstrap.service').write_text(service); (units/'kernelx-bootstrap.timer').write_text(Path('deploy/systemd/kernelx-bootstrap.timer').read_text())
result=subprocess.run(['systemd-analyze','verify',str(units/'kernelx-bootstrap.service'),str(units/'kernelx-bootstrap.timer')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
(base/'unit-verification.log').write_text(result.stdout+'\nexit='+str(result.returncode)+'\n'); assert result.returncode==0,result.stdout
summary=json.loads(Path('acceptance.json').read_text()); summary['unit_verification_exit']=result.returncode;Path('acceptance.json').write_text(json.dumps(summary,indent=2)+'\n')
print('SYSTEMD SERVICE/TIMER VERIFY: PASS')
