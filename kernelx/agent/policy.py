"""Human policy and immutable plan; UTC storage, timezone-local scheduling."""
import math
import re
import uuid
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

from ..protocol import digest


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None: raise ValueError('timestamps require timezone')
    return result.timestamp()


def positive(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
        raise ValueError('finite positive number required')
    return value


def read_policy(value):
    required={'schema_version','server_id','reservation_id','schedule_revision','enabled','valid_from','valid_until','timezone','schedules','skip_dates','allowed_devices','max_workers','cleanup_reserve_seconds','task_timeout_seconds','spool_max_bytes','spool_high_watermark'}
    if set(value)!=required or value['schema_version']!=1: raise ValueError('unsupported policy fields/version')
    uuid.UUID(value['server_id'])
    if not value['reservation_id'] or not isinstance(value['enabled'],bool): raise ValueError('manual reservation and enabled required')
    if type(value['schedule_revision']) is not int or value['schedule_revision']<1: raise ValueError('invalid reservation revision')
    if type(value['max_workers']) is not int or value['max_workers']!=1: raise ValueError('first Agent supports max_workers=1 only')
    ZoneInfo(value['timezone'])
    if timestamp(value['valid_until'])<=timestamp(value['valid_from']): raise ValueError('empty validity')
    if positive(value['cleanup_reserve_seconds'])<3: raise ValueError('cleanup reserve must be at least 3 seconds')
    positive(value['task_timeout_seconds']); positive(value['spool_max_bytes'])
    if not 0<positive(value['spool_high_watermark'])<1: raise ValueError('invalid spool watermark')
    for date in value['skip_dates']: datetime.strptime(date,'%Y-%m-%d')
    devices=value['allowed_devices']
    if not devices or len({d['device_uid'] for d in devices})!=len(devices) or len({d['logical_id'] for d in devices})!=len(devices): raise ValueError('devices must be unique')
    for device in devices:
        if set(device)!={'device_uid','logical_id'} or not device['device_uid'] or type(device['logical_id']) is not int or device['logical_id']<0: raise ValueError('invalid device')
    ids=set()
    for schedule in value['schedules']:
        if set(schedule)!={'id','weekdays','start','end'} or not schedule['id'] or schedule['id'] in ids: raise ValueError('unique schedule ID required')
        ids.add(schedule['id'])
        if not schedule['weekdays'] or any(type(day) is not int or not 1<=day<=7 for day in schedule['weekdays']): raise ValueError('invalid weekday')
        for key in ('start','end'):
            if not re.fullmatch(r'\d\d:\d\d',schedule[key]): raise ValueError('HH:MM required')
            time.fromisoformat(schedule[key])
        if schedule['start']==schedule['end']: raise ValueError('zero/full-day windows unsupported')
    if not ids: raise ValueError('schedule required')
    return value


def local_time(day,clock,zone):
    naive=datetime.combine(day,time.fromisoformat(clock))
    first=naive.replace(tzinfo=zone,fold=0); second=naive.replace(tzinfo=zone,fold=1)
    if first.utcoffset()!=second.utcoffset() or datetime.fromtimestamp(first.timestamp(),zone).replace(tzinfo=None)!=naive:
        raise ValueError('ambiguous or nonexistent DST window boundary')
    return first.timestamp()


def windows(policy,clock):
    """Current local day and preceding overnight windows; never catch up old days."""
    zone=ZoneInfo(policy['timezone']); today=datetime.fromtimestamp(clock,zone).date()
    result=[]
    for day in (today-timedelta(days=1),today):
        for schedule in policy['schedules']:
            if day.isoweekday() not in schedule['weekdays']: continue
            end_day=day+timedelta(days=1) if schedule['end']<schedule['start'] else day
            if day.isoformat() in policy['skip_dates'] or end_day.isoformat() in policy['skip_dates']: continue
            start=max(local_time(day,schedule['start'],zone),timestamp(policy['valid_from']))
            end=min(local_time(end_day,schedule['end'],zone),timestamp(policy['valid_until']))
            if end<=start: continue
            identity=dict(server=policy['server_id'],reservation=policy['reservation_id'],date=day.isoformat(),schedule=schedule['id'])
            result.append(dict(window_id=digest(identity),date=day.isoformat(),start=start,end=end,policy_sha256=digest(policy),schedule_revision=policy['schedule_revision']))
    result.sort(key=lambda w:w['start'])
    for first,second in zip(result,result[1:]):
        if first['end']>second['start']: raise ValueError('overlapping reservations')
    return result


def read_plan(value,policy,manifest):
    if set(value)!={'schema_version','plan_id','valid_from','valid_until','manifest_sha256','preset','tasks'} or value['schema_version']!=1: raise ValueError('unsupported plan fields/version')
    if not value['plan_id'] or value['preset']!='latency-v1' or value['manifest_sha256']!=digest(manifest): raise ValueError('plan manifest/preset mismatch')
    if timestamp(value['valid_until'])<=timestamp(value['valid_from']): raise ValueError('empty plan validity')
    allowed={d['device_uid'] for d in policy['allowed_devices']}; ids=set()
    for task in value['tasks']:
        if set(task)!=({'task_id','adapter','device_uid','warmup','repeats','pilot_upper_seconds','estimated_output_bytes'} | (set() if task['adapter']=='cann-add' else {'case_index'})): raise ValueError('invalid task fields')
        if not task['task_id'] or task['task_id'] in ids or task['adapter'] not in ('cann-add','sgl-kernel-npu','tile-kernels','deepgemm-ascend') or task['device_uid'] not in allowed: raise ValueError('invalid/unauthorized task')
        if task['adapter']!='cann-add':
            from ..libraries import FrozenPerformanceAdapter
            if type(task['case_index']) is not int or task['case_index']<0 or digest(FrozenPerformanceAdapter(task['adapter'],task['case_index']).manifest)!=value['manifest_sha256']:raise ValueError('case binding mismatch')
        if task['adapter']!=value['tasks'][0]['adapter']:raise ValueError('one frozen adapter/case binding per plan')
        ids.add(task['task_id'])
        for name in ('warmup','repeats'):
            if type(task[name]) is not int or not 1<=task[name]<=1000: raise ValueError('invalid repeat policy')
        positive(task['pilot_upper_seconds']); positive(task['estimated_output_bytes'])
    if not ids: raise ValueError('empty plan')
    return value
