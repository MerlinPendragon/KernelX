"""Frozen adapter catalog and exact-environment capability attestations.

Inventory is CPU-only. Importing an upstream package is reserved for a manually
budgeted benchmark child, never for discovery or support-matrix filtering.
"""
import copy
import hashlib
import json
import platform
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

from .agent.storage import atomic_json,database
from .cann_adapter import CannAddAdapter,PRESET
from .protocol import digest,validate,case_key

EXTENSIONS=('sgl-kernel-npu','tile-kernels','deepgemm-ascend','deepep-ascend')
LIBRARIES=('cann-opp','ops-nn','ops-transformer')+EXTENSIONS


def now():return datetime.now(timezone.utc).isoformat()
def fact(value):return value.get('value') if isinstance(value,dict) and value.get('status')=='KNOWN' else None


def environment_tuple(environment,device_uid,library,manifest_sha256):
    device=next(d for d in environment['devices'] if d['device_uid']==device_uid)
    row=next((r for r in environment['software']['operator_libraries'] if r['name']==library),{})
    return dict(library=library,revision=dict(version=fact(row.get('version')),commit=row.get('git_commit'),dirty_tree_sha256=row.get('dirty_tree_sha256'),artifact=row.get('artifact_sha256')),
        soc=fact(device['soc_family']),hardware_bin=fact(device['hardware_bin']),cpu_arch=fact(environment['host']['architecture']),
        cann=dict(toolkit=fact(environment['software']['toolkit']),fingerprints={k:v.get('sha256') for k,v in sorted(environment['extensions'].get('fingerprints',{}).items())}),
        python=fact(environment['software']['python']),packages={p['name']:fact(p['version']) for p in environment['software']['packages']},
        preset_sha256=digest(PRESET),manifest_sha256=manifest_sha256)


class SupportMatrix:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.db=database(self.root/'support.db')
        self.db.execute('CREATE TABLE IF NOT EXISTS case_attestations(identity TEXT,case_key TEXT,evidence TEXT,PRIMARY KEY(identity,case_key))')
        self.db.execute('CREATE TABLE IF NOT EXISTS support(identity TEXT PRIMARY KEY, library TEXT,status TEXT,reason TEXT,checked_at TEXT,environment_tuple TEXT,evidence TEXT)')
    def close(self):self.db.close()
    def lookup(self,binding):
        row=self.db.execute('SELECT * FROM support WHERE identity=?',(digest(binding),)).fetchone()
        return dict(row) if row else None
    def record(self,binding,status,reason,evidence):
        if status not in ('VERIFIED','UNSUPPORTED','UNVERIFIED'):raise ValueError('invalid support status')
        if not evidence:raise ValueError('auditable capability evidence required')
        if status=='VERIFIED' and not (evidence.get('valid') is True and evidence.get('provider_verified') is True and evidence.get('case_key') and evidence.get('bundle_sha256')):
            raise ValueError('measured complete profile and verified provider required')
        with self.db:self.db.execute('INSERT OR REPLACE INTO support VALUES(?,?,?,?,?,?,?)',(digest(binding),binding['library'],status,reason,now(),json.dumps(binding),json.dumps(evidence)))
    def attest_bundle(self,bundle,library,device_uid,scope='core'):
        from .agent.storage import verify_bundle
        root=Path(bundle);sealed=verify_bundle(root)
        environment=json.loads((root/'environment.json').read_text())
        manifest=json.loads((root/'manifest.json').read_text())
        registry=Registry();adapter=registry.get(library)
        expected=adapter.manifest if library=='cann-opp' else adapter.manifest(scope)
        keys={expected['case_key']} if library=='cann-opp' else {r['case_key'] for r in expected['cases']}
        if manifest['case_key'] not in keys:raise ValueError('measured case outside frozen manifest')
        session=json.loads((root/'session.json').read_text())
        profile=json.loads((root/'profile.json').read_text())
        if device_uid not in session['device_uids'] or profile['preset_sha256']!=digest(PRESET):raise ValueError('attestation device/preset mismatch')
        provider=environment['extensions'].get('runtime_host_providers',{})
        verified=provider.get('status')=='VERIFIED_HOST_PROVIDER' if library=='cann-opp' else provider.get('load_status')=='VERIFIED'
        if provider.get('certification_status')=='UNIMPLEMENTED':raise ValueError('runtime provider certification is UNIMPLEMENTED for this backend')
        if not verified:raise ValueError('DECLARED_ONLY cannot attest runtime support')
        binding=environment_tuple(environment,device_uid,library,digest(expected))
        if library!='cann-opp' and binding['revision']['commit']!=adapter.catalog['upstream']['commit']:raise ValueError('frozen revision not established')
        evidence=dict(valid=True,provider_verified=True,case_key=manifest['case_key'],bundle_sha256=digest(sealed),bundle_id=sealed['bundle_id'],evidence_uri=str(root.resolve()))
        identity=digest(binding)
        # Serialize aggregation across collector processes. Different scope hashes
        # or software/BIN tuples cannot fill each other's coverage gaps.
        from .device_lock import DeviceLock
        with DeviceLock('support-attestation',self.root/'.locks',timeout=15):
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO case_attestations VALUES(?,?,?)',(identity,manifest['case_key'],json.dumps(evidence)))
                rows=self.db.execute('SELECT case_key,evidence FROM case_attestations WHERE identity=?',(identity,)).fetchall()
                covered={r['case_key'] for r in rows}&keys
                case_evidence={r['case_key']:json.loads(r['evidence']) for r in rows if r['case_key'] in keys}
                aggregate=dict(evidence,case_evidence=case_evidence,required_case_keys=sorted(keys),missing_case_keys=sorted(keys-covered),scope=scope)
                complete=covered==keys
                self.record(binding,'VERIFIED' if complete else 'UNVERIFIED','all frozen cases have sealed provider/profile evidence' if complete else 'missing frozen case attestations: '+str(len(keys-covered)),aggregate)
        return self.lookup(binding)

    def rows(self):return [dict(r) for r in self.db.execute('SELECT * FROM support ORDER BY library,identity')]


class CatalogAdapter:
    def __init__(self,library):
        if library not in EXTENSIONS:raise ValueError('unknown catalog adapter')
        self.library=library
        self.catalog=json.loads((Path(__file__).parent/'catalogs'/(library+'.json')).read_text())
        for row in self.catalog['cases']:
            validate('case',row['case'])
            if case_key(row['case'])!=row['case_key']:raise ValueError('catalog case identity mismatch')
    def enumerate_cases(self,scope='core'):
        if scope not in ('core','full'):raise ValueError('core/full required')
        return copy.deepcopy([r for r in self.catalog['cases'] if scope=='full' or r['core']])
    def manifest(self,scope='core'):
        return dict(schema_version=1,library=self.library,catalog_version=self.catalog['catalog_version'],scope=scope,scope_definition=self.catalog['scope_definition'],upstream=self.catalog['upstream'],requirements=self.catalog['requirements'],entry=self.catalog['entry'],cases=self.enumerate_cases(scope))
    def capabilities(self,environment,device_uid,scope='core',matrix=None):
        manifest=self.manifest(scope);binding=environment_tuple(environment,device_uid,self.library,digest(manifest));require=self.catalog['requirements'];reasons=[]
        soc=(binding['soc'] or '')+' '+(binding['hardware_bin'] or '')
        if not binding['soc'] or not binding['hardware_bin']:reasons.append('UNKNOWN_HARDWARE')
        elif not any(prefix.lower() in soc.lower() for prefix in require['soc_prefixes']):reasons.append('UPSTREAM_SOC_NOT_SUPPORTED')
        python=binding['python']
        if not python:reasons.append('UNKNOWN_PYTHON')
        elif tuple(int(x) for x in python.split('.')[:2])<tuple(require['python_min']):reasons.append('PYTHON_TOO_OLD')
        packages=binding['packages']
        # DeepJIT may be embedded as a pinned submodule; an absent standalone
        # distribution never proves that an installed kernel lacks its JIT.
        missing=[name for name in require['dependencies'] if name!='deep-jit' and not packages.get(name)]
        if missing:reasons.append('DEPENDENCIES_NOT_INSTALLED:'+','.join(missing))
        import re
        def version_numbers(value):
            match=re.search(r'(\d+)\.(\d+)(?:\.(\d+))?',value or '')
            return tuple(int(x or 0) for x in match.groups()) if match else None
        for dependency,minimum in require.get('versions_min',{}).items():
            actual=version_numbers(binding['cann']['toolkit'] if dependency=='cann' else packages.get(dependency))
            if actual is None:reasons.append('VERSION_UNATTESTED:'+dependency)
            elif actual<tuple(minimum):reasons.append('VERSION_TOO_OLD:'+dependency)
        library=next((r for r in environment['software']['operator_libraries'] if r['name']==self.library),{})
        if not library.get('resolved_path'):reasons.append('LIBRARY_NOT_INSTALLED')
        elif library.get('git_commit') and library['git_commit']!=self.catalog['upstream']['commit']:reasons.append('FROZEN_REVISION_MISMATCH')
        cached=matrix.lookup(binding) if matrix else None
        if reasons:status='UNSUPPORTED';reason='; '.join(reasons)
        elif cached and cached['status']=='VERIFIED':status='VERIFIED';reason=cached['reason']
        else:status='UNVERIFIED';reason='installed candidate requires measured provider/attribution and frozen-revision attestation for this exact tuple'
        evidence=dict(environment_id=environment['environment_id'],source=self.catalog['upstream']['requirements_source'],expected_upstream_commit=self.catalog['upstream']['commit'],actual_provenance=environment['extensions'].get('library_provenance',{}).get(self.library),load_status='DECLARED_ONLY')
        if matrix and status!='VERIFIED':matrix.record(binding,status,reason,evidence)
        return dict(library=self.library,status=status,reason=reason,checked_at=cached['checked_at'] if status=='VERIFIED' else now(),environment_tuple=binding,tuple_sha256=digest(binding),manifest=manifest,manifest_sha256=digest(manifest),provider_certification=dict(status='UNIMPLEMENTED',reason='no validated runtime extension/JIT source association; diagnostic collection cannot certify this backend'),resources=self.enumerate_cases(scope)[0]['resources'],evidence=json.loads(cached['evidence']) if status=='VERIFIED' else evidence)
    def cache_key(self,environment,device_uid,compiler_fingerprints,scope='core'):
        if not compiler_fingerprints or any(not v for v in compiler_fingerprints.values()):raise ValueError('known compiler fingerprints required')
        return digest(dict(environment=environment_tuple(environment,device_uid,self.library,digest(self.manifest(scope))),compiler=compiler_fingerprints,backend='ascend',upstream=self.catalog['upstream'],entry=self.catalog['entry']))
    def build(self,output):
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        began=time.monotonic()
        artifact=dict(library=self.library,upstream=self.catalog['upstream'],entry=self.catalog['entry'],harness_sha256=hashlib.sha256((Path(__file__).parent/'library_benchmark.py').read_bytes()).hexdigest(),online_install=False)
        atomic_json(output/'adapter.json',artifact)
        atomic_json(output/'build.json',dict(source_sha256=artifact['harness_sha256'],binary_sha256=hashlib.sha256((output/'adapter.json').read_bytes()).hexdigest(),cache_state='UPSTREAM_JIT',cache_key=None,elapsed_seconds=time.monotonic()-began,compile_elapsed_seconds=None))
        return output/'adapter.json'
    def resolve_versions(self,providers):
        # Discovery metadata alone cannot establish the implementation actually
        # loaded in the child. Preserve source/JIT/communication dependencies.
        data=json.loads(Path(providers).read_text())
        files=[]
        for path in sorted(set(data.get('loaded_files',[]))):
            p=Path(path).resolve();files.append(dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None))
        return dict(certification_status='UNIMPLEMENTED',certification_reason='backend-specific loaded extension/JIT-to-frozen-source association has not been implemented',library=self.library,version=data.get('version'),git_commit=data.get('git_commit'),repository=self.catalog['upstream']['repository'],loaded_files=files,jit_artifacts=data.get('jit_artifacts',[]),framework=data.get('framework'),communication=data.get('communication'),load_status='DECLARED_ONLY')
    def prepare(self,device,warmup,repeats,raw,sidecar,case_index=0,rank=0):
        if type(warmup) is not int or type(repeats) is not int or not 1<=warmup<=1000 or not 1<=repeats<=1000:raise ValueError('bounded repeat policy required')
        case=self.enumerate_cases('full')[case_index]
        return dict(library=self.library,case=case,device=device,warmup=warmup,repeats=repeats,raw=str(raw),sidecar=str(sidecar),rank=rank,preset=PRESET,upstream=self.catalog['upstream'])
    def benchmark_command(self,binary,device,warmup,repeats,raw,sidecar,case_index=0,rank=0):
        spec=self.prepare(device,warmup,repeats,raw,sidecar,case_index,rank)
        path=Path(binary).parent/'benchmark-spec.json';atomic_json(path,spec)
        return [sys.executable,'-B','-m','kernelx.library_benchmark','--spec',str(path)]
    def attribution_spec(self):return dict(version='aclprof-ranges-v1',boundary='one synchronized logical invocation per kernelx:measure:<iteration> range',tasks='all trace-correlated device tasks; multiple tasks are not averaged as a single task',provider='loaded module/extension plus JIT artifact fingerprints; otherwise DECLARED_ONLY')
    def metric_spec(self):return dict(device_span_us='last correlated device task end minus first start within invocation',host_elapsed_us='monotonic rangeStart to rangeStop after stream synchronize',task_duration_us='raw duration for each correlated task',group_latency_us='maximum synchronized rank-local invocation duration; never subtract cross-host clocks')


class Registry:
    def __init__(self):self.adapters={name:CatalogAdapter(name) for name in EXTENSIONS}
    def get(self,library):
        if library=='cann-opp':return CannAddAdapter()
        if library in self.adapters:return self.adapters[library]
        raise ValueError('no performance entry for '+library)
    def inventory(self,environment,device_uid,scope='core',matrix=None):
        result=[]
        for name in LIBRARIES:
            if name in self.adapters:result.append(self.adapters[name].capabilities(environment,device_uid,scope,matrix));continue
            if name=='cann-opp':
                adapter=CannAddAdapter();manifest=adapter.manifest
                binding=environment_tuple(environment,device_uid,name,digest(manifest));cached=matrix.lookup(binding) if matrix else None
                status=cached['status'] if cached else 'UNVERIFIED'
                result.append(dict(library=name,status=status,reason=cached['reason'] if cached else 'frozen Add seed; requires actual runtime/profile validation',manifest=manifest,manifest_sha256=digest(manifest),environment_tuple=binding,tuple_sha256=digest(binding),evidence=json.loads(cached['evidence']) if cached else {},checked_at=cached['checked_at'] if cached else now(),resources=dict(mode='independent',min_ranks=1),scope_definition='Add seed only; no exhaustive CANN/ops catalog claim'))
            else:result.append(dict(library=name,status='ADAPTER_UNCONFIGURED',reason='independent component has no executable frozen performance entry; not replaced by aggregate OPP version',resources=None,manifest=None,manifest_sha256=None))
        return result


class FrozenPerformanceAdapter:
    """Bind one catalog case to the existing supervised/sealed Runner contract."""
    def __init__(self,library,index=0,rank=0):
        self.adapter=CatalogAdapter(library);self.index=index;self.rank=rank
        cases=self.adapter.enumerate_cases('full')
        if type(index) is not int or not 0<=index<len(cases) or type(rank) is not int or rank<0:raise ValueError('invalid frozen case/rank index')
        selected=cases[index]
        self.manifest=dict(manifest_version=1,library_id=library,implementation=self.adapter.catalog['entry'],case_key=selected['case_key'],case=selected['case'])
        native=CannAddAdapter();self.msprof=native.msprof;self.home=native.home
    def capabilities(self):return dict(status='UNVERIFIED',reason='candidate needs exact tuple/profile/provider measurement')
    def build(self,output,environment=None):return self.adapter.build(output)
    def prepare(self,device,warmup,repeats,raw,sidecar):return self.adapter.prepare(device,warmup,repeats,raw,sidecar,self.index,self.rank)
    def benchmark_command(self,binary,device,warmup,repeats,raw,sidecar):return self.adapter.benchmark_command(binary,device,warmup,repeats,raw,sidecar,self.index,self.rank)
    def resolve_versions(self,path):
        value=self.adapter.resolve_versions(path)
        return dict(value,status='DECLARED_ONLY',api_path=None,api_sha256=None,loaded_libraries_sha256=digest(value['loaded_files']))
