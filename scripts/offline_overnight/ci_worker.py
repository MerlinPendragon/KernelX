"""Supervised upstream CI execution. Traces include references and cold setup.

These are diagnostic CI traces, deliberately not latency-v1 observations.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import runpy
import sys
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--spec',type=Path,required=True)
    spec=json.loads(parser.parse_args().spec.read_text())
    output=Path(spec['output']);raw=output/'raw';raw.mkdir()
    os.environ['TK_TEST_LEVEL']=str(spec['test_level'])
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    import torch
    import torch_npu
    torch.npu.set_device(spec['device'])
    # Force framework initialization before starting the diagnostic trace.
    init=torch.ones(1,device='npu');torch.npu.synchronize();del init
    acl=ctypes.CDLL(str(Path(os.environ['ASCEND_HOME_PATH'])/'lib64/libmsprofiler.so'))
    acl.aclprofCreateConfig.restype=ctypes.c_void_p
    acl.aclprofCreateConfig.argtypes=[ctypes.POINTER(ctypes.c_uint32),ctypes.c_uint32,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint64]
    acl.aclprofInit.argtypes=[ctypes.c_char_p,ctypes.c_size_t]
    acl.aclprofFinalize.argtypes=[]
    for name in ('aclprofStart','aclprofStop','aclprofDestroyConfig'):
        getattr(acl,name).argtypes=[ctypes.c_void_p]
    acl.aclprofCreateStamp.argtypes=[];acl.aclprofCreateStamp.restype=ctypes.c_void_p
    acl.aclprofDestroyStamp.argtypes=[ctypes.c_void_p];acl.aclprofDestroyStamp.restype=None
    acl.aclprofSetStampTraceMessage.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
    acl.aclprofRangeStart.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
    acl.aclprofRangeStop.argtypes=[ctypes.c_uint32]
    def checked(result):
        if result!=0: raise RuntimeError('ACL profiler error: '+str(result))
    path=str(raw.resolve()).encode();checked(acl.aclprofInit(path,len(path)))
    device=(ctypes.c_uint32*1)(spec['device'])
    config=acl.aclprofCreateConfig(device,1,255,None,0x83)
    if not config: raise RuntimeError('ACL profiling configuration failed')
    def append(record):
        with (output/'tests.jsonl').open('a') as handle:
            handle.write(json.dumps(record,ensure_ascii=False)+'\n');handle.flush()
    def encode(value):
        if isinstance(value,(str,int,float,bool)) or value is None: return value
        if isinstance(value,(tuple,list)): return [encode(x) for x in value]
        if isinstance(value,dict): return {str(k):encode(v) for k,v in value.items()}
        if isinstance(value,torch.Tensor): return dict(shape=list(value.shape),dtype=str(value.dtype),stride=list(value.stride()))
        return str(value)
    source=Path(spec['path'])
    # Tests import sibling helpers; installed operator packages remain the providers.
    sys.path.insert(0,str(source.parent))
    checked(acl.aclprofStart(config))
    code=1
    try:
        if spec['mode']=='script':
            # Preserve upstream main() enumeration including orientations/grouped cases.
            sys.argv=[str(source),'--skip-prof'] # avoid nested upstream msprof sessions
            try:
                runpy.run_path(str(source),run_name='__main__')
                code=0
            except SystemExit as exit_code:
                code=exit_code.code if isinstance(exit_code.code,int) else 1
            append(dict(nodeid=spec['relative_path']+'::__main__',phase='call',outcome='passed' if code==0 else 'failed',
                        scope='all upstream main enumeration; inspect source for internal shape loops'))
        else:
            import pytest
            class Plugin:
                def pytest_collection_modifyitems(self,session,config,items):
                    for item in items:
                        params=getattr(getattr(item,'callspec',None),'params',{})
                        append(dict(nodeid=item.nodeid,phase='collected',parameters=encode(params)))
                def pytest_runtest_setup(self,item):
                    if time.time()>=spec['deadline_utc']:
                        pytest.exit('CI budget expired',returncode=2)
                @pytest.hookimpl(hookwrapper=True)
                def pytest_runtest_call(self,item):
                    torch.npu.synchronize()
                    stamp=acl.aclprofCreateStamp()
                    if not stamp: raise RuntimeError('ACL stamp allocation failed')
                    name=('kernelx:ci:'+item.nodeid).encode()[:2048]
                    checked(acl.aclprofSetStampTraceMessage(stamp,name,len(name)))
                    range_id=ctypes.c_uint32();checked(acl.aclprofRangeStart(stamp,ctypes.byref(range_id)))
                    try:
                        yield
                    finally:
                        try:
                            torch.npu.synchronize();checked(acl.aclprofRangeStop(range_id))
                        finally: acl.aclprofDestroyStamp(stamp)
                def pytest_runtest_logreport(self,report):
                    append(dict(nodeid=report.nodeid,phase=report.when,outcome=report.outcome,duration_seconds=report.duration,
                                reason=str(report.longrepr)[:8000] if report.failed or report.skipped else None))
            code=int(pytest.main([str(source),'-q','--continue-on-collection-errors'],plugins=[Plugin()]))
    finally:
        try:
            torch.npu.synchronize();checked(acl.aclprofStop(config))
        finally:
            try: checked(acl.aclprofDestroyConfig(config))
            finally: checked(acl.aclprofFinalize())
        maps=Path('/proc/self/maps')
        if maps.exists():
            files=sorted({line[line.find('/'):].strip() for line in maps.read_text().splitlines() if '/' in line and '.so' in line})
            (output/'providers.json').write_text(json.dumps(dict(torch=torch.__version__,torch_npu=getattr(torch_npu,'__version__',None),
                loaded_files=files,source_commit=spec['source_commit'],installed_version=spec['installed_version'],load_status='DECLARED_ONLY',
                metric_scope='CI_TEST_TRACE; includes cold calls and references; no isolated latency claim'),indent=2))
    return code

if __name__=='__main__':
    sys.exit(main())
