import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kernelx.runner import collect, release_check, window_budget
from kernelx.probe import Collector
from kernelx.supervisor import group_members, run_owned

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def test_timeout_leaves_external_task_alive(self):
        external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            result=run_owned([sys.executable,'-c','import time;time.sleep(30)'],self.root/'log',.15,.1)
            self.assertEqual(result['reason'],'TIMEOUT');self.assertEqual(result['process_release'],'RELEASED')
            self.assertIsNone(external.poll());self.assertLess(result['elapsed_seconds'],2)
        finally: external.kill();external.wait()
    @unittest.skipUnless(Path('/proc').is_dir(),'Linux group membership requires /proc')
    def test_descendant_killed_when_parent_exits(self):
        code='import os,time,signal; pid=os.fork();\nif pid==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)\nelse:\n print(pid,flush=True)'
        result=run_owned([sys.executable,'-c',code],self.root/'log',.2,.1)
        child=int((self.root/'log').read_text().strip())
        self.assertEqual(result['reason'],'TIMEOUT');self.assertIn('SIGKILL',result['signals'])
        self.assertEqual(group_members(result['process_group']),[])
        self.assertIn(child,result['owned_pids'])
    def test_normal_exit_and_failure(self):
        for code in (0,4):
            result=run_owned([sys.executable,'-c','raise SystemExit('+str(code)+')'],self.root/'log',2)
            self.assertEqual(result['exit_code'],code);self.assertIsNone(result['reason'])
    def test_no_early_or_soft_cutoff_start(self):
        self.assertEqual(window_budget(100,200,3,clock=110),87)
        for clock in (99,197,200,201):
            with self.assertRaises(ValueError): window_budget(100,200,3,clock=clock)
        with self.assertRaises(ValueError): window_budget(100,200,1,clock=110)
    def test_rejected_window_has_failure_record_and_no_attempt(self):
        result=collect(self.root/'run',server_id='00000000-0000-4000-8000-000000000001',device=5,window_start='2020-01-01T00:00:00Z',window_end='2020-01-01T01:00:00Z',authorization_id='expired')
        self.assertFalse(result['valid']);self.assertEqual(result['observations'],0)
        self.assertTrue((self.root/'run/failure.json').is_file());self.assertFalse((self.root/'run/attempt.json').exists())
        with self.assertRaises(FileExistsError): collect(self.root/'run')
    def test_release_status_not_inferred_from_unknown_format(self):
        from unittest.mock import patch
        c=Collector('00000000-0000-4000-8000-000000000001')
        for text,expected in [('No process in device.','RELEASED'),('Process ID: 123','RESIDUAL'),('Process ID: 999','RELEASED'),('unrecognized table','UNKNOWN')]:
            with patch.object(c,'run',return_value=c.record(['fixture'],text)):
                self.assertEqual(release_check(5,[123],c)['status'],expected)

if __name__=='__main__': unittest.main()
