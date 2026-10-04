import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kernelx.cann_adapter import CannAddAdapter
from kernelx.device_lock import DeviceLock
from kernelx.runner import collect


class RunnerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_visibility_reorder_subset_and_empty_rejected_before_probe(self):
        for index, visibility in enumerate(('7,6,5,4,3,2,1,0', '5,6', '')):
            with patch.dict(os.environ, {'ASCEND_RT_VISIBLE_DEVICES': visibility}), patch('kernelx.runner.probe') as probe:
                result = collect(self.root / str(index), server_id='unused', device=5,
                                 window_start='2020-01-01T00:00:00Z', window_end='2020-01-01T01:00:00Z',
                                 authorization_id='test')
            self.assertIn('ASCEND_RT_VISIBLE_DEVICES', result['reason'])
            probe.assert_not_called()
            self.assertFalse((self.root / str(index) / 'attempt.json').exists())

    def test_independent_cli_only_one_enters_preflight_and_benchmark(self):
        # Two actual CLI processes with CPU-only adapter/probe doubles share one
        # device identity. First remains in benchmark until explicitly released.
        code = r'''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
from kernelx import runner
from kernelx.__main__ import main
from kernelx.device_lock import DeviceLock
shared=Path(sys.argv[1]); sys.argv=sys.argv[2:]
def release(*args):
    with (shared/'preflight').open('a') as log: log.write('check\n')
    return dict(status='RELEASED',evidence=dict(stdout='No process in device.'))
def run(*args, **kwargs):
    assert 'ASCEND_RT_VISIBLE_DEVICES' not in kwargs['env']
    assert kwargs['pass_fds']
    (shared/'started').write_text('benchmark')
    while not (shared/'finish').exists(): time.sleep(.01)
    raise RuntimeError('mock benchmark finished')
env=json.loads(Path('tests/fixtures/910b1/environment.json').read_text())
with patch.object(runner,'probe',return_value=env), patch.object(runner.CannAddAdapter,'capabilities',return_value=dict(status='UNVERIFIED')), patch.object(runner.CannAddAdapter,'build',return_value=Path('/mock/binary')), patch.object(runner,'release_check',side_effect=release), patch.object(runner,'run_owned',side_effect=run), patch.object(runner,'DeviceLock',side_effect=lambda uid: DeviceLock(uid,shared/'locks')):
    main()
'''
        now = datetime.now(timezone.utc)
        def command(output):
            return [sys.executable, '-c', code, str(self.root), 'kernelx', 'collect-cann-add',
                    '--output', str(output), '--server-id', '55dcc47c-f8a8-4f3f-ab2d-c02bd385c470',
                    '--device', '5', '--window-start', (now-timedelta(seconds=10)).isoformat(),
                    '--window-end', (now+timedelta(seconds=60)).isoformat(), '--authorization-id', 'mock']
        env = dict(os.environ)
        env.pop('ASCEND_RT_VISIBLE_DEVICES', None)
        first = subprocess.Popen(command(self.root/'first'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        try:
            deadline = time.monotonic()+10
            while not (self.root/'started').exists() and first.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue((self.root/'started').exists())
            second = subprocess.run(command(self.root/'second'), capture_output=True, env=env, text=True, timeout=10)
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertIn('device already owned', json.loads(second.stdout)['reason'])
            self.assertEqual((self.root/'preflight').read_text().splitlines(), ['check'])
        finally:
            (self.root/'finish').touch()
            first.communicate(timeout=10)
        with DeviceLock(json.loads(Path('tests/fixtures/910b1/environment.json').read_text())['devices'][5]['device_uid'], self.root/'locks'):
            pass

    def test_lock_survives_parent_descriptor_close_until_child_exit(self):
        with DeviceLock('chip', self.root/'locks') as lock:
            child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], pass_fds=(lock.fd,))
        try:
            with self.assertRaises(RuntimeError):
                with DeviceLock('chip', self.root/'locks'): pass
        finally:
            child.terminate(); child.wait(timeout=5)
        with DeviceLock('chip', self.root/'locks'): pass

    def providers(self, names):
        self.home = self.root/'opt/cann'
        self.home.mkdir(parents=True, exist_ok=True)
        paths=[]
        for name in names:
            path=self.home/name; path.write_bytes(name.encode()); paths.append(str(path))
        path=self.root/'providers.json'
        path.write_text(json.dumps(dict(api_symbol='aclnnAdd', api_path=str(self.home/'libopapi.so'), loaded_libraries=paths)))
        with patch.dict(os.environ, {'ASCEND_HOME_PATH':str(self.home)}):
            adapter=CannAddAdapter()
        return adapter,path

    def test_custom_root_dependency_changes_fingerprint(self):
        adapter,path=self.providers(['libopapi.so','libascendcl.so','libnnopbase.so','libmsprofiler.so'])
        first=adapter.resolve_versions(path)
        self.assertEqual(first['status'],'VERIFIED_HOST_PROVIDER')
        (self.home/'libascendcl.so').write_bytes(b'changed runtime')
        second=adapter.resolve_versions(path)
        self.assertNotEqual(first['loaded_libraries_sha256'],second['loaded_libraries_sha256'])
        self.assertEqual(first['api_sha256'],second['api_sha256'])

    def test_missing_runtime_does_not_claim_verified_provider(self):
        adapter,path=self.providers(['libopapi.so'])
        result=adapter.resolve_versions(path)
        self.assertEqual(result['status'],'UNVERIFIED_HOST_PROVIDER')
        self.assertIn('libascendcl',result['missing_libraries'])
