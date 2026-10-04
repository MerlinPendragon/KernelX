"""Private immutable native artifact cache, checked on every hit."""
import hashlib,json,os,platform,shutil,time
from pathlib import Path
from .agent.storage import atomic_json,fsync_dir
from .device_lock import DeviceLock
from .protocol import digest


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_key(adapter,environment):
    compiler=Path(shutil.which('c++') or '/NONEXISTENT').resolve()
    headers={str(path.relative_to(adapter.home)):sha(path) for path in sorted((adapter.home/'include').rglob('*.h')) if path.is_file()}
    devices=sorted({(d['soc_family'].get('value'),d['hardware_bin'].get('value')) for d in environment['devices']},key=str)
    binding=dict(source_sha256=sha(adapter.base/'native/cann_add.cpp'),compiler_path=str(compiler),compiler_sha256=sha(compiler),headers=headers,target=dict(cpu=platform.machine(),devices=[list(d) for d in devices]),cann_fingerprints={k:v['sha256'] for k,v in environment['extensions']['fingerprints'].items()},flags=['-std=c++17','-O2','-Wl,-rpath,'+str(adapter.home/'lib64')],manifest_sha256=digest(adapter.manifest))
    return digest(binding),binding


def cached_build(adapter,output,environment,root,compile_binary):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.stat().st_uid!=os.geteuid() or root.stat().st_mode&0o022:raise ValueError('private owner-only writable cache required')
    key,binding=native_key(adapter,environment);entry=root/key
    with DeviceLock(key,root/'.locks'):
        if entry.exists():
            metadata=json.loads((entry/'cache.json').read_text())
            if metadata['binding']!=binding or metadata['binary_sha256']!=sha(entry/'cann_add') or (entry/'cann_add').is_symlink():raise ValueError('native cache drift')
            started=time.monotonic();shutil.copyfile(entry/'cann_add',output/'cann_add');(output/'cann_add').chmod(0o500)
            result=dict(metadata['build'],cache_state='HIT',cache_key=key,elapsed_seconds=time.monotonic()-started,compile_elapsed_seconds=0)
            atomic_json(output/'build.json',result);(output/'build.log').write_text('verified immutable native cache hit\n')
            return output/'cann_add'
        binary=compile_binary(output);result=json.loads((output/'build.json').read_text())
        staging=root/(key+'.staging-'+str(os.getpid()));staging.mkdir(mode=0o700)
        shutil.copyfile(binary,staging/'cann_add');(staging/'cann_add').chmod(0o500)
        atomic_json(staging/'cache.json',dict(binding=binding,binary_sha256=sha(binary),build=result))
        with (staging/'cann_add').open('rb') as stream:os.fsync(stream.fileno())
        fsync_dir(staging);os.replace(staging,entry);fsync_dir(root)
        result.update(cache_state='MISS',cache_key=key,compile_elapsed_seconds=result['elapsed_seconds']);atomic_json(output/'build.json',result)
        return binary
