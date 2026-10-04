import copy
import json
import importlib.metadata
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from kernelx.probe import BIN_MAPPING, BIN_MAPPING_VERSION, Collector, classify, fields, parse_mapping, probe, _library_provenance
from kernelx.protocol import digest, validate

FIXTURE = Path(__file__).parent / 'fixtures/910b1/environment.json'
SERVER = '55dcc47c-f8a8-4f3f-ab2d-c02bd385c470'

class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.env = json.loads(FIXTURE.read_text())

    def test_package_git_provenance(self):
        commit = 'a' * 40
        dist = Mock(version='1.2.3')
        dist.read_text.return_value = json.dumps(dict(url='https://example.org/ops.git', vcs_info=dict(vcs='git', commit_id=commit)))
        with patch('kernelx.probe.importlib.metadata.distribution', return_value=dist):
            result = _library_provenance(Collector(SERVER), 'ops-nn')
        self.assertEqual(result['package_id'], 'ops-nn==1.2.3')
        self.assertEqual(result['git_commit'], commit)
        self.assertEqual(result['commit_status'], 'KNOWN')
        self.assertEqual(result['confidence'], 'DECLARED_ONLY')
        self.assertTrue(result['commit_source'])

    def test_no_invented_commit_without_source(self):
        with patch('kernelx.probe.importlib.metadata.distribution', side_effect=importlib.metadata.PackageNotFoundError):
            result = _library_provenance(Collector(SERVER), 'tile-kernels')
        self.assertIsNone(result['git_commit'])
        self.assertEqual(result['commit_status'], 'UNKNOWN')
        self.assertTrue(result['commit_reason'])

    def test_explicit_repo_records_commit_and_dirty_state(self):
        collector = Collector(SERVER)
        outputs = [str(Path('/tmp/library').resolve()), 'b' * 40, 'https://example.org/library.git', ' M kernel.cpp']
        def run(argv):
            return collector.record(argv, outputs.pop(0))
        with patch('kernelx.probe.importlib.metadata.distribution', side_effect=importlib.metadata.PackageNotFoundError), patch.object(collector, 'run', side_effect=run):
            result = _library_provenance(collector, 'tile-kernels', '/tmp/library')
        self.assertEqual(result['git_commit'], 'b' * 40)
        self.assertTrue(result['source_tree_dirty'])

    def test_parent_repo_not_attributed_to_library(self):
        collector = Collector(SERVER)
        with patch('kernelx.probe.importlib.metadata.distribution', side_effect=importlib.metadata.PackageNotFoundError), patch.object(collector, 'run', return_value=collector.record(['git'], '/tmp')):
            result = _library_provenance(collector, 'ops-transformer', '/tmp/library')
        self.assertIsNone(result['git_commit'])
        self.assertEqual(result['commit_status'], 'UNKNOWN')

    def test_mapping_fixture_excludes_mcus(self):
        record = copy.deepcopy(next(e for e in self.env['evidence'] if e['argv'] == ['npu-smi', 'info', '-m']))
        rows = parse_mapping(record)
        self.assertEqual(len(rows), 8)
        self.assertEqual([r['logical_id'] for r in rows], list(range(8)))
        self.assertTrue(all(r['chip_id'] == 0 for r in rows))

    def test_mapping_parse_error(self):
        record = Collector(SERVER).record(['npu-smi', 'info', '-m'], 'unknown format')
        self.assertEqual(parse_mapping(record), [])
        self.assertEqual(record['parse_status'], 'PARSE_ERROR')
        self.assertTrue(record['reason'])

    def test_command_outcomes(self):
        self.assertEqual(classify(1, 'Permission denied'), 'PERMISSION_DENIED')
        self.assertEqual(classify(0, 'Error parameter of -t'), 'UNSUPPORTED')
        self.assertEqual(classify(1, 'No such file or directory'), 'NOT_FOUND')
        self.assertEqual(classify(127, 'error while loading shared libraries: missing'), 'COMMAND_FAILED')
        c = Collector(SERVER)
        with patch('kernelx.probe.subprocess.run', side_effect=FileNotFoundError('missing')):
            self.assertEqual(c.run(['missing'])['execution_status'], 'NOT_FOUND')
        with patch('kernelx.probe.subprocess.run', side_effect=PermissionError('denied')):
            self.assertEqual(c.run(['denied'])['execution_status'], 'PERMISSION_DENIED')
        with patch('kernelx.probe.subprocess.run', side_effect=subprocess.TimeoutExpired(['slow'], 1, b'partial')):
            entry = c.run(['slow'])
            self.assertEqual(entry['execution_status'], 'TIMEOUT')
            self.assertEqual(entry['stdout'], 'partial')
            self.assertIsNone(entry['exit_code'])

    def test_redaction_preserves_replayable_format(self):
        c = Collector(SERVER)
        entry = c.record(['test'], 'VDie ID : CC21EE64 12345678\nNDie ID : 00000000 00000000\nSerial Number : ABC123\n/home/someone/data 10.0.0.1')
        self.assertNotIn('CC21EE64', entry['stdout'])
        self.assertNotIn('ABC123', entry['stdout'])
        self.assertNotIn('/home/someone', entry['stdout'])
        self.assertNotIn('10.0.0.1', entry['stdout'])
        self.assertEqual(fields(entry['stdout'])['NDie ID'], 'NA')
        self.assertTrue(fields(entry['stdout'])['VDie ID'].startswith('redacted:'))
        self.assertEqual(c.sanitize(entry['stdout']), entry['stdout'])
        self.assertEqual(entry['output_sha256'], digest(dict(stdout=entry['stdout'], stderr=entry['stderr'])))
        self.assertNotEqual(c.sanitize('VDie ID : 1234'), Collector('00000000-0000-4000-8000-000000000001').sanitize('VDie ID : 1234'))

    def replay(self, mutation=None, server_id=SERVER, redact=True, raw_transform=None):
        records = {tuple(e['argv']): e for e in self.env['evidence']}
        def run(collector, argv):
            original = records.get(tuple(argv))
            if original:
                original = copy.deepcopy(original)
                if raw_transform:
                    raw_transform(original)
                record = collector.record(argv, original['stdout'], original['stderr'], original['exit_code'], original['execution_status'])
            else:
                record = collector.record(argv, exit_code=None, status='NOT_FOUND', reason='not in fixture')
            if mutation: mutation(record)
            return record
        with patch.object(Collector, 'run', run), patch('kernelx.probe.platform.machine', return_value='aarch64'):
            return probe(server_id, redact=redact)

    def test_documented_models_identified_in_synthetic_replay(self):
        # Name replacements test parser coverage, not target hardware acceptance.
        for model in ('910B1', '910B2', '910B2C', '910B3', '910B4'):
            def transform(record):
                record['stdout'] = record['stdout'].replace('910B1', model)
            result = self.replay(raw_transform=transform)
            self.assertEqual(len(result['devices']), 8)
            self.assertTrue(all(d['soc_family']['value'] == 'Ascend 910B' for d in result['devices']))
            self.assertTrue(all(d['hardware_bin']['value'] == 'Ascend ' + model for d in result['devices']))
            self.assertTrue(all(d['bin_mapping_version'] == BIN_MAPPING_VERSION for d in result['devices']))
            self.assertTrue(all(s['hardware_bin'] == 'Ascend ' + model for s in result['support_matrix']))
            self.assertTrue(all(s['status'] == 'UNVERIFIED' for s in result['support_matrix']))
            self.assertEqual(BIN_MAPPING['Ascend' + model], BIN_MAPPING[model])

    def test_identity_display_modes_and_redacted_replay_agree(self):
        def raw_chip(record):
            if 'board' in record['argv']:
                record['stdout'] = re.sub(r'redacted:[0-9a-f]{64}', 'CC21EE64 12345678', record['stdout'])
        public = self.replay(raw_transform=raw_chip)
        private = self.replay(raw_transform=raw_chip, redact=False)
        self.assertEqual([d['device_uid'] for d in public['devices']], [d['device_uid'] for d in private['devices']])
        self.assertTrue(all(d['identity_confidence'] == 'STABLE_CHIP' for d in public['devices'] + private['devices']))
        self.assertNotIn('CC21EE64', json.dumps(public))
        self.assertIn('CC21EE64', json.dumps(private))
        self.assertEqual(Collector(SERVER).identity_token('CC21EE64 12345678'),
                         Collector(SERVER, redact=False).identity_token('CC21EE64 12345678'))
        token = Collector(SERVER).identity_token('CC21EE64 12345678')
        self.assertEqual(Collector(SERVER).identity_token(token), token)
        # Existing default-redacted v1 UIDs remain stable after this fix.
        replayed = self.replay()
        self.assertEqual([d['device_uid'] for d in replayed['devices']], [d['device_uid'] for d in self.env['devices']])

    def test_unavailable_chip_identity_falls_back_in_both_modes(self):
        for value in ('00000000 00000000', 'NA', 'UNKNOWN'):
            self.assertIsNone(Collector(SERVER).identity_token(value))
            def raw_chip(record):
                if 'board' in record['argv']:
                    record['stdout'] = re.sub(r'redacted:[0-9a-f]{64}', value, record['stdout'])
            public, private = self.replay(raw_transform=raw_chip), self.replay(raw_transform=raw_chip, redact=False)
            self.assertEqual([d['device_uid'] for d in public['devices']], [d['device_uid'] for d in private['devices']])
            self.assertTrue(all(d['identity_confidence'] == 'LOCATION_ONLY' for d in public['devices']))

    def test_version_context_survives_ip_redaction(self):
        c = Collector(SERVER)
        for version in ('8.0.0.1', '8.0.0.2'):
            text = 'Version=' + version + '\nDriver Version : ' + version + '\nnpu-smi ' + version + '\nmsprof version ' + version
            text += '\n' + json.dumps({'version': version, 'host': '10.0.0.1'})
            text += '\nIP Address: 192.168.0.1\nendpoint http://10.0.0.1:80\nVersion=' + version + '; IP=10.1.2.3'
            text += '\nrequired_package_runtime_version=">=' + version + '"'
            sanitized = c.sanitize(text)
            self.assertEqual(sanitized.count(version), 7)
            for address in ('10.0.0.1', '192.168.0.1', '10.1.2.3'):
                self.assertNotIn(address, sanitized)
            self.assertEqual(c.sanitize(sanitized), sanitized)

    def test_different_four_segment_driver_versions_remain_distinct(self):
        results = []
        for version in ('8.0.0.1', '8.0.0.2'):
            def transform(record):
                if record['argv'] == ['cat', '/usr/local/Ascend/driver/version.info']:
                    record['stdout'] = record['stdout'].replace('25.5.0', version)
            result = self.replay(raw_transform=transform)
            self.assertEqual(result['software']['driver']['value'], version)
            results.append(result['software']['driver']['value'])
        self.assertNotEqual(*results)

    def test_full_probe_replays_board_and_mapping(self):
        result = self.replay()
        validate('environment', result)
        self.assertEqual(len(result['devices']), 8)
        for d in result['devices']:
            self.assertEqual(d['hardware_bin']['value'], 'Ascend 910B1')
            self.assertEqual(d['hardware_bin']['status'], 'KNOWN')
            self.assertEqual(d['identity_confidence'], 'STABLE_CHIP')
            self.assertEqual(d['card_identity_confidence'], 'LOCATION_ONLY')
        self.assertEqual([d['device_uid'] for d in result['devices']], [d['device_uid'] for d in self.replay()['devices']])
        self.assertNotEqual(result['devices'][0]['device_uid'], self.replay(server_id='00000000-0000-4000-8000-000000000001')['devices'][0]['device_uid'])

    def test_unknown_board_and_mismatch_fail_closed(self):
        def mutate(record):
            if 'board' in record['argv']:
                record['stdout'] = record['stdout'].replace('910B1', '910B99')
                record['output_sha256'] = digest(dict(stdout=record['stdout'], stderr=record['stderr']))
        result = self.replay(mutate)
        self.assertTrue(all(d['hardware_bin']['status'] == 'UNKNOWN' for d in result['devices']))
        self.assertTrue(all(d['hardware_bin']['value'] is None for d in result['devices']))

    def test_fixture_provenance_and_no_support_inference(self):
        validate('environment', self.env)
        self.assertEqual(self.env['software']['firmware']['status'], 'PERMISSION_DENIED')
        self.assertEqual(self.env['software']['msprof']['status'], 'UNSUPPORTED')
        self.assertEqual(self.env['software']['toolkit']['confidence'], 'DECLARED_ONLY')
        self.assertTrue(self.env['extensions']['version_conflicts'])
        self.assertTrue(all(x['status'] == 'UNVERIFIED' for x in self.env['support_matrix']))

if __name__ == '__main__': unittest.main()
