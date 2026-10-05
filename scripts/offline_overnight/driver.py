#!/usr/bin/env python3
"""Single-file offline overnight Ascend profiling, default eight hours.
Requires installed CANN; broad coverage also requires installed torch/torch_npu.
No online downloads, no automatic dependency installation.
"""
import argparse
import base64
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import sys
import tempfile
import time
import uuid
import zipfile

PAYLOAD_SHA256 = '__SHA256__'
SOURCE_COMMIT = '__COMMIT__'
PAYLOAD = '''__PAYLOAD__'''
MODEL_SOURCES = {
    'Qwen3-8B': 'https://huggingface.co/Qwen/Qwen3-8B/raw/main/config.json',
    'Qwen3.5-35B-A3B': 'https://huggingface.co/Qwen/Qwen3.5-35B-A3B/raw/main/config.json',
    'Qwen3.5-27B': 'https://huggingface.co/Qwen/Qwen3.5-27B/raw/main/config.json',
    'DeepSeek-V3': 'https://huggingface.co/deepseek-ai/DeepSeek-V3/raw/main/config.json',
}

def tensor(shape, dtype='bfloat16'):
    stride = []
    value = 1
    for size in reversed(shape):
        stride.insert(0, value)
        value *= size
    return dict(shape=list(shape), dtype=dtype, layout='ND', stride=stride)


def make_case(library, operator, inputs, outputs, attrs, model, dtype='bfloat16'):
    from kernelx.protocol import case_key, validate
    case = dict(schema_version=1, protocol_version='latency-v1',
                extensions=dict(library=library, offline_diagnostic=True, model_anchor=model,
                                model_source=MODEL_SOURCES.get(model), numerical_correctness='NOT_CHECKED'),
                operator=operator, inputs=[tensor(s, dtype) for s in inputs],
                outputs=[tensor(s, dtype) for s in outputs], attributes=attrs,
                input_generation=dict(algorithm='seeded-ones-v1', version='1', seed=0, input_sha256=None),
                semantic_version='1')
    validate('case', case)
    # Conservative allocation estimate including outputs, masks and workspaces.
    estimate = 3 * sum(math.prod(s['shape']) * (4 if s['dtype']=='float32' else 2) for s in case['inputs']+case['outputs'])
    return dict(library=library, case=case, case_key=case_key(case), estimated_bytes=estimate)


def candidates():
    cases = []
    tokens = (1, 8, 32, 128, 512, 2048, 8192)
    anchors = [('Qwen3.5-35B-A3B',2048), ('Qwen3-8B',4096), ('Qwen3.5-27B',5120), ('DeepSeek-V3',7168), ('representative-wide',8192)]
    projections = [
        ('Qwen3.5-35B-A3B',1024,2048), # fused expert gate+up, not grouped MoE
        ('Qwen3.5-35B-A3B',2048,512),
        ('Qwen3-8B',6144,4096),         # QKV projection
        ('Qwen3-8B',24576,4096),        # fused gate+up
        ('Qwen3-8B',4096,12288),
        ('Qwen3.5-27B',34816,5120),
        ('Qwen3.5-27B',5120,17408),
        ('DeepSeek-V3',4096,7168),      # fused expert gate+up
        ('DeepSeek-V3',7168,2048),
        ('DeepSeek-V3',1536,7168),      # MLA query compression component
        ('DeepSeek-V3',576,7168),       # MLA KV compression + rotary component
    ]
    def add(lib, op, inputs, outputs, attrs, model, dtype='bfloat16'):
        cases.append(make_case(lib,op,inputs,outputs,attrs,model,dtype))
    for m in tokens:
        for model, h in anchors:
            for dtype in ('bfloat16','float16'):
                add('torch-npu','Add',[(m,h),(m,h)],[(m,h)],dict(alpha=1),model,dtype)
                add('torch-npu','RMSNorm',[(m,h),(h,)],[(m,h),(m,1)],dict(epsilon=1e-6),model,dtype)
            add('sgl-kernel-npu','fused-sigmoid-mul',[(m,h),(m,h)],[(m,h)],{},model)
            row = make_case('tile-kernels','per-token-cast',[(m,h)],[(m,h),(m,h//32)],dict(fmt='e4m3',num_per_channels=32,use_packed_ue8m0=False,round_sf=False),model)
            row['case']['outputs'][0]['dtype'] = 'float8_e4m3fn'
            row['case']['outputs'][1]['dtype'] = 'float32'
            from kernelx.protocol import case_key
            row['case_key'] = case_key(row['case'])
            cases.append(row)
        for model, n, k in projections:
            for dtype in ('bfloat16','float16'):
                add('torch-npu','matmul-nt',[(m,k),(n,k)],[(m,n)],{},model,dtype)
            add('deepgemm-ascend','bf16-gemm-nt',[(m,k),(n,k)],[(m,n)],{},model)
        for model, h in [('Qwen3.5-35B-A3B',512),('Qwen3-8B',12288),('Qwen3.5-27B',17408),('DeepSeek-V3',2048),('DeepSeek-V3',18432)]:
            for dtype in ('bfloat16','float16'):
                add('torch-npu','SwiGLU',[(m,2*h)],[(m,h)],dict(dim=-1),model,dtype)
        for model in ('Qwen3.5-35B-A3B','DeepSeek-V3'):
            add('torch-npu','sigmoid-topk-router-component',[(m,256)],[(m,8),(m,8)],dict(topk=8),model)
            cases[-1]['case']['outputs'][1]['dtype'] = 'int64'
            cases[-1]['case_key'] = case_key(cases[-1]['case'])
    # Full-attention components, not complete hybrid/MLA model execution.
    for model, heads, kv_heads, dim in [('Qwen3-8B',32,8,128),('Qwen3.5-35B-A3B',16,2,256),('Qwen3.5-27B',24,4,256)]:
        shapes = [(1,s,s) for s in (128,512,2048,8192)]
        shapes += [(b,q,s) for b in (1,4,8) for q in (1,8) for s in (128,1024,8192,32768)]
        for batch, sq, skv in shapes:
            for dtype in ('bfloat16','float16'):
                add('torch-npu','GQA-Attention',[(batch,heads,sq,dim),(batch,kv_heads,skv,dim),(batch,kv_heads,skv,dim)],[(batch,heads,sq,dim)],
                    dict(batch=batch,heads=heads,kv_heads=kv_heads,head_dim=dim,query_tokens=sq,kv_tokens=skv,causal='bottom-right'),model,dtype)
        for m in tokens:
            # Full-dimension rotary component. Qwen3.5 partial rotary is NOT represented.
            if model == 'Qwen3-8B':
                add('torch-npu','RotaryMul',[(1,m,heads,dim),(1,m,1,dim),(1,m,1,dim)],[(1,m,heads,dim)],dict(rotary_dim=dim),model)
    for model, channels in [('Qwen3.5-35B-A3B',8192),('Qwen3.5-27B',10240)]:
        for m in tokens:
            add('torch-npu','causal-depthwise-conv1d-component',[(1,channels,m),(channels,1,4)],[(1,channels,m)],dict(channels=channels,kernel=4),model)
    # Interleave by backend AND entry so small ops cannot crowd out GEMM/attention.
    queues = defaultdict(deque)
    seen = set()
    for row in cases:
        identity = (row['library'],row['case_key'])
        if identity not in seen:
            seen.add(identity)
            queues[(row['library'],row['case']['operator'])].append(row)
    balanced = []
    while any(queues.values()):
        for key in sorted(queues):
            if queues[key]: balanced.append(queues[key].popleft())
    return balanced


class DiagnosticAdapter:
    def __init__(self, row):
        from kernelx.cann_adapter import CannAddAdapter
        self.row = row
        self.adapter = self
        self.manifest = dict(manifest_version=1,library_id=row['library'],implementation=row['case']['operator'],case_key=row['case_key'],case=row['case'])
        native = CannAddAdapter()
        self.msprof, self.home = native.msprof, native.home
    def capabilities(self,*args):
        return dict(status='UNVERIFIED',reason='offline diagnostic; runtime/JIT provider certification is not implemented')
    def build(self, output, environment=None):
        from kernelx.protocol import digest
        output = Path(output)
        output.mkdir(parents=True,exist_ok=True)
        import kernelx.offline_benchmark as worker
        source_hash = hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()
        (output/'adapter.json').write_text(json.dumps(self.manifest,indent=2))
        (output/'build.json').write_text(json.dumps(dict(source_sha256=source_hash,binary_sha256=digest(self.manifest),cache_state='UPSTREAM_JIT',cache_key=None,elapsed_seconds=0,compile_elapsed_seconds=None)))
        return output/'adapter.json'
    def prepare(self,device,warmup,repeats,raw,sidecar):
        from kernelx.cann_adapter import PRESET
        if type(warmup) is not int or type(repeats) is not int or not 1<=warmup<=1000 or not 1<=repeats<=1000:
            raise ValueError('bounded repeat policy required')
        # Input allocation, extension loading and JIT stay in the worker before
        # profiling; this is the preparation record shared with its launch spec.
        return dict(library=self.row['library'],case=self.row,device=device,warmup=warmup,repeats=repeats,raw=str(raw),sidecar=str(sidecar),rank=0,preset=PRESET)
    def benchmark_command(self,binary,device,warmup,repeats,raw,sidecar):
        spec = self.prepare(device,warmup,repeats,raw,sidecar)
        path = Path(binary).parent/'benchmark-spec.json'
        path.write_text(json.dumps(spec,indent=2))
        return [sys.executable,'-B','-m','kernelx.offline_benchmark','--spec',str(path)]
    def resolve_versions(self,path):
        from kernelx.protocol import digest
        data = json.loads(Path(path).read_text())
        files = []
        for filename in sorted(set(data.get('loaded_files',[]))):
            p = Path(filename)
            files.append(dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None))
        return dict(status='DECLARED_ONLY',load_status='DECLARED_ONLY',certification_status='UNIMPLEMENTED',
                    library=self.row['library'],api_path=None,api_sha256=None,loaded_files=files,
                    loaded_libraries_sha256=digest(files),framework=data.get('framework'),version=data.get('version'),numerical_correctness='NOT_CHECKED')


def json_write(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


def diagnostic_self_test(matrix):
    checked=[]
    with tempfile.TemporaryDirectory(prefix='kernelx-adapter-check-') as temp:
        root=Path(temp)
        for library in sorted({row['library'] for row in matrix}):
            row=next(row for row in matrix if row['library']==library)
            adapter=DiagnosticAdapter(row)
            for name in ('capabilities','build','prepare','benchmark_command','resolve_versions'):
                if not callable(getattr(adapter,name,None)):
                    raise RuntimeError('diagnostic adapter missing '+name)
            prepared=adapter.prepare(0,20,30,root/'raw',root/'sidecar.jsonl')
            command=adapter.benchmark_command(root/'adapter.json',0,20,30,root/'raw',root/'sidecar.jsonl')
            if json.loads(Path(command[-1]).read_text())!=prepared:
                raise RuntimeError('preparation and worker spec differ')
            checked.append(library)
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device',type=int,default=0)
    parser.add_argument('--hours',type=float,default=8)
    parser.add_argument('--data-dir',type=Path,default=Path('kernelx-night'))
    parser.add_argument('--server-id')
    parser.add_argument('--libraries',default='cann-opp,torch-npu,ops-nn,ops-transformer,sgl-kernel-npu,tile-kernels,deepgemm-ascend,deepep-ascend')
    parser.add_argument('--ci-hours',type=float,default=4,help='maximum CI portion within the total eight-hour budget; zero disables upstream tests')
    parser.add_argument('--ci-level',type=int,choices=(0,1,2),default=2,help='TileKernels upstream test level: core/default/full')
    parser.add_argument('--ci-file-timeout',type=float,default=600)
    parser.add_argument('--ci-root',action='append',default=[],help='existing version-matched source tree, LIBRARY=/path')
    parser.add_argument('--inventory',action='store_true',help='CPU-only installed versions and CI matching report')
    parser.add_argument('--warmup',type=int,default=20)
    parser.add_argument('--repeats',type=int,default=30)
    parser.add_argument('--case-timeout',type=float,default=300,help='per-case benchmark/JIT timeout seconds')
    parser.add_argument('--max-case-gib',type=float,default=4)
    parser.add_argument('--reserve-gib',type=float,default=5)
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--list-cases',action='store_true',help='CPU-only candidate matrix; missing libraries filtered only during real run')
    args = parser.parse_args()
    if sys.version_info<(3,9): parser.error('Python >= 3.9 required')
    if not math.isfinite(args.hours) or args.hours<=0 or args.device<0 or not 1<=args.warmup<=1000 or not 1<=args.repeats<=1000:
        parser.error('positive hours, nonnegative device, warmup/repeats in 1..1000 required')
    if not math.isfinite(args.ci_hours) or args.ci_hours<0: parser.error('ci-hours must be finite and nonnegative')
    if any(not math.isfinite(x) or x<=0 for x in (args.case_timeout,args.ci_file_timeout,args.max_case_gib,args.reserve_gib)):
        parser.error('timeouts and memory/disk budgets must be finite and positive')
    requested = set(args.libraries.split(','))
    known = {'cann-opp','torch-npu','sgl-kernel-npu','tile-kernels','deepgemm-ascend','ops-nn','ops-transformer','deepep-ascend'}
    if requested-known: parser.error('unknown libraries: '+','.join(sorted(requested-known)))
    payload = base64.b64decode(PAYLOAD)
    if hashlib.sha256(payload).hexdigest()!=PAYLOAD_SHA256: raise RuntimeError('embedded payload checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='kernelx-night-') as runtime:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for entry in archive.infolist():
                p = Path(entry.filename)
                if p.is_absolute() or '..' in p.parts or p.parts[0]!='kernelx': raise RuntimeError('invalid embedded archive')
            archive.extractall(runtime)
        sys.path.insert(0,runtime)
        sys.dont_write_bytecode=True
        args.ci_roots={}
        for item in args.ci_root:
            if '=' not in item: parser.error('--ci-root requires LIBRARY=/path')
            library,path=item.split('=',1)
            if library not in known or not path: parser.error('invalid --ci-root')
            args.ci_roots[library]=path
        if args.inventory:
            from kernelx.offline_ci import discover
            bindings=discover(runtime,requested,args.ci_roots)
            print(json.dumps([{k:v for k,v in b.items() if k!='inventory'} for b in bindings],ensure_ascii=False,indent=2))
            return 0
        matrix = [r for r in candidates() if r['library'] in requested and r['estimated_bytes']<=args.max_case_gib*2**30]
        from kernelx.protocol import validate
        for row in matrix: validate('case',row['case'])
        if args.self_test:
            from kernelx.release import self_test
            from kernelx.offline_ci import source_inventory
            ci_root=Path(runtime)/'kernelx/offline_ci_sources'
            ci_files={p.name:len(source_inventory(p,p.name)) for p in ci_root.iterdir() if p.is_dir()}
            print(json.dumps(dict(**self_test(),candidate_cases=len(matrix),by_library=dict(Counter(r['library'] for r in matrix)),diagnostic_adapters_checked=diagnostic_self_test(matrix),embedded_ci_files=ci_files,source_commit=SOURCE_COMMIT),indent=2))
            return 0
        if args.list_cases:
            print(json.dumps(matrix,ensure_ascii=False,indent=2))
            return 0
        if sys.platform!='linux': parser.error('real profiling requires Linux/Ascend')
        if any(name in os.environ for name in ('ASCEND_RT_VISIBLE_DEVICES','ASCEND_VISIBLE_DEVICES')):
            parser.error('confirm direct IDs, then unset ASCEND_RT_VISIBLE_DEVICES and ASCEND_VISIBLE_DEVICES')
        from kernelx.device_lock import DeviceLock
        data = args.data_dir.resolve()
        data.mkdir(parents=True,exist_ok=True)
        # Same data-dir cannot be launched twice. Runner also locks the actual device.
        with DeviceLock('offline-overnight-session',data/'.locks'):
            return run(args,parser,data,runtime,matrix,requested)


def run(args,parser,data,runtime,matrix,requested):
    import kernelx.runner as runner
    import kernelx.libraries as libraries
    import kernelx.cann_adapter as cann_adapter
    from kernelx.probe import Collector, probe
    started = datetime.now(timezone.utc)
    hard_end = started+timedelta(hours=args.hours)
    deadline = time.monotonic()+args.hours*3600
    stop_at = deadline-60  # cleanup, final evidence and metadata reserve
    interrupted = [False]
    previous = {}
    for sig in (signal.SIGTERM,signal.SIGINT):
        previous[sig] = signal.signal(sig,lambda *_: interrupted.__setitem__(0,True))
    # Every probe/build/benchmark/export command obeys the same monotonic budget.
    original_run = runner.run_owned
    original_probe_run = Collector.run
    def bounded_run(argv,log,timeout,**kwargs):
        remaining = stop_at-time.monotonic()
        if remaining<=0: raise RuntimeError('overnight command budget exhausted')
        original_cancel = kwargs.pop('cancel',None)
        kwargs['cancel'] = lambda: interrupted[0] or time.monotonic()>=stop_at or (original_cancel is not None and original_cancel())
        execution=original_run(argv,log,min(timeout,remaining),**kwargs)
        if execution.get('reason')=='INTERRUPTED': interrupted[0]=True
        return execution
    def bounded_probe(self,argv):
        remaining = deadline-time.monotonic()-5
        if remaining<=0: raise RuntimeError('overnight probe budget exhausted')
        saved = self.timeout
        self.timeout = min(saved,remaining)
        try: return original_probe_run(self,argv)
        finally: self.timeout=saved
    runner.run_owned = bounded_run
    cann_adapter.run_owned = bounded_run
    Collector.run = bounded_probe
    os.environ.pop('KERNELX_NATIVE_CACHE_ROOT',None)
    # Native children resolve the exact embedded snapshot; no external repo required.
    os.environ['PYTHONPATH'] = runtime+(os.pathsep+os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    home = cann_adapter.CannAddAdapter().home
    os.environ['ASCEND_HOME_PATH']=str(home)
    identity = data/'server-id'
    server_id = str(uuid.UUID(args.server_id)) if args.server_id else (identity.read_text().strip() if identity.exists() else str(uuid.uuid4()))
    server_id = str(uuid.UUID(server_id))
    if identity.exists() and identity.read_text().strip()!=server_id: parser.error('server UUID differs from persisted identity')
    if not identity.exists(): identity.write_text(server_id+'\n')
    session = data/('night-'+started.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    session.mkdir()
    skip = {}
    packages = {'torch-npu':('torch','torch_npu'),'sgl-kernel-npu':('torch','torch_npu','sgl_kernel_npu'),
                'tile-kernels':('torch','torch_npu','tile_kernels'),'deepgemm-ascend':('torch','torch_npu','deep_gemm')}
    for library in requested:
        if library in ('ops-nn','ops-transformer'): skip[library]='ADAPTER_UNCONFIGURED: no independent offline entry; CANN dispatch is not library certification'
        elif library=='deepep-ascend': skip[library]='requires explicitly coordinated multi-rank group; not covered by this single-device collector'
        elif library in packages:
            missing=[]
            for package in packages[library]:
                try: present=importlib.util.find_spec(package) is not None
                except (ImportError,ValueError): present=False
                if not present: missing.append(package)
            if missing: skip[library]='NOT_INSTALLED: '+','.join(missing)
    from kernelx.offline_ci import discover, run_suites, shape_cases
    bindings=discover(runtime,requested,args.ci_roots)
    json_write(session/'ci-shape-inventory.json',bindings)
    ci_cases=shape_cases(bindings,make_case)
    matrix=ci_cases+matrix
    seen=set()
    filtered=[]
    filtered_cases=[]
    for row in matrix:
        key=(row['library'],row['case_key'])
        if row['estimated_bytes']>args.max_case_gib*2**30:
            filtered_cases.append(dict(library=row['library'],case_key=row['case_key'],reason='MEMORY_ESTIMATE_EXCEEDS_BUDGET'));continue
        if key not in seen and row['library'] not in skip:
            seen.add(key);filtered.append(row)
    matrix=filtered
    if 'cann-opp' in requested:
        matrix.insert(0,dict(library='cann-opp',case=cann_adapter.CannAddAdapter().manifest['case'],case_key=cann_adapter.CannAddAdapter().manifest['case_key'],estimated_bytes=65536))
    snapshot = probe(server_id)
    json_write(session/'environment.json',snapshot)
    targets = [d for d in snapshot['devices'] if d['logical_id']==args.device]
    if len(targets)!=1 or targets[0]['npu_id']!=args.device or targets[0]['chip_id']!=0:
        mappings = [{key:d[key] for key in ('logical_id','npu_id','chip_id')} for d in snapshot['devices']]
        raise RuntimeError('device '+str(args.device)+' requires one direct mapping (logical_id == npu_id, chip_id == 0); parsed='+str(mappings)+'; see '+str(session/'environment.json'))
    device_uid = targets[0]['device_uid']
    json_write(session/'matrix.json',dict(source_commit=SOURCE_COMMIT,payload_sha256=PAYLOAD_SHA256,
               model_sources=MODEL_SOURCES,selected_cases=matrix,skipped_libraries=skip,filtered_cases=filtered_cases,
               limitations=['diagnostic provider only; no extension/JIT certification','numerical correctness NOT_CHECKED',
                            'no full GatedDeltaNet, grouped MoE, MLA attention, TP or DeepEP communication',
                            'estimated case memory is not a hard runtime HBM limit']))
    ledger = session/'attempts.jsonl'
    summary = dict(state='RUNNING',started_at=started.isoformat(),deadline=hard_end.isoformat(),device=args.device,
                   server_id=server_id,session=str(session),planned_unique_cases=len(matrix),skipped_libraries=skip,
                   attempts=0,successful_attempts=0,covered_unique_cases=0,round=0)
    successes = set()
    attempted = set()
    failures = Counter()
    disabled_libraries = set()
    consecutive_invalid = 0
    csv_path = session/'latencies.csv'
    fields = ['round','library','operator','model','dtype','input_shapes','valid','device_release','device_span_median_us','device_span_p95_us','host_elapsed_median_us','output','reason']
    print('Output: '+str(session),flush=True)
    print('Deadline: '+hard_end.isoformat(),flush=True)
    print('Cases: '+str(len(matrix))+'; skipped: '+json.dumps(skip,ensure_ascii=False),flush=True)
    json_write(session/'summary.json',summary)
    ci_report=run_suites(args,bindings,session,server_id,device_uid,deadline,interrupted,bounded_run)
    summary['ci']=ci_report
    json_write(session/'summary.json',summary)
    if ci_report.get('stop_reason')=='DEVICE_RELEASE_UNCONFIRMED':
        summary.update(state='STOPPED',stop_reason='DEVICE_RELEASE_UNCONFIRMED')
        json_write(session/'summary.json',summary)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return 1
    reason = 'BUDGET_COMPLETE'
    try:
        with csv_path.open('w',newline='') as csv_file:
            writer=csv.DictWriter(csv_file,fieldnames=fields)
            writer.writeheader()
            while True:
                active = [r for r in matrix if r['library'] not in disabled_libraries and failures[(r['library'],r['case_key'])]<2]
                if not active:
                    reason='NO_RUNNABLE_CASES';break
                # First cover untouched candidates, then revisit successful cases.
                active.sort(key=lambda r:(r['library'],r['case_key']) in attempted)
                summary['round']+=1
                for row in active:
                    if interrupted[0]: reason='INTERRUPTED';break
                    if stop_at-time.monotonic()<60: reason='BUDGET_COMPLETE';break
                    if shutil.disk_usage(data).free<args.reserve_gib*2**30: reason='DISK_RESERVE_REACHED';break
                    library=row['library'];case=row['case'];key=(library,row['case_key'])
                    output=session/('run-%06d'%(summary['attempts']+1))
                    if library!='cann-opp':
                        libraries.FrozenPerformanceAdapter=lambda *_: DiagnosticAdapter(row)
                    print('Round %d case %d: %s %s %s'%(summary['round'],summary['attempts']+1,library,case['operator'],[x['shape'] for x in case['inputs']]),flush=True)
                    remaining=stop_at-time.monotonic()
                    kwargs = dict(server_id=server_id,device=args.device,expected_device_uid=device_uid,
                                  authorization_id='exclusive-overnight-'+session.name,
                                  window_start=started.isoformat(),window_end=hard_end.isoformat(),
                                  warmup=args.warmup,repeats=args.repeats,timeout=min(args.case_timeout,remaining),
                                  cleanup=10,cancel=lambda:interrupted[0] or time.monotonic()>=stop_at)
                    if library!='cann-opp': kwargs['adapter_library']=library
                    result=runner.collect(output,**kwargs)
                    attempted.add(key);summary['attempts']+=1
                    if result['valid']:
                        successes.add(key);summary['successful_attempts']+=1;consecutive_invalid=0
                    else:
                        failures[key]+=1;consecutive_invalid+=1
                        log=(output/'benchmark.log').read_text(errors='replace') if (output/'benchmark.log').exists() else ''
                        if 'ModuleNotFoundError' in log or 'ImportError:' in log:
                            disabled_libraries.add(library)
                            skip[library]='IMPORT_FAILED: see '+str(output/'benchmark.log')
                    summary['covered_unique_cases']=len(successes)
                    summary['attempted_unique_cases']=len(attempted)
                    summary['covered_by_library']=dict(Counter(k[0] for k in successes))
                    summary['last_output']=str(output)
                    summary['updated_at']=datetime.now(timezone.utc).isoformat()
                    json_write(output/'offline-case.json',row)
                    entry=dict(round=summary['round'],library=library,case_key=row['case_key'],operator=case['operator'],result=result)
                    with ledger.open('a') as handle:
                        handle.write(json.dumps(entry,ensure_ascii=False)+'\n');handle.flush();os.fsync(handle.fileno())
                    samples=defaultdict(list)
                    if result['valid']:
                        for obs in json.loads((output/'observations.json').read_text()):
                            samples[obs['metric']['name']].extend(float(x) for x in obs['raw_samples'])
                    span=sorted(samples['device_span_us']);host=samples['host_elapsed_us']
                    writer.writerow(dict(round=summary['round'],library=library,operator=case['operator'],model=case.get('extensions',{}).get('model_anchor','native-smoke'),
                        dtype=case['inputs'][0]['dtype'],input_shapes=json.dumps([x['shape'] for x in case['inputs']]),valid=result['valid'],device_release=result['device_release'],
                        device_span_median_us=statistics.median(span) if span else '',device_span_p95_us=span[max(0,math.ceil(.95*len(span))-1)] if span else '',
                        host_elapsed_median_us=statistics.median(host) if host else '',output=str(output),reason=result.get('reason','')))
                    csv_file.flush()
                    json_write(session/'summary.json',summary)
                    # Real idle evidence is required even after startup/profile failure.
                    released=runner.release_check(args.device,[],Collector(server_id))
                    json_write(output/'overnight-idle-check.json',released)
                    import re
                    if released['status']!='RELEASED' or not re.search(r'no process|no running process',released['evidence']['stdout'],re.I):
                        reason='DEVICE_RELEASE_UNCONFIRMED';break
                    if (output/'execution.json').exists():
                        execution=json.loads((output/'execution.json').read_text())
                        if execution['process_release']!='RELEASED': reason='PROCESS_RELEASE_UNCONFIRMED';break
                    # Avoid spending eight hours producing the same broken profiler output.
                    parsed_path=output/'parsed.json'
                    infrastructure_failure=False
                    if parsed_path.exists() and (output/'execution.json').exists():
                        parsed_result=json.loads(parsed_path.read_text())
                        executed=json.loads((output/'execution.json').read_text())
                        infrastructure_failure=executed['exit_code']==0 and not result['valid']
                    if consecutive_invalid>=12 and infrastructure_failure:
                        reason='REPEATED_PIPELINE_FAILURE';break
                else:
                    continue
                break
    except BaseException as exc:
        reason='RUNNER_ERROR'
        summary['error']=str(exc)
        raise
    finally:
        summary['state']='STOPPED'
        summary['stop_reason']=reason
        summary['finished_at']=datetime.now(timezone.utc).isoformat()
        summary['elapsed_seconds']=args.hours*3600-(deadline-time.monotonic())
        summary['skipped_libraries']=skip
        summary['uncovered_unique_cases']=len(matrix)-len(successes)
        json_write(session/'summary.json',summary)
        for sig,handler in previous.items(): signal.signal(sig,handler)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    return 0 if reason=='BUDGET_COMPLETE' and summary['successful_attempts'] else 1


if __name__=='__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError,RuntimeError) as error:
        print('ERROR: '+str(error),file=sys.stderr)
        sys.exit(1)
