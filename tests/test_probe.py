import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from kernelx.probe import Collector, classify, fields, parse_mapping, probe
from kernelx.protocol import digest, validate

FIXTURE = Path(__file__).parent / 'fixtures/910b1/environment.json'
SERVER = '55dcc47c-f8a8-4f3f-ab2d-c02bd385c470'

class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.env = json.loads(FIXTURE.read_text())

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

    def replay(self, mutation=None, server_id=SERVER):
        records = {tuple(e['argv']): e for e in self.env['evidence']}
        def run(collector, argv):
            original = records.get(tuple(argv))
            if original:
                record = collector.record(argv, original['stdout'], original['stderr'], original['exit_code'], original['execution_status'])
            else:
                record = collector.record(argv, exit_code=None, status='NOT_FOUND', reason='not in fixture')
            if mutation: mutation(record)
            return record
        with patch.object(Collector, 'run', run), patch('kernelx.probe.platform.machine', return_value='aarch64'):
            return probe(server_id)

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
