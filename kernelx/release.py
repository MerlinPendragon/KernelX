"""Offline, content-addressed releases signed with RSA-PSS/SHA256.

Python >=3.9 standard library plus the host OpenSSL CLI; no online installer.
The detached signature covers the exact manifest bytes, including payload hashes.
"""
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

from .agent.storage import atomic_json, fsync_dir, safe_path
from .protocol import digest, validate, case_key

FINGERPRINTS=('msprof','cann-opp-version','cann-opapi','cann-nnopbase','cann-ascendcl','cann-msprofiler')


def sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): value.update(chunk)
    return value.hexdigest()


def openssl(arguments):
    result=subprocess.run(['openssl']+arguments,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
    if result.returncode: raise ValueError('OpenSSL signature operation failed: '+result.stderr.decode(errors='replace')[:500])
    return result.stdout


def compatibility(environment,device_uid):
    validate('environment',environment)
    device=next(d for d in environment['devices'] if d['device_uid']==device_uid)
    result=dict(cpu_arch=environment['host']['architecture']['value'],soc=device['soc_family']['value'],
                hardware_bin=device['hardware_bin']['value'],python_min=[3,9],
                fingerprints={name:environment['extensions'].get('fingerprints',{}).get(name,{}).get('sha256') for name in FINGERPRINTS})
    check_group(result)
    return result


def check_group(value):
    if set(value)!={'cpu_arch','soc','hardware_bin','python_min','fingerprints'} or not all(isinstance(value[k],str) and value[k] for k in ('cpu_arch','soc','hardware_bin')):
        raise ValueError('known CPU/SoC/BIN required')
    if value['python_min']!=[3,9] or set(value['fingerprints'])!=set(FINGERPRINTS) or any(not re.fullmatch('[a-f0-9]{64}',str(v)) for v in value['fingerprints'].values()):
        raise ValueError('exact profiler/OPP fingerprints and Python >=3.9 required')


def build_release(source,repository,commit,group,key,sequence=1):
    source=Path(source); repository=Path(repository); repository.mkdir(parents=True,exist_ok=True)
    check_group(group)
    if type(sequence) is not int or sequence<1: raise ValueError('positive channel sequence required')
    if not re.fullmatch('[a-f0-9]{40}',commit): raise ValueError('full git commit required')
    files=[]
    for path in sorted((source/'kernelx').rglob('*')):
        if path.is_symlink(): raise ValueError('source symlink unsupported')
        if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc': continue
        relative=path.relative_to(source).as_posix(); safe_path(relative)
        files.append(dict(path=relative,bytes=path.stat().st_size,sha256=sha(path)))
    if not files: raise ValueError('empty release')
    staging=repository/('.build-'+str(os.getpid())); staging.mkdir(exist_ok=False)
    try:
        artifact=staging/'payload.tar.gz'
        with artifact.open('wb') as output, gzip.GzipFile(fileobj=output,mode='wb',filename='',mtime=0) as zipped, tarfile.open(fileobj=zipped,mode='w|') as tar:
            for row in files:
                info=tarfile.TarInfo(row['path']); info.size=row['bytes']; info.mode=0o444
                with (source/row['path']).open('rb') as stream: tar.addfile(info,stream)
        manifest=dict(schema_version=1,sequence=sequence,git_commit=commit,protocol_version='latency-v1',compatibility=group,
            artifact=dict(name='payload.tar.gz',bytes=artifact.stat().st_size,sha256=sha(artifact),unpacked_bytes=sum(f['bytes'] for f in files)),
            files=files,schemas={f['path']:f['sha256'] for f in files if '/schemas/' in f['path']},
            case_manifests={f['path']:f['sha256'] for f in files if '/manifests/' in f['path']},
            dependencies=dict(python_min=[3,9],python_packages=[],signature='RSA-PSS-SHA256',openssl_min='1.1.1',native_dependencies='host CANN; fingerprinted separately; never installed by bootstrap'))
        manifest['release_id']=digest(manifest); atomic_json(staging/'manifest.json',manifest)
        openssl(['dgst','-sha256','-sign',str(key),'-sigopt','rsa_padding_mode:pss','-sigopt','rsa_pss_saltlen:-1','-out',str(staging/'manifest.sig'),str(staging/'manifest.json')])
        for path in staging.iterdir():
            with path.open('rb') as stream: os.fsync(stream.fileno())
        fsync_dir(staging)
        destination=repository/manifest['release_id']
        if destination.exists():
            if json.loads((destination/'manifest.json').read_text())!=manifest: raise ValueError('immutable release conflict')
        else: os.replace(staging,destination); fsync_dir(repository)
        return destination
    finally:
        if staging.exists(): shutil.rmtree(staging)


def verify_manifest(directory,key):
    directory=Path(directory)
    if (directory/'manifest.json').stat().st_size>1024*1024 or (directory/'manifest.sig').stat().st_size>65536: raise ValueError('oversized manifest/signature')
    openssl(['dgst','-sha256','-verify',str(key),'-sigopt','rsa_padding_mode:pss','-sigopt','rsa_pss_saltlen:-1','-signature',str(directory/'manifest.sig'),str(directory/'manifest.json')])
    manifest=json.loads((directory/'manifest.json').read_text()); body=dict(manifest); identity=body.pop('release_id')
    if digest(body)!=identity or not re.fullmatch('[a-f0-9]{64}',identity): raise ValueError('release ID mismatch')
    required={'schema_version','sequence','git_commit','protocol_version','compatibility','artifact','files','schemas','case_manifests','dependencies','release_id'}
    if set(manifest)!=required or manifest['schema_version']!=1 or manifest['protocol_version']!='latency-v1' or not re.fullmatch('[a-f0-9]{40}',manifest['git_commit']): raise ValueError('unsupported release protocol/schema')
    if type(manifest['sequence']) is not int or manifest['sequence']<1: raise ValueError('invalid signed sequence')
    check_group(manifest['compatibility'])
    artifact=manifest['artifact']
    if set(artifact)!={'name','bytes','sha256','unpacked_bytes'} or artifact['name']!='payload.tar.gz': raise ValueError('invalid artifact')
    if any(type(artifact[k]) is not int or artifact[k]<=0 for k in ('bytes','unpacked_bytes')) or not re.fullmatch('[a-f0-9]{64}',artifact['sha256']): raise ValueError('invalid artifact sizes/hash')
    dependencies=manifest['dependencies']
    if dependencies.get('python_packages')!=[] or dependencies.get('python_min')!=[3,9] or dependencies.get('signature')!='RSA-PSS-SHA256' or dependencies.get('openssl_min')!='1.1.1': raise ValueError('unsupported dependency lock')
    paths=set()
    for row in manifest['files']:
        path=safe_path(row['path'])
        if set(row)!={'path','bytes','sha256'} or path.parts[0]!='kernelx' or row['path'] in paths or type(row['bytes']) is not int or row['bytes']<0 or not re.fullmatch('[a-f0-9]{64}',row['sha256']): raise ValueError('invalid/duplicate indexed file')
        paths.add(row['path'])
    if sum(row['bytes'] for row in manifest['files'])!=artifact['unpacked_bytes'] or not paths: raise ValueError('unpacked size mismatch')
    for field,component in (('schemas','schemas'),('case_manifests','manifests')):
        expected={row['path']:row['sha256'] for row in manifest['files'] if component in Path(row['path']).parts}
        if not expected or manifest[field]!=expected: raise ValueError('schema/case manifest index mismatch')
    return manifest


def compatible(manifest,host):
    check_group(host)
    if manifest['compatibility']!=host: raise ValueError('release CPU/SoC/BIN/CANN/profiler compatibility mismatch')
    if platform.machine()!=host['cpu_arch']: raise ValueError('snapshot CPU architecture differs from actual host')


def extract_payload(package,destination,manifest):
    package=Path(package); destination=Path(destination)
    if package.stat().st_size!=manifest['artifact']['bytes'] or sha(package)!=manifest['artifact']['sha256']: raise ValueError('corrupt payload')
    indexed={row['path']:row for row in manifest['files']}; seen=set()
    destination.mkdir(exist_ok=False)
    with tarfile.open(package,'r:gz') as tar:
        for member in tar:
            relative=str(safe_path(member.name)); row=indexed.get(relative)
            if not member.isfile() or not row or relative in seen or member.size!=row['bytes']: raise ValueError('unindexed, duplicate, linked or invalid tar member')
            target=destination/relative; target.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source,output,1024*1024); output.flush(); os.fsync(output.fileno())
            if sha(target)!=row['sha256']: raise ValueError('extracted file hash mismatch')
            target.chmod(0o444); seen.add(relative)
    if seen!=set(indexed): raise ValueError('incomplete release payload')
    for directory in sorted((p for p in destination.rglob('*') if p.is_dir()),reverse=True): fsync_dir(directory)
    fsync_dir(destination)


def check_installed(directory,manifest):
    directory=Path(directory)
    for row in manifest['files']:
        path=directory/safe_path(row['path'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes'] or sha(path)!=row['sha256']: raise ValueError('installed release drift')
    if any(p.is_symlink() for p in directory.rglob('*')): raise ValueError('installed release contains symlink')
    actual={p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()}-{'manifest.json','manifest.sig'}
    if actual!={r['path'] for r in manifest['files']}: raise ValueError('unindexed installed application file')


def self_test():
    from .agent import Agent, Center
    from .cann_adapter import CannAddAdapter
    adapter=CannAddAdapter(); validate('case',adapter.manifest['case'])
    if case_key(adapter.manifest['case'])!=adapter.manifest['case_key']: raise ValueError('case key mismatch')
    # Read every shipped schema/manifest; no runtime, compiler or NPU initialized.
    for path in Path(__file__).parent.glob('schemas/*.json'): json.loads(path.read_text())
    return dict(healthy=True,protocol='latency-v1',stdlib_only=True,npu_used=False)
