import csv
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from kernelx.profile_parser import parse_add

FIXTURE=Path(__file__).parent/'fixtures/cann_add'

class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for path in FIXTURE.iterdir(): shutil.copy(path,self.root/path.name)
        self.csv=next(self.root.glob('op_summary_*.csv'))
        self.trace=next(self.root.glob('msprof_[0-9]*.json'))
        self.tx=next(self.root.glob('msprof_tx_*.json'))
        self.sidecar=self.root/'sidecar.jsonl'
    def parse(self): return parse_add(self.csv,self.trace,self.tx,self.sidecar,5,10,3)
    def mutate_rows(self,fn):
        with self.csv.open() as source:
            reader=csv.DictReader(source); columns=reader.fieldnames; rows=list(reader)
        fn(rows)
        with self.csv.open('w',newline='') as dest:
            writer=csv.DictWriter(dest,fieldnames=columns);writer.writeheader();writer.writerows(rows)
    def mutate_trace(self,path,fn):
        data=json.loads(path.read_text());fn(data);path.write_text(json.dumps(data))

    def test_real_fixture_and_row_order(self):
        first=self.parse();self.assertTrue(first['valid'],first['reasons'])
        self.assertEqual(first['actual_task_count'],10)
        self.assertEqual([i['iteration'] for i in first['invocations']],list(range(10)))
        self.assertEqual(first['invocations'][0]['task_duration_us'],[4.24])
        self.assertEqual(first['invocations'][0]['device_span_us'],4.24)
        self.assertNotEqual(first['invocations'][0]['host_elapsed_us'],4.24)
        self.mutate_rows(lambda rows: rows.reverse())
        self.assertEqual(first['invocations'],self.parse()['invocations'])
        self.assertIn('Context ID',first['raw_rows'][0])
    def test_multiple_correlated_tasks_use_span_not_sum_or_average(self):
        from kernelx.profile_parser import parse_ranges
        # Add an overlapping second task to each invocation. CPU transformation
        # tests attribution math only; this is not another library's NPU result.
        def add_rows(rows):
            extra=[]
            for row in rows:
                clone=dict(row);clone['Task ID']=str(int(row['Task ID'])+10000);clone['OP Type']='Other';clone['Op Name']='second';extra.append(clone)
            rows.extend(extra)
        self.mutate_rows(add_rows)
        def add_trace(data):
            import copy
            for event in list(data):
                if event.get('args',{}).get('Task Id') is not None:
                    clone=copy.deepcopy(event);clone['args']['Task Id']=int(event['args']['Task Id'])+10000;clone['name']='second';data.append(clone)
        self.mutate_trace(self.trace,add_trace)
        result=parse_ranges(self.csv,self.trace,self.tx,self.sidecar,5,10,3)
        self.assertTrue(result['valid'],result['reasons']);self.assertEqual(result['actual_task_count'],20)
        first=result['invocations'][0];self.assertEqual(first['task_duration_us'],[4.24,4.24]);self.assertEqual(first['device_span_us'],4.24)
        self.assertFalse(self.parse()['valid'])
        self.assertFalse(parse_ranges(self.csv,self.trace,self.tx,self.sidecar,5,10,3,rank=1)['valid'])

    def test_missing_trace_preserves_csv_but_invalidates(self):
        self.trace.unlink();result=self.parse()
        self.assertFalse(result['valid']);self.assertEqual(result['quality'],['TRACE_MISSING'])
        self.assertEqual(len(result['raw_rows']),10);self.assertEqual(result['invocations'],[])
    def test_unknown_units_rejected(self):
        self.csv.write_text(self.csv.read_text().replace('Task Duration(us)','Task Duration(ns)'))
        self.assertEqual(self.parse()['quality'],['UNIT_UNKNOWN'])
    def test_missing_task_rejected(self):
        self.mutate_rows(lambda rows: rows.pop())
        self.assertEqual(self.parse()['quality'],['INSUFFICIENT_DATA'])
    def test_overlapping_ranges_rejected(self):
        self.mutate_trace(self.tx,lambda data: data[0].update(dur=100000))
        self.assertEqual(self.parse()['quality'],['ATTRIBUTION_UNKNOWN'])
    def test_decimal_precision_detects_sub_float_ulp_mismatch(self):
        def mutate(rows): rows[0]['Task Start Time(us)']=str(Decimal(rows[0]['Task Start Time(us)'].strip())+Decimal('.1'))
        self.mutate_rows(mutate)
        self.assertEqual(self.parse()['quality'],['ATTRIBUTION_UNKNOWN'])
    def test_phase_order_and_release_required(self):
        self.sidecar.write_text('\n'.join(self.sidecar.read_text().splitlines()[:-1]))
        self.assertEqual(self.parse()['quality'],['ATTRIBUTION_UNKNOWN'])
    def test_wrong_device_trace_rejected(self):
        def mutate(data):
            for event in data:
                if event.get('name')=='process_labels' and event.get('args',{}).get('labels')=='NPU 5': event['args']['labels']='NPU 4'
        self.mutate_trace(self.trace,mutate)
        self.assertEqual(self.parse()['quality'],['ATTRIBUTION_UNKNOWN'])
    def test_duplicate_task_ids_rejected(self):
        def mutate(data):
            task=next(e for e in data if e.get('args',{}).get('Task Id') is not None);data.append(task)
        self.mutate_trace(self.trace,mutate)
        self.assertEqual(self.parse()['quality'],['ATTRIBUTION_UNKNOWN'])
    def test_nonfinite_and_negative_duration_rejected(self):
        for value in ('nan','-1'):
            self.mutate_rows(lambda rows: rows[0].update({'Task Duration(us)':value}))
            self.assertEqual(self.parse()['quality'],['UNIT_UNKNOWN'])

class Warmup20DeliveryTests(unittest.TestCase):
    def test_formal_run_profiles_only_ten_measurements(self):
        root=Path(__file__).parent/'fixtures/cann_add_warmup20'
        exported=root/'exports'
        result=parse_add(next(exported.glob('op_summary_*.csv')),next(exported.glob('msprof_[0-9]*.json')),
                         next(exported.glob('msprof_tx_*.json')),root/'sidecar.jsonl',5,10,20)
        self.assertTrue(result['valid'],result['reasons']);self.assertEqual(result['actual_task_count'],10)
        phases=[json.loads(line) for line in (root/'sidecar.jsonl').read_text().splitlines()]
        warmups=[p for p in phases if p['phase']=='WARMUP']
        self.assertEqual(len(warmups),20)
        self.assertTrue(all(p['host_elapsed_us']>=0 and not p['profile_active'] for p in warmups))
        self.assertFalse(parse_add(next(exported.glob('op_summary_*.csv')),next(exported.glob('msprof_[0-9]*.json')),
                                  next(exported.glob('msprof_tx_*.json')),root/'sidecar.jsonl',5,10,3)['valid'])
    def test_linked_protocol_entities_and_real_fault_evidence(self):
        import hashlib
        from urllib.parse import urlparse
        from kernelx.protocol import validate
        root=Path(__file__).parent/'fixtures/cann_add_warmup20'
        entities={name:json.loads((root/(name+'.json')).read_text()) for name in ('environment','attempt','profile','session','plan')}
        for name,value in entities.items(): validate(name,value)
        artifacts=json.loads((root/'artifacts.json').read_text())
        for artifact in artifacts: validate('artifact',artifact)
        artifact_ids={a['artifact_id'] for a in artifacts}
        self.assertTrue(set(entities['profile']['artifact_ids'])<=artifact_ids)
        origin=json.loads((root/'fixture-origin.json').read_text())
        for artifact in artifacts:
            uri=urlparse(artifact['uri'])
            self.assertEqual(uri.netloc,entities['profile']['profile_id'])
            if artifact['kind']=='RAW_PROF': continue # retained separately with original bytes
            path=root/origin['source_to_fixture_paths'][uri.path.lstrip('/')]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),artifact['sha256'])
            self.assertEqual(path.stat().st_size,artifact['bytes'])
        observations=json.loads((root/'observations.json').read_text())
        self.assertEqual(len(observations),30)
        for observation in observations:
            validate('observation',observation)
            self.assertEqual(observation['profile_id'],entities['profile']['profile_id'])
            self.assertEqual(observation['environment_id'],entities['environment']['environment_id'])
            self.assertEqual(observation['session_id'],entities['session']['session_id'])
        for fault in json.loads((root/'fault-acceptance.json').read_text()):
            self.assertTrue(fault['external_sentinel_alive']);self.assertTrue(fault['profiling_active_before_termination'])
            self.assertFalse(fault['result']['valid']);self.assertEqual(fault['result']['observations'],0)
            self.assertEqual(fault['result']['device_release'],'RELEASED');self.assertTrue(fault['result']['released_by_hard_cutoff'])

if __name__=='__main__': unittest.main()
