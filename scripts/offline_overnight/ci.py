"""Version-bound offline upstream CI inventory and supervised diagnostic traces."""
import ast
from collections import defaultdict, deque
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

DISTRIBUTIONS = {
    'sgl-kernel-npu': ('sgl-kernel-npu','sgl_kernel_npu'),
    'tile-kernels': ('tile-kernels','tile_kernels'),
    'deepgemm-ascend': ('deepgemm-ascend','deep-gemm','deep_gemm'),
    'deepep-ascend': ('deepep-ascend','deep-ep','deep_ep'),
    'ops-nn': ('ops-nn','ops_nn'),
    'ops-transformer': ('ops-transformer','ops_transformer'),
}
MODULES = {'sgl-kernel-npu':'sgl_kernel_npu','tile-kernels':'tile_kernels',
           'deepgemm-ascend':'deep_gemm','deepep-ascend':'deep_ep'}

def git(root, *args):
    try:
        return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL,timeout=5).strip()
    except (OSError,subprocess.SubprocessError): return None


def installed(library):
    result = dict(library=library,version=None,commit=None,source_root=None)
    for name in DISTRIBUTIONS.get(library,()):
        try: dist=importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError: continue
        result['version']=dist.version
        result['distribution']=dist.metadata['Name']
        result['package_root']=str(dist.locate_file(''))
        raw=dist.read_text('direct_url.json')
        if raw:
            direct=json.loads(raw)
            result['commit']=direct.get('vcs_info',{}).get('commit_id')
            if direct.get('url','').startswith('file:'):
                result['source_root']=unquote(urlparse(direct['url']).path)
        # Build suffix commits are recorded by DeepGEMM/DeepEP packaging.
        suffix=dist.version.split('+',1)[-1] if '+' in dist.version else ''
        if not result['commit'] and re.fullmatch('[0-9a-f]{7,40}',suffix): result['commit']=suffix
        if not result['commit'] and re.fullmatch('g[0-9a-f]{7,40}',suffix): result['commit']=suffix[1:]
        break
    module=MODULES.get(library)
    if module and not result['source_root']:
        try: spec=importlib.util.find_spec(module)
        except (ImportError,ValueError): spec=None
        if spec and spec.origin:
            for root in list(Path(spec.origin).resolve().parents)[:6]:
                if (root/'.git').exists() and (root/'tests').is_dir():
                    result['source_root']=str(root)
                    result['commit']=git(root,'rev-parse','HEAD') or result['commit']
                    break
    return result


def source_inventory(root,library):
    """Preserve generators/expressions; never pretend unresolved products are concrete shapes."""
    entries=[]
    paths=set()
    for name in ('tests','test','testing','.github/workflows'):
        folder=root/name
        if folder.is_dir(): paths.update(p for p in folder.rglob('*') if p.is_file() and p.suffix in ('.py','.cpp','.h','.hpp','.json','.csv','.yaml','.yml','.sh'))
    for path in sorted(paths):
        text=path.read_text(errors='replace')
        record=dict(library=library,path=str(path.relative_to(root)),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        if path.suffix=='.py':
            parameters=[];expressions=[];tests=[]
            try: tree=ast.parse(text)
            except SyntaxError: tree=None
            if tree:
                for node in ast.walk(tree):
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'): tests.append(node.name)
                    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='parametrize':
                        parameters.append(dict(line=node.lineno,expression=ast.get_source_segment(text,node)))
                    if isinstance(node,(ast.Assign,ast.For)):
                        fragment=ast.get_source_segment(text,node) or ''
                        first=fragment.splitlines()[0]
                        if re.search(r'(?i)shape|hidden|tokens|batch|head|seq|_nk|_mnk|_list|config|cases|params',first):
                            # Keep the original symbolic RHS/iterator rather than evaluating upstream code.
                            expression=node.value if isinstance(node,ast.Assign) else node.iter
                            expressions.append(dict(line=node.lineno,expression=ast.get_source_segment(text,expression)))
                record.update(tests=tests,parameter_generators=parameters,shape_expressions=expressions)
        else:
            record['shape_lines']=[dict(line=i,text=line[:1200]) for i,line in enumerate(text.splitlines(),1)
                                   if re.search(r'(?i)shape|parametrize|TEST_[FP]|INSTANTIATE_TEST|num_tokens|hidden_size',line)]
        entries.append(record)
    return entries


def discover(runtime,requested,explicit_roots=None):
    frozen=Path(runtime)/'kernelx/offline_ci_sources'
    sources=json.loads((frozen/'sources.json').read_text()) if (frozen/'sources.json').exists() else []
    commits={row['lib']:row['ref'] for row in sources}
    roots=json.loads(os.environ.get('KERNELX_LIBRARY_ROOTS','{}'))
    roots.update(explicit_roots or {})
    result=[]
    for library in sorted(requested & set(DISTRIBUTIONS)):
        binding=installed(library)
        local=roots.get(library) or binding.get('source_root')
        root=Path(local).resolve() if local else None
        installed_commit=binding.get('commit')
        if root and root.is_dir():
            source_commit=git(root,'rev-parse','HEAD')
            dirty=git(root,'status','--porcelain')
            if installed_commit and source_commit and not source_commit.startswith(installed_commit):
                binding.update(status='VERSION_UNMATCHED',reason='installed and source commits differ',source_commit=source_commit)
                root=None
            elif source_commit and not dirty:
                # Explicit local roots are deployment provenance declarations, not JIT certification.
                binding.update(status='LOCAL_SOURCE_MATCHED' if installed_commit else 'LOCAL_SOURCE_DECLARED',source_commit=source_commit,
                               reason='existing local checkout; loaded extension/JIT provenance remains DECLARED_ONLY')
            else:
                binding.update(status='SOURCE_UNVERIFIED',reason='local test tree lacks clean Git provenance')
                root=None
        elif library in commits and installed_commit and commits[library].startswith(installed_commit):
            root=frozen/library
            binding.update(status='FROZEN_COMMIT_MATCHED',source_commit=commits[library],reason='embedded CI snapshot matches installed Git/build commit')
        else:
            binding.update(status='VERSION_UNMATCHED',reason='no matching local test source or embedded commit; do not substitute current upstream CI')
            root=None
        binding['ci_root']=str(root) if root else None
        binding['embedded_commit']=commits.get(library)
        # Record all available frozen expressions, even when ineligible for execution.
        inventory_root=root or (frozen/library if (frozen/library).is_dir() else None)
        binding['inventory']=source_inventory(inventory_root,library) if inventory_root else []
        binding['inventory_source']='matched-local' if root and root!=frozen/library else 'embedded-frozen'
        result.append(binding)
    return result


def tasks(bindings):
    queues=defaultdict(deque);skips=[]
    for binding in bindings:
        root=binding.get('ci_root')
        if not root: continue
        for entry in binding['inventory']:
            path=Path(root)/entry['path']
            if not path.name.startswith('test_') or path.suffix!='.py': continue
            text=path.read_text(errors='replace')
            if binding['library']=='deepep-ascend' or re.search(r'init_dist\(|init_process_group\(|multiprocessing|mp\.spawn\(',text):
                skips.append(dict(library=binding['library'],file=entry['path'],reason='MULTI_RANK_REQUIRES_SEPARATE_COORDINATED_RUNNER'));continue
            if not entry.get('tests'): continue
            mode='script' if binding['library']=='deepgemm-ascend' and "__main__" in text and '--skip-prof' in text else 'pytest'
            # Native CI is inventoried but requires its built test binary.
            queues[binding['library']].append(dict(library=binding['library'],root=root,path=str(path),relative_path=entry['path'],mode=mode,
                                                  source_commit=binding.get('source_commit'),installed_version=binding.get('version')))
    jobs=[]
    while any(queues.values()):
        for library in sorted(queues):
            if queues[library]: jobs.append(queues[library].popleft())
    return jobs,skips


def run_suites(args,bindings,session,server_id,device_uid,deadline,interrupted,bounded_run):
    from kernelx.device_lock import DeviceLock
    from kernelx.probe import Collector
    from kernelx.runner import release_check
    jobs,skips=tasks(bindings)
    report=dict(planned_files=len(jobs),attempted_files=0,completed_files=0,collected_tests=0,passed_tests=0,failed_tests=0,skipped_tests=0,files=[],skipped_files=skips,
                metric_scope='CI_TEST_TRACE includes upstream correctness/reference/setup work; not latency-v1 observations')
    report_path=session/'ci-summary.json'
    def save(): report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    if not jobs or args.ci_hours<=0: return report
    if args.device!=0:
        report['stop_reason']='CI_DEVICE_MAPPING_UNSUPPORTED: upstream tests may hard-code npu:0; choose device 0 or only model suite'
        save();return report
    if importlib.util.find_spec('pytest') is None:
        report['stop_reason']='PYTEST_NOT_INSTALLED';save();return report
    ci_deadline=min(deadline-120,time.monotonic()+args.ci_hours*3600)
    for index,job in enumerate(jobs):
        if interrupted[0] or ci_deadline-time.monotonic()<30: break
        if __import__('shutil').disk_usage(session).free<args.reserve_gib*2**30:
            report['stop_reason']='DISK_RESERVE_REACHED';break
        output=session/('ci-%04d'%(index+1));output.mkdir()
        spec=dict(job,output=str(output),device=args.device,deadline_utc=time.time()+max(0,ci_deadline-time.monotonic()),test_level=args.ci_level)
        (output/'spec.json').write_text(json.dumps(spec,indent=2))
        print('CI: '+job['library']+' '+job['relative_path'],flush=True)
        with DeviceLock(device_uid) as lock:
            before=release_check(args.device,[],Collector(server_id))
            if before['status']!='RELEASED' or not re.search('no process|no running process',before['evidence']['stdout'],re.I):
                report['stop_reason']='DEVICE_RELEASE_UNCONFIRMED';save();return report
            execution=bounded_run([sys.executable,'-B','-m','kernelx.offline_ci_worker','--spec',str(output/'spec.json')],output/'ci.log',
                                  min(args.ci_file_timeout,ci_deadline-time.monotonic()),pass_fds=(lock.fd,),ownership_path=output/'ownership.json')
            (output/'execution.json').write_text(json.dumps(execution,indent=2))
            release=release_check(args.device,execution['owned_pids'],Collector(server_id))
            (output/'device-release.json').write_text(json.dumps(release,indent=2))
        record=dict(job,output=str(output),exit_code=execution['exit_code'],reason=execution['reason'],device_release=release['status'])
        results=output/'tests.jsonl'
        if results.exists():
            for line in results.read_text().splitlines():
                item=json.loads(line)
                if item.get('phase')=='collected': report['collected_tests']+=1
                if item.get('phase')=='call' or (item.get('phase')=='setup' and item.get('outcome') in ('failed','skipped')):
                    report[item['outcome']+'_tests']+=1
        raw=list((output/'raw').glob('PROF_*'))
        if raw and release['status']=='RELEASED' and ci_deadline-time.monotonic()>10:
            from kernelx.cann_adapter import CannAddAdapter
            exported=bounded_run([CannAddAdapter().msprof,'--export=on','--output='+str(raw[0])],output/'export.log',min(120,ci_deadline-time.monotonic()))
            record['export_exit_code']=exported['exit_code']
        report['files'].append(record);report['attempted_files']+=1
        if execution['exit_code']==0 and not execution['reason']: report['completed_files']+=1
        save()
        if release['status']!='RELEASED' or execution['process_release']!='RELEASED' or not re.search('no process|no running process',release['evidence']['stdout'],re.I):
            report['stop_reason']='DEVICE_RELEASE_UNCONFIRMED';save();return report
    report.setdefault('stop_reason','CI_BUDGET_COMPLETE' if report['attempted_files']<len(jobs) else ('CI_QUEUE_COMPLETE' if report['completed_files']==len(jobs) else 'CI_QUEUE_WITH_FAILURES'))
    save();return report


def shape_cases(bindings,make_case):
    """Exact positive shape seeds for supported entries, plus upstream CI execution.

A shape-only GEMM seed is not claimed to cover its original transpose/accumulate
variant; the upstream script retains those variants in the CI trace suite.
"""
    from kernelx.protocol import case_key,validate
    rows=[]
    def parsed(path):
        return ast.parse(path.read_text()) if path.is_file() else None
    def assignment(tree,function,variable):
        if tree is None:return []
        for node in ast.walk(tree):
            if isinstance(node,ast.FunctionDef) and node.name==function:
                for item in ast.walk(node):
                    if isinstance(item,ast.Assign):
                        if any(isinstance(t,ast.Name) and t.id==variable for t in item.targets):
                            try:return ast.literal_eval(item.value)
                            except (ValueError,TypeError):pass
                        for target in item.targets:
                            if isinstance(target,(ast.Tuple,ast.List)):
                                names=[getattr(t,'id',None) for t in target.elts]
                                if variable in names:
                                    try:return ast.literal_eval(item.value)[names.index(variable)]
                                    except (ValueError,TypeError,IndexError):pass
        return []
    def append(binding,op,inputs,outputs,attrs,dtype,source,output_dtypes=None,strides=None):
        if any(any(d==0 for d in shape) for shape in inputs):return # empty CI correctness stays in upstream tests
        row=make_case(binding['library'],op,inputs,outputs,attrs,'CI',dtype)
        row['case']['extensions'].update(ci_source=source,ci_commit=binding['source_commit'],ci_installed_version=binding['version'],
                                        ci_case_scope='shape/layout seed; full upstream parameter variants are in CI trace suite')
        if output_dtypes:
            for tensor,dtype_out in zip(row['case']['outputs'],output_dtypes):tensor['dtype']=dtype_out
        if strides:
            for index,stride in strides.items():row['case']['inputs'][index]['stride']=stride
        row['case_key']=case_key(row['case']);validate('case',row['case']);rows.append(row)
    for binding in bindings:
        if not binding.get('ci_root'):continue
        root=Path(binding['ci_root']);library=binding['library']
        if library=='sgl-kernel-npu':
            source='tests/python/sgl_kernel_npu/test_fused_sigmoid_mul.py'
            tree=parsed(root/source)
            if tree is None:continue
            for function in tree.body:
                if not isinstance(function,ast.FunctionDef):continue
                for loop in ast.walk(function):
                    if not isinstance(loop,ast.For):continue
                    try:values=ast.literal_eval(loop.iter)
                    except (ValueError,TypeError):continue
                    if not isinstance(values,(list,tuple)) or not values:continue
                    if isinstance(loop.target,ast.Name) and loop.target.id=='shape':
                        for shape in values:
                            if not isinstance(shape,(list,tuple)):continue
                            for dtype in ('bfloat16','float16','float32'):
                                append(binding,'fused-sigmoid-mul',[shape,shape],[shape],{},dtype,source)
                    elif isinstance(loop.target,ast.Tuple) and [getattr(t,'id',None) for t in loop.target.elts]==['n','d']:
                        for n,d in values:
                            for dtype in ('bfloat16','float16','float32'):
                                for gate_shape in ((n,),(n,1)):
                                    append(binding,'fused-sigmoid-mul-broadcast',[(n,d),gate_shape],[(n,d)],{},dtype,source)
            names={f.name for f in tree.body if isinstance(f,ast.FunctionDef)}
            source_text=(root/source).read_text()
            # Preserve layouts as well as dimensions from the frozen positive tests.
            if 'test_fused_sigmoid_mul_non_contiguous' in names and 'torch.randn(32, 8192' in source_text:
                append(binding,'fused-sigmoid-mul',[(32,4096)]*2,[(32,4096)],dict(input_views={'0':'stride2','1':'stride2'}),'bfloat16',source,strides={0:[8192,2],1:[8192,2]})
            if 'test_fused_sigmoid_mul_broadcast_transposed' in names and 'torch.randn(4096, 32' in source_text:
                for dtype in ('bfloat16','float16','float32'):
                    append(binding,'fused-sigmoid-mul-broadcast',[(32,4096),(32,1)],[(32,4096)],dict(input_views={'0':'transpose'}),dtype,source,strides={0:[1,32]})
        elif library=='tile-kernels':
            source='tile_kernels/testing/generator.py';tree=parsed(root/source)
            widths=assignment(tree,'generate_hidden_sizes','base_list')
            # Both default CI and full boundary-token generator values are preserved.
            tokens=set(assignment(tree,'generate_num_tokens','tokens'))
            if tree:
                for node in ast.walk(tree):
                    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='tokens' for t in node.targets):
                        try:tokens.update(ast.literal_eval(node.value))
                        except (ValueError,TypeError):
                            if isinstance(node.value,ast.BinOp) and isinstance(node.value.op,ast.Add):
                                try:tokens.update(ast.literal_eval(node.value.left))
                                except (ValueError,TypeError):pass
            # Benchmark/core test adds its own hidden dimensions.
            test_source='tests/quant/test_per_token_cast.py'
            test=parsed(root/test_source)
            if test:
                for node in ast.walk(test):
                    if isinstance(node,ast.IfExp):
                        try:extra=ast.literal_eval(node.body)
                        except (ValueError,TypeError):continue
                        if isinstance(extra,list) and extra and all(isinstance(x,int) and x>=128 for x in extra):widths+=extra
            for m in sorted(tokens):
                for h in sorted(set(widths)):
                    if not isinstance(h,int) or h%128:continue
                    for dtype in ('bfloat16','float32'):
                        append(binding,'per-token-cast',[(m,h)],[(m,h),(m,h//32)],dict(fmt='e4m3',num_per_channels=32,use_packed_ue8m0=False,round_sf=False),dtype,
                               source+'; '+test_source,output_dtypes=['float8_e4m3fn','float32'])
        elif library=='deepgemm-ascend':
            source='tests/generators.py';tree=parsed(root/source)
            shapes=set()
            if tree:
                for function in tree.body:
                    if isinstance(function,ast.FunctionDef) and function.name.startswith('_enumerate_normal'):
                        for shape in assignment(tree,function.name,'shapes'):
                            if isinstance(shape,tuple) and len(shape)==3:shapes.add(shape)
            nk=assignment(tree,'_enumerate_normal','bf16_output_nk')+assignment(tree,'_enumerate_normal','fp32_output_nk')
            forwards=assignment(tree,'_enumerate_normal','m_fwd_list')
            backwards=assignment(tree,'_enumerate_normal','m_bwd_list')
            for m in forwards:
                for n,k in nk:shapes.add((m,n,k))
            for m in backwards:
                for n,k in nk:
                    shapes.add((m,k,n));shapes.add((n,m,k))
            for m,n,k in sorted(shapes):
                append(binding,'bf16-gemm-nt',[(m,k),(n,k)],[(m,n)],dict(ci_variant='shape-only-NT-no-accumulate'),'bfloat16',source)
    return rows
