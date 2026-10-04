"""Review regressions. Synthetic provider fixtures exercise aggregation only;
no extension certification or NPU execution is claimed by these CPU tests.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kernelx.agent.fleet import Fleet
from kernelx.agent.storage import atomic_json, seal
from kernelx.libraries import CatalogAdapter, FrozenPerformanceAdapter, Registry, SupportMatrix
from kernelx.protocol import digest
from test_agent import ENV, DEVICE, fixture_run


def bundle(root, library='cann-opp', index=0, certification=None, bin_override=None):
    fixture_run(root)
    def read(name): return json.loads((root/(name+'.json')).read_text())
    env=read('environment')
    if library!='cann-opp':
        adapter=CatalogAdapter(library); manifest=FrozenPerformanceAdapter(library,index).manifest
        atomic_json(root/'manifest.json',manifest)
        key=manifest['case_key']
        for name in ('attempt','profile'):
            value=read(name);value['case_key']=key;atomic_json(root/(name+'.json'),value)
        observations=read('observations')
        for value in observations:value['case_key']=key
        atomic_json(root/'observations.json',observations)
        plan=read('plan');plan['plan_sha256']=digest(manifest);plan['case_keys']=[key]
        atomic_json(root/'plan.json',plan)
        session=read('session');session['plan_sha256']=digest(manifest)
        atomic_json(root/'session.json',session)
        row=next(r for r in env['software']['operator_libraries'] if r['name']==library)
        row.update(git_commit=adapter.catalog['upstream']['commit'],resolved_path='CPU_ONLY_TRUSTED_ATTESTER_FIXTURE')
        for p in env['software']['packages']:
            p['version'].update(status='KNOWN',value='99.0.0',reason=None,confidence='VERIFIED')
        triton=copy.deepcopy(env['software']['packages'][0]);triton['name']='triton'
        env['software']['packages'].append(triton)
        # A controlled future-attester boundary, never emitted by the current
        # diagnostic benchmark. It tests sealed evidence aggregation, not loading.
        env['extensions']['runtime_host_providers']=dict(load_status='VERIFIED',evidence_type='CPU_ONLY_TRUSTED_ATTESTER_FIXTURE')
    if certification:env['extensions']['runtime_host_providers']['certification_status']=certification
    if bin_override:next(d for d in env['devices'] if d['device_uid']==DEVICE)['hardware_bin']['value']=bin_override
    atomic_json(root/'environment.json',env)
    profile=read('profile');artifacts=read('artifacts')
    for row in artifacts:
        path=root/row['uri'].split('artifact://'+profile['profile_id']+'/',1)[1]
        row.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)
    atomic_json(root/'artifacts.json',artifacts);seal(root)
    return env


def submit(fleet, environment, capabilities, identity, library='cann-opp', scope='core'):
    return fleet.submit(dict(submission_id=identity,libraries=[library],scope=scope,
        servers=[dict(server_id=environment['server_id'],device_uid=DEVICE,capabilities={c['library']:c for c in capabilities})],
        mode='paired',valid_from='2026-01-01T00:00:00Z',valid_until='2030-01-01T00:00:00Z',warmup=20,repeats=10,pilot_upper_seconds=1,estimated_output_bytes=1024))


class ReviewContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.matrix=SupportMatrix(self.root/'matrix');self.addCleanup(lambda:self.matrix.close())
        self.fleet=Fleet(self.root/'fleet');self.addCleanup(self.fleet.close)

    def test_cann_attestation_inventory_submitted_unchanged(self):
        root=self.root/'bundle';env=bundle(root)
        self.assertEqual(self.matrix.attest_bundle(root,'cann-opp',DEVICE)['status'],'VERIFIED')
        inventory=Registry().inventory(env,DEVICE,matrix=self.matrix)
        run=submit(self.fleet,env,inventory,'canonical-inventory')
        dispatch=self.fleet.status(run)['dispatches'][0]
        self.assertEqual(dispatch['state'],'PENDING')
        self.assertEqual(json.loads(dispatch['plan'])['tasks'][0]['adapter'],'cann-add')
        stale=copy.deepcopy(inventory);stale[0]['environment_tuple']['hardware_bin']='different'
        run=submit(self.fleet,env,stale,'stale-inventory')
        self.assertEqual(self.fleet.status(run)['dispatches'][0]['state'],'UNVERIFIED')

    def test_full_case_evidence_persists_and_inventory_drives_dispatch(self):
        library='sgl-kernel-npu';environments=[]
        for index in range(3):
            root=self.root/str(index);env=bundle(root,library,index);environments.append(env)
            row=self.matrix.attest_bundle(root,library,DEVICE,'full')
            self.assertEqual(row['status'],'VERIFIED' if index==2 else 'UNVERIFIED')
            self.assertEqual(len(json.loads(row['evidence'])['missing_case_keys']),2-index)
            self.matrix.close();self.matrix=SupportMatrix(self.root/'matrix')
            inventory=Registry().inventory(env,DEVICE,'full',self.matrix)
            run=submit(self.fleet,env,inventory,'coverage-'+str(index),library,'full')
            dispatches=self.fleet.status(run)['dispatches']
            self.assertEqual(len(dispatches),3 if index==2 else 1)
            self.assertTrue(all(d['state']==('PENDING' if index==2 else 'UNVERIFIED') for d in dispatches))
        other=self.root/'other-bin';bundle(other,library,0,bin_override='other')
        row=self.matrix.attest_bundle(other,library,DEVICE,'full')
        self.assertEqual(row['status'],'UNVERIFIED')
        self.assertEqual(len(json.loads(row['evidence'])['missing_case_keys']),2)
        row=self.matrix.attest_bundle(self.root/'0',library,DEVICE,'core')
        self.assertEqual(len(json.loads(row['evidence'])['case_evidence']),1)

    def test_unimplemented_provider_cannot_certify_even_with_confirmation_flag(self):
        for library in Registry().adapters:
            source=self.root/(library+'.json');loaded=self.root/(library+'.so');loaded.write_bytes(b'CPU_ONLY')
            atomic_json(source,dict(provider_confirmed=True,git_commit=CatalogAdapter(library).catalog['upstream']['commit'],loaded_files=[str(loaded)],jit_artifacts=[]))
            result=CatalogAdapter(library).resolve_versions(source)
            self.assertEqual(result['load_status'],'DECLARED_ONLY')
            self.assertEqual(result['certification_status'],'UNIMPLEMENTED')
            self.assertEqual(result['loaded_files'][0]['sha256'],hashlib.sha256(b'CPU_ONLY').hexdigest())
        root=self.root/'unimplemented';bundle(root,'sgl-kernel-npu',certification='UNIMPLEMENTED')
        with self.assertRaisesRegex(ValueError,'UNIMPLEMENTED'):self.matrix.attest_bundle(root,'sgl-kernel-npu',DEVICE)
        self.assertEqual(self.matrix.rows(),[])
