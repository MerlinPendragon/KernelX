"""Versioned msprof Add attribution: exact Decimal timestamps, explicit units.

Only the validated single-device, synchronous Add protocol is accepted. Every
measured invocation must have one task inside its unique msproftx trace range.
"""
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

PARSER_VERSION = 'msprof-add-v1'
REQUIRED_COLUMNS = {'Device_id','Task ID','Stream ID','OP Type','Op Name','Task Start Time(us)','Task Duration(us)'}

class InvalidProfile(ValueError):
    def __init__(self, label, reason):
        super().__init__(reason)
        self.label = label


def number(value):
    try:
        val = Decimal(str(value).strip())
        if not val.is_finite() or val < 0:
            raise InvalidOperation()
        return val
    except (InvalidOperation, ValueError):
        raise InvalidProfile('UNIT_UNKNOWN', 'invalid or non-finite nonnegative timing value')


def events(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict): data = data.get('traceEvents')
    if not isinstance(data, list):
        raise InvalidProfile('TRACE_MISSING', 'unrecognized trace container')
    return data


def parse_add(op_summary, trace, tx_trace, sidecar, device_id, repeats, warmup):
    result = dict(parser_version=PARSER_VERSION, valid=False, quality=[], reasons=[],
                  expected_task_count=repeats, actual_task_count=0, raw_rows=[], invocations=[])
    try:
        if not Path(op_summary).is_file():
            raise InvalidProfile('INSUFFICIENT_DATA', 'op_summary missing')
        with Path(op_summary).open(newline='') as source:
            reader = csv.DictReader(source)
            if not REQUIRED_COLUMNS <= set(reader.fieldnames or []):
                raise InvalidProfile('UNIT_UNKNOWN', 'required named columns with explicit us units missing')
            rows = list(reader)
        result['raw_rows'] = rows
        tasks = [r for r in rows if r['Device_id'] == str(device_id) and r['OP Type'] == 'Add']
        result['actual_task_count'] = len(tasks)
        if len(tasks) != repeats or len(rows) != len(tasks):
            raise InvalidProfile('INSUFFICIENT_DATA', 'profile must contain exactly the declared Add task set')
        for path in (trace, tx_trace, sidecar):
            if not Path(path).is_file():
                raise InvalidProfile('TRACE_MISSING', 'trace or sidecar missing: ' + Path(path).name)
        phases = [json.loads(line) for line in Path(sidecar).read_text().splitlines() if line.strip()]
        warmups = [p for p in phases if p['phase'] == 'WARMUP']
        measures = [p for p in phases if p['phase'] == 'MEASURE']
        if (len(warmups) != warmup or any(p.get('profile_active') is not False for p in warmups)
            or [p.get('iteration') for p in warmups] != list(range(warmup))
            or len(measures) != repeats or [p.get('iteration') for p in measures] != list(range(repeats))
            or any(p.get('profile_active') is not True or p.get('rank') != 0 for p in measures)):
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'incomplete or inconsistent phase/iteration sidecar')
        phase_names = [p['phase'] for p in phases]
        if phase_names != ['WARMUP']*warmup + ['PROFILE_START'] + ['MEASURE']*repeats + ['PROFILE_STOP','RELEASED']:
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'profiling boundary/release ordering invalid')
        if phases[warmup].get('warmup_completed') != warmup or phases[-2].get('measured_iterations') != repeats:
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'profiling boundary count mismatch')
        tx = [e for e in events(tx_trace) if e.get('ph') == 'X' and e.get('name','').startswith('kernelx:measure:')]
        if len(tx) != repeats or len({e['name'] for e in tx}) != repeats:
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'range count or unique names mismatch')
        ranges = {e['name']: (number(e['ts']), number(e['ts'])+number(e['dur'])) for e in tx}
        ordered = sorted(ranges.values())
        if any(left[1] > right[0] for left,right in zip(ordered,ordered[1:])):
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'overlapping synchronous invocation ranges')
        trace_events = events(trace)
        hardware_pids = {e['pid'] for e in trace_events if e.get('name') == 'process_name' and e.get('args',{}).get('name') == 'Ascend Hardware'}
        device_pids = {e['pid'] for e in trace_events if e.get('name') == 'process_labels' and e.get('args',{}).get('labels') == 'NPU ' + str(device_id)}
        if len(hardware_pids & device_pids) != 1:
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'trace hardware device label missing or ambiguous')
        hardware_pid = next(iter(hardware_pids & device_pids))
        trace_tasks = [e for e in trace_events if e.get('ph') == 'X' and e.get('args',{}).get('Task Id') is not None]
        if any(e['pid'] != hardware_pid for e in trace_tasks):
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'task from unexpected hardware trace layer')
        # Match by stream+task ID and validate names/timestamps/durations; no row-order attribution.
        lookup = {}
        for event in trace_tasks:
            key = (str(event.get('tid')), str(event['args']['Task Id']))
            if key in lookup:
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'duplicate device trace task identity')
            lookup[key] = event
        if len(lookup) != repeats:
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'unexpected or missing device trace tasks')
        assigned = {m['range_name']: [] for m in measures}
        if set(assigned) != set(ranges):
            raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'sidecar range names differ from trace')
        for row in tasks:
            key = (row['Stream ID'],row['Task ID'])
            event = lookup.get(key)
            if not event or event['name'] != row['Op Name']:
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'CSV task not matched to trace')
            start, duration = number(row['Task Start Time(us)']), number(row['Task Duration(us)'])
            if abs(start-number(event['ts'])) > Decimal('.001') or abs(duration-number(event['dur'])) > Decimal('.001'):
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'CSV/trace timing mismatch')
            candidates = [name for name,(begin,end) in ranges.items() if begin <= start and start+duration <= end]
            if len(candidates) != 1:
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'task not uniquely contained in one synchronous range')
            assigned[candidates[0]].append((row,start,duration))
        for measure in measures:
            matches = assigned[measure['range_name']]
            if len(matches) != 1:
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'expected exactly one Add task per invocation')
            row,start,duration = matches[0]
            host_ns = measure['end_monotonic_ns']-measure['start_monotonic_ns']
            if host_ns < 0:
                raise InvalidProfile('ATTRIBUTION_UNKNOWN', 'non-monotonic host sidecar')
            result['invocations'].append(dict(iteration=measure['iteration'], rank=0,
                task_ids=[row['Stream ID']+':'+row['Task ID']], task_duration_us=[float(duration)],
                device_span_us=float(duration), host_elapsed_us=host_ns/1000,
                device_start_us=str(start), range_name=measure['range_name']))
        result.update(valid=True, quality=['VALID'])
    except InvalidProfile as exc:
        result['quality'] = [exc.label]; result['reasons'] = [str(exc)]; result['invocations'] = []
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        result['quality'] = ['ATTRIBUTION_UNKNOWN']; result['reasons'] = [str(exc)]; result['invocations'] = []
    return result
