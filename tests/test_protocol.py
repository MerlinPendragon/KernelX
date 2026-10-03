import copy
import json
import unittest
from pathlib import Path

from kernelx.protocol import ValidationError, canonical_json, case_key, comparison_key, digest, validate

ROOT = Path(__file__).resolve().parents[1]

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / 'examples/case.json').read_text())
        self.env = json.loads((ROOT / 'tests/fixtures/910b1/environment.json').read_text())

    def test_all_examples_validate(self):
        for path in (ROOT / 'examples').glob('*.json'):
            validate(path.stem, json.loads(path.read_text()))
        validate('environment', self.env)

    def test_stable_key_for_reordered_objects(self):
        reordered = dict(reversed(list(self.case.items())))
        reordered['inputs'][0] = dict(reversed(list(reordered['inputs'][0].items())))
        self.assertEqual(case_key(self.case), case_key(reordered))

    def test_semantic_changes_change_key(self):
        mutations = [lambda c: c.update(semantic_version='2'), lambda c: c.update(operator='Mul'),
                     lambda c: c['inputs'][0]['shape'].__setitem__(0, 9),
                     lambda c: c['inputs'][0].update(dtype='float32'),
                     lambda c: c['inputs'][0].update(layout='NZ'),
                     lambda c: c['inputs'][0].update(stride=[1, 8]),
                     lambda c: c['attributes'].update(alpha=2),
                     lambda c: c['input_generation'].update(seed=18),
                     lambda c: c['input_generation'].update(version='2')]
        for mutate in mutations:
            changed = copy.deepcopy(self.case)
            mutate(changed)
            self.assertNotEqual(case_key(self.case), case_key(changed))

    def test_schema_version_and_required_fields_rejected(self):
        for field in self.case:
            changed = copy.deepcopy(self.case)
            del changed[field]
            with self.assertRaises(ValidationError): validate('case', changed)
        for value in (2, True, '1'):
            changed = dict(self.case, schema_version=value)
            with self.assertRaises(ValidationError): validate('case', changed)
        with self.assertRaises(ValidationError): validate('case', dict(self.case, protocol_version='latency-v2'))
        with self.assertRaises(ValidationError): validate('case', dict(self.case, extra='unexpected'))

    def test_bad_numbers_rejected(self):
        for number in (float('nan'), float('inf')):
            with self.assertRaises(ValueError): canonical_json(dict(value=number))
        changed = copy.deepcopy(self.case)
        changed['inputs'][0]['shape'][0] = True
        with self.assertRaises(ValidationError): validate('case', changed)

    def test_unknown_fact_and_evidence_integrity(self):
        for mutate in (lambda e: e['software']['firmware'].update(value='fake'),
                       lambda e: e['software']['firmware'].update(reason=None),
                       lambda e: e['software']['driver'].update(value=None),
                       lambda e: e['software']['driver'].update(source=['missing']),
                       lambda e: e['evidence'][0].update(stdout='tampered')):
            changed = copy.deepcopy(self.env)
            mutate(changed)
            with self.assertRaises(ValidationError): validate('environment', changed)

    def test_quality_and_complete_sample_rules(self):
        obs = json.loads((ROOT / 'examples/observation.json').read_text())
        for changed in (dict(obs, raw_samples=[]), dict(obs, quality=['VALID', 'CONTENDED']),
                        dict(obs, completeness='PARTIAL'), dict(obs, raw_samples=[-1]),
                        dict(obs, metric=dict(obs['metric'], unit='ns'))):
            with self.assertRaises(ValidationError): validate('observation', changed)
        profile = json.loads((ROOT / 'examples/profile.json').read_text())
        with self.assertRaises(ValidationError): validate('profile', dict(profile, actual_task_count=1))

    def test_strict_grouping_fail_closed(self):
        context = dict(runtime_library_sha256='a'*64, framework_sha256='b'*64, implementation_sha256='c'*64,
                       preset_sha256='d'*64, parser_version='1', protocol_version='latency-v1', compile_options=[],
                       concurrency=1, thermal_state='warm', communication={})
        with self.assertRaises(ValidationError): comparison_key(self.case, self.env, self.env['devices'][0], context)
        env = copy.deepcopy(self.env)
        for name in ('firmware', 'toolkit', 'msprof'):
            env['software'][name].update(value='fixture', status='KNOWN', confidence='VERIFIED', reason=None)
        key = comparison_key(self.case, env, env['devices'][0], context)
        env['devices'][0]['hardware_bin'].update(value=None, status='UNKNOWN', confidence='NONE', reason='not known')
        with self.assertRaises(ValidationError): comparison_key(self.case, env, env['devices'][0], context)
        env = copy.deepcopy(self.env)
        for name in ('firmware', 'toolkit', 'msprof'):
            env['software'][name].update(value='fixture', status='KNOWN', confidence='VERIFIED', reason=None)
        context['preset_sha256'] = 'e'*64
        self.assertNotEqual(key, comparison_key(self.case, env, env['devices'][0], context))
        env['software']['driver']['value'] = '8.0.0.1'
        first = comparison_key(self.case, env, env['devices'][0], context)
        env['software']['driver']['value'] = '8.0.0.2'
        self.assertNotEqual(first, comparison_key(self.case, env, env['devices'][0], context))

if __name__ == '__main__': unittest.main()
