"""Bounded ownership of one child process group; never signal external tasks."""
import os
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


def run_owned(argv, log, timeout, grace=2.0, env=None):
    if timeout <= 0 or grace < 0:
        raise ValueError('positive timeout and nonnegative grace required')
    started, start = now(), time.monotonic()
    reason, signals, seen = None, [], set()
    with Path(log).open('w') as output:
        proc = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        pgid = proc.pid
        seen.add(proc.pid)
        try:
            deadline = start + timeout
            while True:
                seen.update(group_members(pgid))
                code = proc.poll()
                if code is not None and not group_members(pgid):
                    break
                if time.monotonic() >= deadline:
                    reason = 'TIMEOUT'
                    break
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        except (KeyboardInterrupt, SystemExit):
            reason = 'INTERRUPTED'
        finally:
            # Main process may have exited while its descendants are still alive.
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
    return dict(argv=list(argv), pid=proc.pid, process_group=pgid, owned_pids=sorted(seen),
                started_at=started, ended_at=now(), elapsed_seconds=time.monotonic()-start,
                exit_code=proc.returncode, reason=reason, signals=signals,
                process_release='RESIDUAL' if group_members(pgid) else 'RELEASED')
