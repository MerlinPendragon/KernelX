"""Durable case sealing and an idempotent reference central importer."""
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

from ..protocol import digest, validate, case_key
from ..device_lock import DeviceLock
from .policy import timestamp


def database(path):
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL'); db.execute('PRAGMA foreign_keys=ON')
    return db


def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def atomic_json(path,value):
    path=Path(path); temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w') as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,path); fsync_dir(path.parent)


def safe_path(value):
    path=PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ('.','..') for part in path.parts) or str(path)!=value:
        raise ValueError('unsafe artifact path')
    return path


def complete_case(root):
    root=Path(root)
    def read(name): return json.loads((root/(name+'.json')).read_text())
    result=read('result')
    if not result.get('valid') or result.get('device_release')!='RELEASED' or not result.get('released_by_hard_cutoff'): raise ValueError('case incomplete or device not released')
    entities={name:read(name) for name in ('environment','plan','session','attempt','profile')}
    for name,value in entities.items(): validate(name,value)
    session,attempt,profile=entities['session'],entities['attempt'],entities['profile']
    if session['state']!='COMPLETED' or attempt['state']!='SUCCEEDED' or profile['completeness']!='COMPLETE' or profile['quality']!=['VALID']: raise ValueError('partial entities cannot become successful')
    if attempt['session_id']!=session['session_id'] or profile['attempt_id']!=attempt['attempt_id'] or session['environment_id']!=entities['environment']['environment_id']: raise ValueError('entity references mismatch')
    plan=entities['plan']; authorization=read('authorization')
    if session['plan_id']!=plan['plan_id'] or session['plan_sha256']!=plan['plan_sha256'] or plan['plan_sha256']!=digest(read('manifest')) or profile['preset_sha256']!=digest(read('preset')):
        raise ValueError('plan/preset fingerprint mismatch')
    if attempt['release_status']!='RELEASED' or not attempt['released_at'] or not timestamp(authorization['window_start'])<=timestamp(session['started_at'])<=timestamp(attempt['released_at'])<=timestamp(authorization['window_end']):
        raise ValueError('release timestamp outside authorized window')
    case=read('manifest')['case']; validate('case',case); entities['case']=case
    if case_key(case)!=attempt['case_key'] or profile['case_key']!=attempt['case_key']: raise ValueError('case identity mismatch')
    observations=read('observations')
    if not observations or len(observations)!=result['observations'] or len({o['observation_id'] for o in observations})!=len(observations): raise ValueError('observation cardinality mismatch')
    for row in observations:
        validate('observation',row)
        if row['session_id']!=session['session_id'] or row['attempt_id']!=attempt['attempt_id'] or row['profile_id']!=profile['profile_id'] or row['environment_id']!=session['environment_id'] or row['case_key']!=attempt['case_key'] or row['quality']!=['VALID'] or row['device_uid'] not in session['device_uids']: raise ValueError('observation references/quality mismatch')
    artifacts=read('artifacts')
    if set(profile['artifact_ids'])!={a['artifact_id'] for a in artifacts}: raise ValueError('artifact references mismatch')
    for artifact in artifacts:
        validate('artifact',artifact)
        prefix='artifact://'+profile['profile_id']+'/'
        if not artifact['uri'].startswith(prefix): raise ValueError('artifact URI/profile mismatch')
        path=root / safe_path(artifact['uri'][len(prefix):])
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=artifact['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=artifact['sha256']: raise ValueError('artifact checksum mismatch')
    return entities,observations,artifacts


def seal(root):
    """Validate first, fsync every file, then publish the immutable commit marker."""
    root=Path(root); complete_case(root)
    files=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink(): raise ValueError('symlinks are not allowed in spool bundles')
        if path.is_file() and path.name not in ('sealed.json','sealed.json.tmp'):
            with path.open('rb') as stream: data=stream.read(); os.fsync(stream.fileno())
            files.append(dict(path=str(path.relative_to(root)),bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
    for path in sorted((p for p in root.rglob('*') if p.is_dir()),reverse=True): fsync_dir(path)
    body=dict(schema_version=1,files=files)
    body['bundle_id']=digest(body)
    atomic_json(root/'sealed.json',body)
    return body


def verify_bundle(root):
    root=Path(root); body=json.loads((root/'sealed.json').read_text())
    if set(body)!={'schema_version','files','bundle_id'} or body['schema_version']!=1 or digest({k:v for k,v in body.items() if k!='bundle_id'})!=body['bundle_id']: raise ValueError('bundle manifest mismatch')
    paths=set()
    for row in body['files']:
        relative=str(safe_path(row['path'])); path=root/relative
        if relative in paths or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()): raise ValueError('invalid bundle file')
        paths.add(relative)
        if path.stat().st_size!=row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']: raise ValueError('bundle file checksum mismatch')
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.name!='sealed.json'}
    if paths!=actual: raise ValueError('unindexed bundle files')
    complete_case(root)
    return body


class Center:
    """Local-filesystem reference for the durable central import/ACK contract."""
    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'bundles').mkdir(exist_ok=True)
        self.db=database(self.root/'center.db')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS entities(kind TEXT, identity TEXT, sha256 TEXT, payload TEXT, PRIMARY KEY(kind,identity));
        CREATE TABLE IF NOT EXISTS observations(observation_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(bundle_id TEXT PRIMARY KEY, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fleet_links(bundle_id TEXT PRIMARY KEY, global_run_id TEXT, dispatch_id TEXT, server_id TEXT, device_uid TEXT, agent_attempt_id TEXT, session_id TEXT, attempt_id TEXT);
        CREATE TABLE IF NOT EXISTS bundle_profiles(bundle_id TEXT, profile_id TEXT, PRIMARY KEY(bundle_id,profile_id));
        ''')

    def close(self): self.db.close()

    def entry(self,observation_id=None):
        row=self.db.execute('SELECT payload FROM observations WHERE observation_id=?',(observation_id,)).fetchone() if observation_id else self.db.execute('SELECT payload FROM observations ORDER BY rowid LIMIT 1').fetchone()
        if row is None: raise ValueError('observation not found')
        observation=json.loads(row['payload'])
        def entity(kind,identity):
            row=self.db.execute('SELECT payload FROM entities WHERE kind=? AND identity=?',(kind,identity)).fetchone()
            if row is None: raise ValueError('missing central entity reference')
            return json.loads(row['payload'])
        environment=entity('environment',observation['environment_id'])
        device=next(d for d in environment['devices'] if d['device_uid']==observation['device_uid'])
        return dict(observation,case=entity('case',observation['case_key']),
            hardware=dict(device=device,driver=environment['software']['driver'],firmware=environment['software']['firmware']),
            software=environment['software'],artifact_bundle_ids=[row['bundle_id'] for row in self.db.execute('SELECT bundle_id FROM bundle_profiles WHERE profile_id=?',(observation['profile_id'],))],fleet_links=[dict(row) for row in self.db.execute('SELECT * FROM fleet_links WHERE session_id=?',(observation['session_id'],))],library_provenance=environment['extensions'].get('library_provenance',{}))

    def artifact_path(self,artifact_id):
        row=self.db.execute('SELECT payload FROM entities WHERE kind=? AND identity=?',('artifact',artifact_id)).fetchone()
        if row is None: raise ValueError('artifact not found')
        artifact=json.loads(row['payload']); profile,relative=artifact['uri'][len('artifact://'):].split('/',1)
        for row in self.db.execute('SELECT bundle_id FROM bundle_profiles WHERE profile_id=?',(profile,)):
            path=self.root/'bundles'/row['bundle_id']/safe_path(relative)
            if path.is_file() and path.stat().st_size==artifact['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==artifact['sha256']: return path
        raise ValueError('artifact absent or checksum mismatch')

    def import_bundle(self,source):
        with DeviceLock('center-import',self.root/'.locks',timeout=15):
            return self._import_bundle(source)

    def _import_bundle(self,source):
        manifest=verify_bundle(source); identity=manifest['bundle_id']; destination=self.root/'bundles'/identity
        if not destination.exists():
            staging=self.root/'bundles'/('.'+identity+'.staging')
            if staging.exists(): shutil.rmtree(staging)
            shutil.copytree(source,staging)
            # Seal again fsyncs copied data before the directory is published.
            if seal(staging)!=manifest: raise ValueError('copied bundle differs')
            os.replace(staging,destination); fsync_dir(destination.parent)
        verify_bundle(destination)
        entities,observations,artifacts=complete_case(destination)
        context_path=destination/'agent-context.json'
        context=json.loads(context_path.read_text()) if context_path.exists() else {}
        fleet=context.get('fleet')
        if fleet and (context['policy']['server_id']!=entities['session']['server_id'] or context['task']['device_uid'] not in entities['session']['device_uids']): raise ValueError('fleet context differs from captured identity')
        receipt=dict(bundle_id=identity,manifest_sha256=digest(manifest),durable=True,observations=len(observations))
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for kind,value in list(entities.items())+[('artifact',a) for a in artifacts]:
                key=value.get(kind+'_id') if kind!='case' else case_key(value)
                existing=self.db.execute('SELECT sha256 FROM entities WHERE kind=? AND identity=?',(kind,key)).fetchone()
                if existing and existing['sha256']!=digest(value): raise ValueError('conflicting immutable entity identity')
                self.db.execute('INSERT OR IGNORE INTO entities VALUES(?,?,?,?)',(kind,key,digest(value),json.dumps(value)))
            for row in observations:
                existing=self.db.execute('SELECT sha256 FROM observations WHERE observation_id=?',(row['observation_id'],)).fetchone()
                if existing and existing['sha256']!=digest(row): raise ValueError('conflicting observation ID')
                self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)',(row['observation_id'],digest(row),json.dumps(row)))
            self.db.execute('INSERT OR IGNORE INTO imports VALUES(?,?)',(identity,json.dumps(receipt)))
            if fleet:
                self.db.execute('INSERT OR IGNORE INTO fleet_links VALUES(?,?,?,?,?,?,?,?)',(identity,fleet['global_run_id'],fleet['dispatch_id'],entities['session']['server_id'],context['task']['device_uid'],context['attempt_id'],entities['session']['session_id'],entities['attempt']['attempt_id']))
            self.db.execute('INSERT OR IGNORE INTO bundle_profiles VALUES(?,?)',(identity,entities['profile']['profile_id']))
        # ACK follows synchronous transaction commit and durable artifact publication.
        return receipt
