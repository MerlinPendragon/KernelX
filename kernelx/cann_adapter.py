"""Frozen ACLNN Add adapter and latency-v1 ACL profiling preset."""
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Protocol

from .protocol import case_key, digest
from .supervisor import run_owned

PRESET = dict(name='latency-v1', collector='aclprof', data_type_mask=0x83,
              modules=['ACL_PROF_TASK_TIME','ACL_PROF_ACL_API','ACL_PROF_MSPROFTX'],
              aicore_metrics='ACL_AICORE_NONE', attribution='synchronous-msproftx-range-v1')

class LibraryAdapter(Protocol):
    def capabilities(self): ...
    def enumerate_cases(self): ...
    def build(self, output): ...
    def resolve_versions(self, providers): ...
    def prepare(self, device, warmup, repeats, raw, sidecar): ...
    def benchmark_command(self, binary, device, warmup, repeats, raw, sidecar): ...
    def attribution_spec(self): ...
    def metric_spec(self): ...

class CannAddAdapter:
    def __init__(self):
        self.base = Path(__file__).parent
        self.manifest = json.loads((self.base/'manifests/cann_add.json').read_text())
        if case_key(self.manifest['case']) != self.manifest['case_key']:
            raise ValueError('frozen manifest case key mismatch')
        self.home = Path(os.environ.get('ASCEND_HOME_PATH') or os.environ.get('ASCEND_TOOLKIT_HOME') or '/usr/local/Ascend/ascend-toolkit/latest').resolve()
        self.msprof = shutil.which('msprof') or str(self.home/'tools/profiler/bin/msprof')

    def capabilities(self):
        missing = [str(self.home/p) for p in ('include/aclnnop/aclnn_add.h','include/acl/acl_prof.h','lib64/libopapi.so','lib64/libnnopbase.so','lib64/libmsprofiler.so') if not (self.home/p).is_file()]
        if not shutil.which('c++'): missing.append('c++')
        if not Path(self.msprof).is_file(): missing.append('msprof')
        return dict(status='UNVERIFIED' if not missing else 'UNSUPPORTED', reason='requires actual benchmark and attribution validation' if not missing else 'missing: '+', '.join(missing), preset=PRESET)

    def enumerate_cases(self): return [self.manifest['case']]
    def attribution_spec(self): return dict(operator='Add', tasks_per_invocation=1, boundary='profiling starts after warmup; unique synchronous msproftx range')
    def metric_spec(self): return dict(task_duration_us='single matched task', device_span_us='matched task end minus start', host_elapsed_us='monotonic launch to stream synchronization under profiler')

    def build(self, output, environment=None):
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        cache=os.environ.get('KERNELX_NATIVE_CACHE_ROOT')
        if cache:
            if environment is None:raise ValueError('actual environment snapshot required for cached build')
            from .build_cache import cached_build
            return cached_build(self,output,environment,cache,self._compile)
        binary=self._compile(output)
        data=json.loads((output/'build.json').read_text());data.update(cache_state='DISABLED',cache_key=None,compile_elapsed_seconds=data['elapsed_seconds'])
        (output/'build.json').write_text(json.dumps(data,indent=2)+'\n')
        return binary

    def _compile(self, output):
        output=Path(output); output.mkdir(parents=True, exist_ok=True)
        binary=output/'cann_add'
        argv=['c++','-std=c++17','-O2',str(self.base/'native/cann_add.cpp'),'-I'+str(self.home/'include'),'-L'+str(self.home/'lib64'),'-Wl,-rpath,'+str(self.home/'lib64'),'-lopapi','-lnnopbase','-lascendcl','-lmsprofiler','-ldl','-o',str(binary)]
        result=run_owned(argv,output/'build.log',120)
        result.update(source_sha256=hashlib.sha256((self.base/'native/cann_add.cpp').read_bytes()).hexdigest(), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest() if binary.exists() else None)
        (output/'build.json').write_text(json.dumps(result,indent=2)+'\n')
        if result['exit_code'] != 0 or result['reason']: raise RuntimeError('build failed; see build.log')
        return binary

    def prepare(self, device, warmup, repeats, raw, sidecar):
        # Preparation happens in the native process before aclprofStart; never
        # split input/JIT initialization across environments or PID contexts.
        return dict(device=device,warmup=warmup,repeats=repeats,raw=str(raw),sidecar=str(sidecar),preset_sha256=digest(PRESET))

    def benchmark_command(self,binary,device,warmup,repeats,raw,sidecar):
        if not 1 <= warmup <= 1000 or not 1 <= repeats <= 1000: raise ValueError('bounded warmup/repeats required')
        return [str(binary),str(device),str(warmup),str(repeats),str(raw),str(sidecar)]

    def resolve_versions(self, providers):
        data=json.loads(Path(providers).read_text())
        # Preserve every mapped shared library, including dependencies outside the
        # installation root. Directory spelling is not evidence of ownership.
        libraries, unavailable = [], []
        paths = sorted({str(Path(path).resolve()) for path in data['loaded_libraries']})
        for path in paths:
            try:
                libraries.append(dict(path=path,sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))
            except OSError as exc:
                unavailable.append(dict(path=path,reason=str(exc)))
        api=Path(data['api_path']).resolve()
        api_sha256=next((item['sha256'] for item in libraries if item['path']==str(api)),None)
        required={'libopapi','libnnopbase','libmsprofiler','libascendcl'}
        names={Path(item['path']).name.split('.so')[0] for item in libraries}
        missing=sorted(required-names)
        if api_sha256 is None: missing.append('mapped API provider')
        verified=not missing and not unavailable
        return dict(api_symbol=data['api_symbol'],api_path=str(api),api_sha256=api_sha256,
                    loaded_libraries=libraries,loaded_libraries_sha256=digest(libraries),
                    missing_libraries=missing,unavailable_libraries=unavailable,
                    status='VERIFIED_HOST_PROVIDER' if verified else 'UNVERIFIED_HOST_PROVIDER',
                    device_kernel_status='DECLARED_ONLY: profiler kernel identity observed; device binary load path not proven')
