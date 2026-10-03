"""Small linked protocol examples for runner, agent and analysis consumers."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernelx.protocol import case_key, validate
root = Path(__file__).resolve().parents[1]
def uid(n): return '00000000-0000-4000-8000-%012d' % n
sha = 'a' * 64
time = '2026-10-03T15:30:00Z'
common = dict(schema_version=1, protocol_version='latency-v1', extensions={})
def entity(**values): return dict(common, **values)
fact = dict(value='fixture-v1', status='KNOWN', reason=None, source=['fixture'], confidence='DECLARED_ONLY')
tensor = dict(shape=[8, 16], dtype='float16', layout='ND', stride=[16, 1])
case = entity(operator='Add', inputs=[tensor, tensor], outputs=[tensor], attributes={},
              input_generation=dict(algorithm='uniform', version='1', seed=17, input_sha256=sha), semantic_version='1')
key = case_key(case)
examples = dict(case=case,
 plan=entity(plan_id=uid(1), plan_sha256=sha, case_manifest_sha256=sha, policy_id=uid(2), policy_version=1,
             valid_from=time, valid_until='2026-10-04T15:30:00Z', case_keys=[key], preset='latency-v1',
             budget_seconds=3600, cleanup_reserve_seconds=60, priorities={key: 1}),
 session=entity(session_id=uid(3), server_id=uid(4), device_uids=[uid(5)], environment_id=uid(6), plan_id=uid(1),
                plan_sha256=sha, release_id='fixture', policy_id=uid(2), policy_version=1, window_id='fixture-window',
                started_at=time, ended_at=time, state='COMPLETED', resource_ledger=[dict(device_uid=uid(5), start_monotonic_ns=1, end_monotonic_ns=100)], reason=None),
 attempt=entity(attempt_id=uid(7), session_id=uid(3), task_id=uid(8), case_key=key, ordinal=0, started_at=time, ended_at=time,
                state='SUCCEEDED', exit_code=0, process_group=1234, release_status='RELEASED', released_at=time, evidence_ids=['fixture'], reason=None),
 artifact=entity(artifact_id=uid(9), uri='fixture://example/PROF', sha256=sha, bytes=1024, kind='RAW_PROF', created_at=time, redacted=True, media_type='application/x-tar'),
 profile=entity(profile_id=uid(10), attempt_id=uid(7), case_key=key, preset='latency-v1', preset_sha256=sha,
                collector_version=fact, exporter_version=fact, parser_version='fixture-v1', artifact_ids=[uid(9)], final_argv=['msprof', '<fixture>'],
                completeness='COMPLETE', export_status='KNOWN', attribution_status='KNOWN', expected_task_count=2, actual_task_count=2,
                task_mapping=[dict(iteration=0, rank=0, task_ids=['a']), dict(iteration=1, rank=0, task_ids=['b'])], quality=['VALID'], reason=None),
 observation=entity(observation_id=uid(11), profile_id=uid(10), attempt_id=uid(7), session_id=uid(3), environment_id=uid(6),
                    device_uid=uid(5), task_id=uid(8), case_key=key, input_sha256=sha, seed=17, round=0, iteration=0, rank=0,
                    phase='MEASURE', task_ids=['a'], metric=dict(name='task_duration_us', unit='us', boundary='single matched device task', definition_version='1'),
                    raw_samples=[1.25], completeness='COMPLETE', quality=['VALID']))
for name, data in examples.items():
    validate(name, data)
    (root / 'examples' / (name + '.json')).write_text(json.dumps(data, indent=2) + '\n')
