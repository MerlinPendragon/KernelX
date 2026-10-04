"""Nonblocking, cross-process ownership of a stable device identity."""
import fcntl
import hashlib
import os
import time
from pathlib import Path


class DeviceLock:
    def __init__(self, device_uid, directory='/tmp/kernelx-device-locks',timeout=0):
        root = Path(directory)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / (hashlib.sha256(device_uid.encode()).hexdigest() + '.lock')
        self.fd = None
        self.timeout = timeout

    def __enter__(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline=time.monotonic()+self.timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not self.timeout: raise RuntimeError('device already owned by another KernelX runner')
                    if time.monotonic()>=deadline: raise TimeoutError('control/import lock timeout')
                    time.sleep(min(.01,max(0,deadline-time.monotonic())))
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *args):
        # Never unlink the file: replacing its inode would let two runners lock it.
        # The native child inherits this fd so a killed supervisor cannot release
        # ownership while its benchmark is still alive.
        os.close(self.fd)
        self.fd = None
