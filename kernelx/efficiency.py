"""Auditable pilot reports; no kernel-microsecond based resource estimates."""
import csv,hashlib,json,math,statistics,random,itertools
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from .protocol import digest


def union_seconds(intervals):
    if any(a is None or b is None for a,b in intervals):return None
    total=0.;end=None
    for a,b in sorted(intervals):
        if b<a:raise ValueError('negative resource interval')
        total+=max(0,b-max(a,end if end is not None else a));end=max(b,end if end is not None else b)
    return total


def quantile(values,p):
    if not values:return None
    values=sorted(values);position=(len(values)-1)*p;lo=int(position);hi=math.ceil(position)
    return values[lo]+(values[hi]-values[lo])*(position-lo)


def summary(values):
    return dict(n=len(values),p50=quantile(values,.5),p95=quantile(values,.95),min=min(values) if values else None,max=max(values) if values else None,cv=statistics.stdev(values)/statistics.mean(values) if len(values)>1 and statistics.mean(values) else None)


def schedule(tasks,budget_seconds,cleanup_seconds=10,rotation_quota=.5):
    """Anchors first, pair members adjacent; never extend the supplied window."""
    if not all(math.isfinite(v) for v in (budget_seconds,cleanup_seconds,rotation_quota)) or cleanup_seconds<3 or budget_seconds<=cleanup_seconds or not 0<=rotation_quota<=1:raise ValueError('invalid finite window')
    ordered=sorted(tasks,key=lambda t:(not t.get('anchor',False),t.get('pair_id',''),t['case_key']))
    selected=[];used=0.;rotation=0.;limit=budget_seconds-cleanup_seconds
    groups=defaultdict(list)
    for task in ordered:groups[task.get('pair_id') or task['case_key']].append(task)
    for group in groups.values():
        costs=[t.get('upper_seconds') for t in group]
        if any(c is None or not isinstance(c,(int,float)) or not math.isfinite(c) or c<=0 for c in costs):continue
        cost=sum(costs);rot=not any(t.get('anchor') for t in group)
        if used+cost<=limit and (not rot or rotation+cost<=limit*rotation_quota):
            selected.extend(group);used+=cost;rotation+=cost if rot else 0
    return dict(tasks=selected,used_seconds=used,remaining_seconds=budget_seconds-used,cleanup_seconds=cleanup_seconds,automatic_reservation_change=False)


def generate(bundles,inventory,output,control_dir=None,center_dir=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    fleet_runs=[];fleet_attempts={};import_receipts={}
    if control_dir:
        from .agent.fleet import Fleet
        fleet=Fleet(control_dir)
        try:
            fleet_runs=[fleet.status(row['run_id']) for row in fleet.db.execute('SELECT run_id FROM runs ORDER BY rowid')]
            for run in fleet_runs:
                for dispatch in run['dispatches']:
                    for attempt in json.loads(dispatch.get('report') or '{}').get('attempts',[]):fleet_attempts[attempt['output']]=dict(attempt,server_id=dispatch['server_id'],global_run_id=run['run_id'],dispatch_id=dispatch['dispatch_id'],dispatch_mode=run['plan']['mode'],reservation_from=run['plan']['valid_from'],reservation_until=run['plan']['valid_until'],device_uid=next(s['device_uid'] for s in run['plan']['servers'] if s['server_id']==dispatch['server_id']))
        finally:fleet.close()
    if center_dir:
        from .agent.storage import Center
        center=Center(center_dir)
        try:import_receipts={row['bundle_id']:json.loads(row['receipt']) for row in center.db.execute('SELECT * FROM imports')}
        finally:center.close()
    known_devices={}
    for source in bundles:
        path=Path(source)/'environment.json'
        if path.is_file():
            e=json.loads(path.read_text())
            for d in e.get('devices',[]):known_devices[(e['server_id'],d['device_uid'])]=d
    rows=[];measurements=defaultdict(list);inputs=[];server_intervals=defaultdict(list);card_intervals=defaultdict(list);chip_intervals=defaultdict(list);reserved_servers=defaultdict(list);reserved_cards=defaultdict(list);reserved_chips=defaultdict(list);missing=[];warm=[]
    def read(root,name,default=None):
        path=root/name
        return json.loads(path.read_text()) if path.is_file() else default
    def wall(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    for bundle in bundles:
        root=Path(bundle);result=read(root,'result.json',read(root,'failure.json',{}));env=read(root,'environment.json',{});manifest=read(root,'manifest.json',{});session=read(root,'session.json',{});cost=read(root,'cost.json',{});auth=read(root,'authorization.json',{});observations=read(root,'observations.json',[])
        attempt_record=fleet_attempts.get(str(root.resolve()))
        if not cost and attempt_record:
            cost=dict(wall_start=attempt_record['started'],wall_end=attempt_record['ended'],total_wall_seconds=attempt_record['cost'],phases=None,source='durable Agent attempt ledger',cache_state='FAILED_CACHE_CHECK')
        snapshot={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.json'))};inputs.append(dict(path=str(root.resolve()),sha256=snapshot))
        device=next((d for d in env.get('devices',[]) if d.get('device_uid')==auth.get('device_uid')),{});server=env.get('server_id') or (attempt_record or {}).get('server_id');uid=device.get('device_uid') or (attempt_record or {}).get('device_uid');card=device.get('physical_card_uid') or device.get('card_uid') or known_devices.get((server,uid),{}).get('card_uid');bin_value=device.get('hardware_bin',{}).get('value')
        software=env.get('software',{});fingerprints=env.get('extensions',{}).get('fingerprints',{})
        # Exclude capture IDs/timestamps; retain all actual versions/fingerprints,
        # measured providers, BIN, repeat protocol and frozen preset identity.
        def canonical(value):
            if isinstance(value,dict):return {k:canonical(v) for k,v in value.items() if k not in ('source','evidence_id','used_by_case')}
            if isinstance(value,list):return [canonical(v) for v in value]
            return value
        libraries=canonical(software.get('operator_libraries',[]))
        cohort=dict(case_key=manifest.get('case_key'),hardware_bin=bin_value,libraries=libraries,packages=canonical(software.get('packages')),fingerprints={k:v.get('sha256') for k,v in fingerprints.items()},preset=read(root,'preset.json'),repeat_policy=session.get('extensions',{}).get('repeat_policy'),provider=env.get('extensions',{}).get('runtime_host_providers'))
        identity=digest(cohort);valid=result.get('valid') is True
        if valid:
            from .agent.storage import verify_bundle
            verify_bundle(root)
        context=read(root,'agent-context.json',{})
        global_context=context.get('fleet',{})
        if not global_context and attempt_record:global_context={k:attempt_record[k] for k in ('global_run_id','dispatch_id')}
        if attempt_record and not global_context.get('mode'):global_context['mode']=attempt_record['dispatch_mode']
        if not auth and attempt_record:auth=dict(window_start=attempt_record['reservation_from'],window_end=attempt_record['reservation_until'])
        sealed=read(root,'sealed.json',{});receipt=import_receipts.get(sealed.get('bundle_id'),{})
        latency=wall(receipt['imported_at'])-cost['wall_end'] if receipt.get('imported_at') and cost.get('wall_end') is not None else None
        predicted=next((r['plan']['pilot_upper_seconds'] for r in fleet_runs if r['run_id']==global_context.get('global_run_id')),None)
        row=dict(predicted_upper_seconds=predicted,global_run_id=global_context.get('global_run_id'),dispatch_id=global_context.get('dispatch_id'),dispatch_mode=global_context.get('mode'),imported=bool(receipt),data_available_latency_seconds=latency,bundle=str(root.resolve()),library=manifest.get('library_id'),case_key=manifest.get('case_key'),cohort=identity,window=session.get('window_id') or (attempt_record or {}).get('window_id'),date=datetime.fromtimestamp(cost.get('wall_start',wall(session['started_at']) if session.get('started_at') else 0),timezone.utc).strftime('%Y-%m-%d'),server_id=server,device_uid=uid,card_uid=card,valid=valid,failure_reason=result.get('reason'),quality=read(root,'profile.json',{}).get('quality',result.get('quality')),cost_seconds=cost.get('total_wall_seconds'),cache_state=cost.get('cache_state'),cache_key=cost.get('cache_key'),observations=len(observations),cpu_seconds=cost.get('cpu_seconds'),io_bytes=cost.get('io_bytes'),disk_peak_lower_bound=cost.get('disk_peak_bytes_lower_bound'),phases=cost.get('phases'),unallocated_seconds=cost.get('unallocated_seconds'),cohort_tuple=cohort)
        rows.append(row)
        if cost.get('wall_start') is not None:server_intervals[server].append((cost['wall_start'],cost.get('wall_end')))
        else:
            missing.append('server interval:'+str(root));server_intervals[server].append((None,None))
        ledger=session.get('resource_ledger',[])
        for interval in ledger:
            seconds=(interval['end_monotonic_ns']-interval['start_monotonic_ns'])/1e9 if interval.get('end_monotonic_ns') is not None else None
            # Anchor monotonic duration to this attempt's wall start. Absolute
            # monotonic clocks are never compared between hosts.
            attempt=read(root,'attempt.json',{});a=wall(attempt['started_at']) if attempt.get('started_at') else None;b=a+seconds if a is not None and seconds is not None else None
            chip_intervals[(server,uid)].append((a,b))
            if card:card_intervals[(server,card)].append((a,b))
            else:missing.append('physical card identity:'+str(root))
        if auth.get('window_start'):
            interval=(wall(auth['window_start']),wall(auth['window_end']));reserved_servers[server].append(interval);reserved_chips[(server,uid)].append(interval)
            if card:reserved_cards[(server,card)].append(interval)
        if valid:
            for obs in observations:
                if obs.get('metric',{}).get('name')=='device_span_us':
                    for value in obs['raw_samples']:measurements[identity].append(dict(value=value,window=row['window'],date=row['date'],server=server,device=uid,card=card,pair_id=global_context.get('global_run_id') if global_context.get('mode')=='paired' else None))
        sidecar=root/'sidecar.jsonl'
        if sidecar.is_file():
            values=[(r['end_monotonic_ns']-r['start_monotonic_ns'])/1000 for r in map(json.loads,sidecar.read_text().splitlines()) if r.get('phase')=='WARMUP']
            if values:
                candidates=[]
                for count in (10,15,20):
                    if count>len(values):continue
                    previous=statistics.median(values[count-10:count-5]);tail=summary(values[count-5:count]);shift=abs(tail['p50']-previous)/previous if previous else None
                    candidates.append(dict(count=count,tail=tail,median_shift=shift,stable_at_2pct=shift is not None and shift<=.02 and tail['cv'] is not None and tail['cv']<=.02))
                warm.append(dict(candidate_counts=candidates,window=row['window'],n=len(values),last5=summary(values[-5:]),last10=summary(values[-10:]),proposed_warmup=None,reason='host elapsed warmup pilot; keep 20 until cross-window stability and device timing agree'))
    def hours(groups):
        values=[union_seconds(v) for v in groups.values()]
        return sum(values)/3600 if values and all(v is not None for v in values) else None
    stable=[]
    for cohort,values in measurements.items():
        by_window=defaultdict(list)
        for v in values:by_window[v['window']].append(v['value'])
        per_date=defaultdict(list);pairs=defaultdict(lambda:defaultdict(list))
        for value in values:
            per_date[(value['device'],value['date'])].append(value['value'])
            if value['pair_id']:pairs[value['pair_id']][(value['server'],value['device'],value['card'])].append(value['value'])
        ratios=defaultdict(list)
        for pair_id,targets in pairs.items():
            for left,right in itertools.combinations(sorted(targets,key=str),2):
                a=statistics.median(targets[left]);b=statistics.median(targets[right])
                if a>0 and b>0:ratios[(left,right)].append(dict(pair_id=pair_id,ratio=b/a))
        paired=[]
        for targets,samples in ratios.items():
            logs=[math.log(v['ratio']) for v in samples];interval=None
            if len(samples)>=3:
                rng=random.Random(0);bootstrap=[math.exp(statistics.mean(rng.choices(logs,k=len(logs)))) for _ in range(1000)];interval=[quantile(bootstrap,.025),quantile(bootstrap,.975)]
            paired.append(dict(targets=targets,independent_pair_windows=len(samples),samples=samples,geometric_ratio=math.exp(statistics.mean(logs)),interval=interval,confidence='LOW_PILOT' if interval else 'INSUFFICIENT_INDEPENDENT_PAIRS'))
        cross_server=any(p['targets'][0][0]!=p['targets'][1][0] and p['independent_pair_windows']>=3 for p in paired)
        cross_card=any(p['targets'][0][0]==p['targets'][1][0] and p['targets'][0][2] and p['targets'][1][2] and p['targets'][0][2]!=p['targets'][1][2] and p['independent_pair_windows']>=3 for p in paired)
        stable.append(dict(cohort=cohort,windows={k:summary(v) for k,v in by_window.items()},independent_windows=len(by_window),independent_dates=len({v['date'] for v in values}),devices=len({v['device'] for v in values}),servers=len({v['server'] for v in values}),cross_day='AVAILABLE' if any(sum(d==device for d,_ in per_date)>=3 for device,_ in per_date) else 'PENDING',per_chip_day={str(k):summary(v) for k,v in per_date.items()},cross_card='AVAILABLE' if cross_card else 'PENDING',cross_server='AVAILABLE' if cross_server else 'PENDING',confidence_interval=None,paired_ratios=paired))
    by_library=defaultdict(list)
    for row in rows:by_library[row['library']].append(row)
    coverage=[];capacities=[]
    for item in inventory:
        library=item['library'];runs=by_library[library];passed=[r for r in runs if r['valid']];cases=item.get('manifest',{}).get('cases',[]) if item.get('manifest') else [];denominator=len(cases) or (1 if library=='cann-opp' else None)
        costs=[r['cost_seconds'] for r in passed if r['cost_seconds'] is not None];hot=[r['cost_seconds'] for r in passed if r['cache_state']=='HIT' and r['cost_seconds'] is not None];effective=len(passed)/len(runs) if runs else None
        comparable={r['cohort'] for r in passed}
        cohort_costs={key:summary([r['cost_seconds'] for r in passed if r['cohort']==key and r['cost_seconds'] is not None]) for key in sorted(comparable)}
        complete_cohorts=[key for key in comparable if denominator is not None and len({r['case_key'] for r in passed if r['cohort']==key})==denominator]
        frozen_complete=len(comparable)==1 and len(complete_cohorts)==1
        coverage.append(dict(complete_cohorts=complete_cohorts,cohort_costs=cohort_costs,cohort_count=len(comparable),failure_cost_seconds=sum(r['cost_seconds'] for r in runs if not r['valid'] and r['cost_seconds'] is not None),library=library,status='MEASURED_SEED' if passed else item['status'],planned_cases=denominator,applicable_cases=0 if item['status']=='UNSUPPORTED' else denominator,unsupported_cases=denominator if item['status']=='UNSUPPORTED' else 0,attempts=len(runs),successes=len(passed),effective_rate=effective,unique_valid_cases=len({r['case_key'] for r in passed}),frozen_scope_complete=frozen_complete,core_full_scope=(item.get('manifest',{}).get('scope_definition') or item.get('scope_definition')) if item.get('manifest') else None,cold=summary([r['cost_seconds'] for r in passed if r['cache_state']=='MISS' and r['cost_seconds'] is not None] if len(comparable)==1 else []),hot=summary(hot if len(comparable)==1 else []),cost=summary(costs if len(comparable)==1 else []),reason=item.get('reason'),confidence='LOW_PILOT' if costs else 'NO_COST_DATA'))
        for minutes in (60,90,120):
            upper=max(costs)/effective if costs and effective and len(comparable)==1 else None
            capacities.append(dict(library=library,minutes=minutes,conservative_effective_repeat_capacity=math.floor((minutes*60-10)/upper) if upper else None,new_unique_case_capacity=denominator if frozen_complete and upper and upper<=minutes*60-10 else None,rotation_days=1 if frozen_complete and upper and upper<=minutes*60-10 else None,target_hours=(max(costs)/effective)*denominator/3600 if frozen_complete and effective and len(comparable)==1 else None,uncertainty='pilot maximum / observed effective rate; not a statistical bound' if upper else 'unavailable: missing library cost layers',automatic_reservation_change=False))
    progress=dict(global_runs=[{k:r[k] for k in ('run_id','state','counts','libraries','servers')} for r in fleet_runs],dispatch_modes=sorted({r['plan']['mode'] for r in fleet_runs}),attempts=len(rows),valid_attempts=sum(r['valid'] for r in rows),ingested_bundles=sum(r['imported'] for r in rows),backlog=sum(r['counts'].get('PENDING_UPLOAD',0) for r in fleet_runs),data_available_latency_seconds=summary([r['data_available_latency_seconds'] for r in rows if r['data_available_latency_seconds'] is not None]))
    report=dict(schema_version=1,model_inputs=dict(inventory_sha256=digest(inventory),fleet_requests=[r['plan'] for r in fleet_runs],center_receipts=import_receipts,bootstrap_seed=0,pilot_cleanup_seconds=10,confidence='LOW_PILOT'),progress=progress,inputs=inputs,inventory=inventory,attempts=rows,coverage=coverage,stability=stable,warmup=warm,capacity=capacities,resources=dict(actual_server_hours=hours(server_intervals),actual_card_hours=hours(card_intervals),actual_chip_hours=hours(chip_intervals),global_wall_seconds=union_seconds([i for g in server_intervals.values() for i in g]) if server_intervals else None,reserved_server_hours=hours(reserved_servers),reserved_card_hours=hours(reserved_cards),reserved_chip_hours=hours(reserved_chips),cpu_seconds=sum(r['cpu_seconds'] for r in rows) if rows and all(r['cpu_seconds'] is not None for r in rows) else None,measured_collector_cpu_lower_bound=sum(r['cpu_seconds'] for r in rows if r['cpu_seconds'] is not None),io_bytes=None,import_latency_seconds=None),missing=sorted(set(missing+['cross-day/card/server paired coverage','unmeasured library cost strata','upload CPU/I/O'])),prediction_error=[dict(bundle=r['bundle'],predicted_upper_seconds=r['predicted_upper_seconds'],actual_seconds=r['cost_seconds'],error_seconds=r['cost_seconds']-r['predicted_upper_seconds'],source='frozen fleet pilot_upper_seconds') for r in rows if r['predicted_upper_seconds'] is not None and r['cost_seconds'] is not None],proposal='Keep warmup=20. Reuse existing authorized windows for anchor-first collection; request no automatic extra resources.')
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n');(output/'inputs.json').write_text(json.dumps(inputs,indent=2)+'\n')
    for name,records in [('attempts',rows),('coverage',coverage),('capacity',capacities)]:
        with (output/(name+'.csv')).open('w') as stream:
            fields=list(records[0]) if records else ['library'];writer=csv.DictWriter(stream,fields);writer.writeheader();writer.writerows({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in records)
    lines=['# 910B1 采集效率试采报告','','这是冻结目录的试采结果。full 仅表示该版本选定入口的三个 shape，不代表上游全部 API。','', '| 库 | 计划 case | 成功/attempt | 有效不同 case | 热缓存 p50 秒 | 状态 |','| --- | ---: | ---: | ---: | ---: | --- |']
    for r in coverage:lines.append('| %s | %s | %s/%s | %s | %s | %s |'%(r['library'],r['planned_cases'],r['successes'],r['attempts'],r['unique_valid_cases'],r['hot']['p50'],r['status']))
    lines+=['','资源账本：`'+json.dumps(report['resources'],ensure_ascii=False)+'`','','三窗口不能替代三天。跨日、跨卡、跨机稳定性和未运行库的容量保留为待测；未知成本保留 null。预热维持 20 次。','','60/90/120 分钟容量与输入哈希见 capacity.csv、report.json 和 inputs.json。预测仅适用于已测的冻结 seed，不能外推到未测算子族。']
    markdown='\n'.join(lines)+'\n';(output/'report.md').write_text(markdown)
    import html
    (output/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>KernelX pilot</title><pre>'+html.escape(markdown)+'</pre>')
    return dict(output=str(output.resolve()),attempts=len(rows),valid=sum(r['valid'] for r in rows),independent_windows=len({r['window'] for r in rows}),independent_dates=len({r['date'] for r in rows}))
