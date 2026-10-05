import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

driver=load('overnight_driver',HERE/'driver.py')
ci=load('overnight_ci',HERE/'ci.py')

class OfflineTests(unittest.TestCase):
    def test_adapter_self_test_checks_all_diagnostic_libraries(self):
        checked=driver.diagnostic_self_test(driver.candidates())
        self.assertEqual(checked,['deepgemm-ascend','sgl-kernel-npu','tile-kernels','torch-npu'])
        with patch.object(driver.DiagnosticAdapter,'prepare',None):
            with self.assertRaisesRegex(RuntimeError,'missing prepare'):
                driver.diagnostic_self_test(driver.candidates())

    def test_diagnostic_adapters_reach_runner_worker_launch(self):
        from kernelx import runner, libraries
        env=json.loads((ROOT/'tests/fixtures/910b1/environment.json').read_text())
        rows=driver.candidates()
        for library in ('sgl-kernel-npu','torch-npu','tile-kernels','deepgemm-ascend'):
            with self.subTest(library=library), tempfile.TemporaryDirectory() as temp:
                root=Path(temp)
                row=next(row for row in rows if row['library']==library)
                adapter=driver.DiagnosticAdapter(row)
                def build(output,environment=None):
                    output=Path(output);output.mkdir()
                    (output/'adapter.json').write_text(json.dumps(adapter.manifest))
                    return output/'adapter.json'
                start=datetime.now(timezone.utc)-timedelta(seconds=1)
                launch=Mock(side_effect=RuntimeError('CPU_TEST_WORKER_LAUNCH_REACHED'))
                with patch.object(runner,'probe',return_value=env), \
                     patch.object(libraries,'FrozenPerformanceAdapter',return_value=adapter), \
                     patch.object(adapter,'build',side_effect=build), \
                     patch.object(runner,'DeviceLock',return_value=Mock(__enter__=Mock(return_value=Mock(fd=42,path=root/'lock')),__exit__=Mock(return_value=False))), \
                     patch.object(runner,'release_check',return_value=dict(status='RELEASED',evidence=dict(stdout='No process in device.'))), \
                     patch.object(runner,'run_owned',launch):
                    result=runner.collect(root/'run',server_id=env['server_id'],device=0,
                        window_start=start.isoformat(),window_end=(start+timedelta(seconds=120)).isoformat(),
                        authorization_id='cpu-test',adapter_library=library,warmup=20,repeats=30)
                self.assertEqual(result['reason'],'CPU_TEST_WORKER_LAUNCH_REACHED')
                launch.assert_called_once()
                command=launch.call_args.args[0]
                self.assertEqual(command[2:5],['-m','kernelx.offline_benchmark','--spec'])
                prepared=json.loads((root/'run/preparation.json').read_text())
                spec=json.loads(Path(command[-1]).read_text())
                self.assertEqual(prepared,spec)
                self.assertEqual(spec['library'],library)
                self.assertEqual(spec['case']['case_key'],row['case_key'])

    def test_prepare_rejects_invalid_repeat_policy(self):
        adapter=driver.DiagnosticAdapter(driver.candidates()[0])
        for warmup,repeats in [(0,30),(20,1001),(True,30),(20,1.5)]:
            with self.subTest(warmup=warmup,repeats=repeats), self.assertRaises(ValueError):
                adapter.prepare(0,warmup,repeats,'raw','sidecar')

    def test_frozen_ci_shapes_include_layouts_and_boundary_tokens(self):
        sources=HERE/'ci_sources'
        metadata=json.loads((sources/'sources.json').read_text())
        commits={r['lib']:r['ref'] for r in metadata}
        bindings=[dict(library=lib,ci_root=str(sources/lib),source_commit=sha,version='test') for lib,sha in commits.items()]
        rows=ci.shape_cases(bindings,driver.make_case)
        sgl=[r for r in rows if r['library']=='sgl-kernel-npu']
        self.assertTrue(any(r['case']['inputs'][0]['shape']==[4,16,256] for r in sgl))
        self.assertTrue(any(r['case']['inputs'][0]['stride']==[8192,2] for r in sgl))
        tile=[r for r in rows if r['library']=='tile-kernels']
        self.assertTrue(any(r['case']['inputs'][0]['shape']==[8001,65536] for r in tile))
        self.assertTrue(any(r['case']['inputs'][0]['shape']==[1,65536] for r in tile))
        self.assertTrue(all(r['case']['attributes']['num_per_channels']==32 for r in tile))
        gemm=[r for r in rows if r['library']=='deepgemm-ascend']
        self.assertTrue(any([x['shape'] for x in r['case']['inputs']]==[[130,2048],[257,2048]] for r in gemm))
        self.assertTrue(any(r['case']['inputs'][1]['shape']==[129280,7168] for r in gemm))
    def test_version_mismatch_does_not_execute_current_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'kernelx/offline_ci_sources';root.mkdir(parents=True)
            (root/'sources.json').write_text(json.dumps([dict(lib='tile-kernels',ref='abc1234'+'0'*33)]))
            (root/'tile-kernels/tests').mkdir(parents=True)
            (root/'tile-kernels/tests/test_demo.py').write_text('def test_demo():\n    pass\n')
            with patch.object(ci,'installed',return_value=dict(library='tile-kernels',version='1.0',commit='badbeef',source_root=None)),patch.dict('os.environ',{'KERNELX_LIBRARY_ROOTS':'{}'}):
                bindings=ci.discover(temp,{'tile-kernels'})
            self.assertEqual(bindings[0]['status'],'VERSION_UNMATCHED')
            self.assertIsNone(bindings[0]['ci_root'])
            self.assertEqual(ci.tasks(bindings)[0],[])
            self.assertTrue(bindings[0]['inventory'])
    def test_build_commit_suffix_matches_embedded_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'kernelx/offline_ci_sources';root.mkdir(parents=True)
            (root/'sources.json').write_text(json.dumps([dict(lib='tile-kernels',ref='abc1234'+'0'*33)]))
            (root/'tile-kernels/tests').mkdir(parents=True)
            (root/'tile-kernels/tests/test_demo.py').write_text('def test_demo():\n    pass\n')
            with patch.object(ci,'installed',return_value=dict(library='tile-kernels',version='1.0+abc1234',commit='abc1234',source_root=None)),patch.dict('os.environ',{'KERNELX_LIBRARY_ROOTS':'{}'}):
                bindings=ci.discover(temp,{'tile-kernels'})
            self.assertEqual(bindings[0]['status'],'FROZEN_COMMIT_MATCHED')
            self.assertEqual(len(ci.tasks(bindings)[0]),1)
    def test_full_ci_generators_preserved_without_importing_torch(self):
        root=HERE/'ci_sources/tile-kernels'
        entries=ci.source_inventory(root,'tile-kernels')
        cast=next(e for e in entries if e['path']=='tests/quant/test_per_token_cast.py')
        self.assertTrue(any('generate_test_params(get_test_level())' in p['expression'] for p in cast['parameter_generators']))
        self.assertTrue(any('generate_num_tokens' in e['expression'] for e in cast['shape_expressions']))
    def test_model_attention_includes_decode_long_context_and_current_heads(self):
        rows=driver.candidates()
        attention=[r['case'] for r in rows if r['case']['operator']=='GQA-Attention']
        self.assertTrue(any(c['attributes']['query_tokens']==1 and c['attributes']['kv_tokens']==32768 for c in attention))
        self.assertTrue(any(c['attributes']['heads']==16 and c['attributes']['kv_heads']==2 and c['attributes']['head_dim']==256 for c in attention))
        self.assertEqual(len({r['case_key'] for r in rows}),len(rows))

if __name__=='__main__':unittest.main()
