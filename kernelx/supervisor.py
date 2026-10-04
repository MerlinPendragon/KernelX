"""Bounded ownership of one child process group; never signal external tasks."""
import os
import json
import signal
import subprocess
import time
from pathlib import Path

from .probe import now


def group_members(pgid):
    """Linux process group membership for release evidence; excludes zombies."""
    result = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            parts = path.read_text().rsplit(')', 1)[1].split()
            if int(parts[2]) == pgid and parts[0] != 'Z':
                result.append(int(path.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    return result



def process_identity(pid):
    try:
        parts=Path('/proc/%d/stat' % pid).read_text().rsplit(')',1)[1].split()
        return dict(pid=pid,pgid=int(parts[2]),start_ticks=parts[19],boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    except (OSError,ValueError,IndexError): return None


def ownership_save(path,value):
    if path is None: return
    path=Path(path); temporary=path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(value,output); output.flush(); os.fsync(output.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def terminate_recorded(path,grace=1):
    """Recover only a group with a surviving PID + Linux start-time match."""
    record=json.loads(Path(path).read_text()); pgid=record['process_group']
    if record['state']=='RELEASED': return 'RELEASED'
    if not group_members(pgid): return 'RELEASED' if Path('/proc').is_dir() else 'UNKNOWN'
    matched=any(process_identity(item['pid'])==item and item['pgid']==pgid for item in record['members'])
    if not matched: return 'UNKNOWN'
    try: os.killpg(pgid,signal.SIGTERM)
    except ProcessLookupError: return 'RELEASED'
    deadline=time.monotonic()+grace
    while group_members(pgid) and time.monotonic()<deadline: time.sleep(.02)
    if group_members(pgid):
        try: os.killpg(pgid,signal.SIGKILL)
        except ProcessLookupError: pass
    deadline=time.monotonic()+.5
    while group_members(pgid) and time.monotonic()<deadline: time.sleep(.02)
    return 'RESIDUAL' if group_members(pgid) else 'RELEASED'


def run_owned(argv, log, timeout, grace=2.0, env=None, pass_fds=(), cancel=None, ownership_path=None, cwd=None):
    if timeout <= 0 or grace < 0:
        raise ValueError('positive timeout and nonnegative grace required')
    started, start = now(), time.monotonic()
    reason, signals, seen = None, [], set()
    with Path(log).open('w') as output:
        proc = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, env=env, pass_fds=pass_fds, start_new_session=True, cwd=cwd)
        pgid = proc.pid
        seen.add(proc.pid)
        identities={}
        ownership_errors=[]
        def persist(state):
            for pid in seen:
                identity=process_identity(pid)
                if identity: identities[pid]=identity
            try:
                ownership_save(ownership_path,dict(process_group=pgid,state=state,members=list(identities.values())))
                return True
            except OSError as exc:
                ownership_errors.append(str(exc)); return False
        last_persist=0
        try:
            if not persist('ACTIVE'): reason='OWNERSHIP_RECORD_FAILED'
            deadline = start + timeout
            while True:
                if reason: break
                seen.update(group_members(pgid))
                if ownership_path and time.monotonic()-last_persist>=1:
                    if not persist('ACTIVE'): reason='OWNERSHIP_RECORD_FAILED'; break
                    last_persist=time.monotonic()
                code = proc.poll()
                if code is not None and not group_members(pgid):
                    break
                if cancel is not None and cancel():
                    reason = 'CANCELLED'
                    break
                if time.monotonic() >= deadline:
                    reason = 'TIMEOUT'
                    break
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        except (KeyboardInterrupt, SystemExit):
            reason = 'INTERRUPTED'
        finally:
            # Main process may have exited while its descendants are still alive.
            persist('DRAINING')
            alive = proc.poll() is None or bool(group_members(pgid))
            if alive:
                signals.append('SIGTERM')
                try: os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError: pass
                end = time.monotonic() + grace
                while time.monotonic() < end and (proc.poll() is None or group_members(pgid)):
                    seen.update(group_members(pgid)); time.sleep(.02)
                if proc.poll() is None or group_members(pgid):
                    signals.append('SIGKILL')
                    try: os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError: pass
            proc.wait(timeout=max(1, grace))
            # Kernel reaping can lag after SIGKILL; preserve residual state.
            end = time.monotonic() + min(grace, .5)
            while group_members(pgid) and time.monotonic() < end:
                time.sleep(.02)
        persist('RESIDUAL' if group_members(pgid) else 'RELEASED')
    return dict(argv=list(argv), pid=proc.pid, process_group=pgid, owned_pids=sorted(seen),
                started_at=started, ended_at=now(), elapsed_seconds=time.monotonic()-start,
                exit_code=proc.returncode, reason=reason or ('OWNERSHIP_RECORD_FAILED' if ownership_errors else None), signals=signals, ownership_errors=ownership_errors,
                process_release='RESIDUAL' if group_members(pgid) else 'RELEASED')
