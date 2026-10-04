import argparse
import json
from pathlib import Path

from .probe import probe
from .protocol import case_key, validate


def main():
    parser = argparse.ArgumentParser(prog="kernelx")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("probe", help="read-only environment snapshot")
    scan.add_argument("--server-id", required=True, help="registered server UUID, not logical device ID")
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--timeout", type=float, default=15)
    scan.add_argument("--include-identities", action="store_true", help="disable default redaction for private local evidence")
    inventory=commands.add_parser('library-inventory',help='CPU-only frozen catalogs and exact support tuple')
    inventory.add_argument('--environment',type=Path,required=True)
    inventory.add_argument('--device-uid',required=True)
    inventory.add_argument('--scope',choices=['core','full'],default='core')
    inventory.add_argument('--matrix',type=Path,required=True)
    inventory.add_argument('--output',type=Path,required=True)
    schedule=commands.add_parser('schedule-pilot',help='anchor/pair/rotation quota plan inside a fixed manual budget')
    schedule.add_argument('--tasks',type=Path,required=True)
    schedule.add_argument('--budget-seconds',type=float,required=True)
    schedule.add_argument('--cleanup-seconds',type=float,default=10)
    schedule.add_argument('--rotation-quota',type=float,default=.5)
    schedule.add_argument('--output',type=Path,required=True)
    report=commands.add_parser('efficiency-report')
    report.add_argument('--bundles',type=Path,nargs='+',required=True)
    report.add_argument('--inventory',type=Path,required=True)
    report.add_argument('--output',type=Path,required=True)
    report.add_argument('--control-dir',type=Path)
    report.add_argument('--center-dir',type=Path)
    check = commands.add_parser("validate")
    check.add_argument("entity", choices=["case", "plan", "session", "attempt", "artifact", "profile", "observation", "environment"])
    check.add_argument("path", type=Path)
    key = commands.add_parser("case-key")
    key.add_argument("path", type=Path)
    run = commands.add_parser("collect-cann-add", help="one CANN Add case in an explicitly authorized window")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--server-id", required=True)
    run.add_argument("--device", type=int, required=True)
    run.add_argument("--window-start", required=True)
    run.add_argument("--window-end", required=True)
    run.add_argument("--authorization-id", required=True)
    run.add_argument("--warmup", type=int, default=20)
    run.add_argument("--repeats", type=int, default=10)
    run.add_argument("--timeout", type=float, default=90)
    catalog_run = commands.add_parser("collect-library", help="manually budgeted compatible catalog pilot; no dependency installation")
    catalog_run.add_argument("--output", type=Path, required=True)
    catalog_run.add_argument("--server-id", required=True)
    catalog_run.add_argument("--device", type=int, required=True)
    catalog_run.add_argument("--window-start", required=True)
    catalog_run.add_argument("--window-end", required=True)
    catalog_run.add_argument("--authorization-id", required=True)
    catalog_run.add_argument("--warmup", type=int, default=20)
    catalog_run.add_argument("--repeats", type=int, default=10)
    catalog_run.add_argument("--timeout", type=float, default=90)
    catalog_run.add_argument("--library",choices=["sgl-kernel-npu","tile-kernels","deepgemm-ascend"],required=True)
    catalog_run.add_argument("--case-index",type=int,default=0)
    parse = commands.add_parser("parse-cann-add", help="offline attribution of exported Add data")
    parse.add_argument("--exports", type=Path, required=True)
    parse.add_argument("--sidecar", type=Path, required=True)
    parse.add_argument("--device", type=int, required=True)
    parse.add_argument("--warmup", type=int, default=20)
    parse.add_argument("--repeats", type=int, default=10)
    parse.add_argument("--output", type=Path, required=True)
    tick = commands.add_parser('agent-tick', help='one durable reservation tick; repeat with an external timer')
    tick.add_argument('--state',type=Path,required=True)
    tick.add_argument('--policy',type=Path,required=True)
    tick.add_argument('--plan',type=Path,required=True)
    destination=tick.add_mutually_exclusive_group()
    destination.add_argument('--center-dir',type=Path)
    destination.add_argument('--upload-url')
    tick.add_argument('--token-env')
    tick.add_argument('--resource-root',type=Path)
    tick.add_argument('--cache-root',type=Path)
    status=commands.add_parser('agent-status',help='persistent windows, attempts, uploads and audit ledger')
    status.add_argument('--state',type=Path,required=True)
    status.add_argument('--output',type=Path)
    ingest=commands.add_parser('ingest-bundle',help='durable idempotent central reference import')
    ingest.add_argument('--center-dir',type=Path,required=True)
    ingest.add_argument('--bundle',type=Path,required=True)
    entry=commands.add_parser('center-entry',help='join a persisted observation with case, hardware and library versions')
    entry.add_argument('--center-dir',type=Path,required=True)
    entry.add_argument('--observation-id')
    entry.add_argument('--output',type=Path)
    clear=commands.add_parser('agent-clear-device',help='manually clear quarantine after identity/idle checks')
    for name in ('state','policy','plan'): clear.add_argument('--'+name,type=Path,required=True)
    clear.add_argument('--device-uid',required=True)
    group=commands.add_parser('group-submit',help='explicit jointly authorized DeepEP group, never a shard')
    group.add_argument('--control-dir',type=Path,required=True)
    group.add_argument('--request',type=Path,required=True)
    group.add_argument('--policies',type=Path,nargs='+',required=True)
    group.add_argument('--ready-timeout',type=float,default=30)
    rank=commands.add_parser('group-rank-tick',help='one reserved rank with all-rank readiness and cancellation')
    for name in ('control-dir','state','policy','matrix'):rank.add_argument('--'+name,type=Path,required=True)
    rank.add_argument('--rank',type=int,required=True)
    rank.add_argument('--group-id',required=True)
    groupstatus=commands.add_parser('group-status')
    groupstatus.add_argument('--control-dir',type=Path,required=True)
    groupstatus.add_argument('--group-id',required=True)
    submit=commands.add_parser('fleet-submit',help='immutable all/selected-library submission with explicit server bindings')
    submit.add_argument('--control-dir',type=Path,required=True)
    submit.add_argument('--request',type=Path,required=True)
    fleetstatus=commands.add_parser('fleet-status',help='global/library/server progress including unsupported gaps')
    fleetstatus.add_argument('--control-dir',type=Path,required=True)
    fleetstatus.add_argument('--run-id',required=True)
    retry=commands.add_parser('fleet-retry',help='explicit new dispatch after failure; preserves previous attempts')
    retry.add_argument('--control-dir',type=Path,required=True)
    retry.add_argument('--dispatch-id',required=True)
    retry.add_argument('--retry-id',required=True)
    worker=commands.add_parser('fleet-agent-tick',help='actively pull bound work through the filesystem reference API')
    for name in ('control-dir','state','policy','center-dir'): worker.add_argument('--'+name,type=Path,required=True)
    fleetclear=commands.add_parser('fleet-clear-device',help='manually clear the shared server quarantine after identity/idle checks')
    for name in ('state','policy'): fleetclear.add_argument('--'+name,type=Path,required=True)
    fleetclear.add_argument('--device-uid',required=True)
    release=commands.add_parser('release-build',help='sign a clean tracked source tree for one exact host compatibility group')
    for name in ('source','repository','environment','key'): release.add_argument('--'+name,type=Path,required=True)
    release.add_argument('--device-uid',required=True)
    release.add_argument('--sequence',type=int,required=True)
    commands.add_parser('release-self-test',help='CPU-only dependency/schema import test; no NPU initialization')
    for command in ('bootstrap-tick','bootstrap-status','bootstrap-clear-device'):
        boot=commands.add_parser(command)
        boot.add_argument('--config',type=Path,required=True)
        if command=='bootstrap-clear-device': boot.add_argument('--device-uid',required=True)
    args = parser.parse_args()
    if args.command=='schedule-pilot':
        from .efficiency import schedule
        data=schedule(json.loads(args.tasks.read_text()),args.budget_seconds,args.cleanup_seconds,args.rotation_quota)
        args.output.write_text(json.dumps(data,indent=2)+'\n')
        print(json.dumps(dict(output=str(args.output),tasks=len(data['tasks']),used_seconds=data['used_seconds'])))
        return
    if args.command=='library-inventory':
        from .libraries import Registry,SupportMatrix
        matrix=SupportMatrix(args.matrix)
        try:data=Registry().inventory(json.loads(args.environment.read_text()),args.device_uid,args.scope,matrix)
        finally:matrix.close()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(data,indent=2)+'\n')
        print(json.dumps(dict(output=str(args.output),statuses={r['library']:r['status'] for r in data})))
        return
    if args.command=='efficiency-report':
        from .efficiency import generate
        print(json.dumps(generate(args.bundles,json.loads(args.inventory.read_text()),args.output,args.control_dir,args.center_dir)))
        return
    if args.command in ('release-build','release-self-test','bootstrap-tick','bootstrap-status','bootstrap-clear-device'):
        if args.command=='release-self-test':
            from .release import self_test
            print(json.dumps(self_test()))
        elif args.command=='release-build':
            import subprocess
            from .release import build_release, compatibility
            commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.source,text=True).strip()
            changed=subprocess.check_output(['git','status','--porcelain','--','kernelx'],cwd=args.source,text=True)
            if changed: raise ValueError('release requires clean tracked kernelx source at git HEAD')
            group=compatibility(json.loads(args.environment.read_text()),args.device_uid)
            path=build_release(args.source,args.repository,commit,group,args.key,args.sequence)
            print(json.dumps(dict(release_id=path.name,path=str(path))))
        else:
            from .bootstrap import Bootstrap
            bootstrap=Bootstrap(json.loads(args.config.read_text()))
            try:
                if args.command=='bootstrap-clear-device':
                    bootstrap.clear_device(args.device_uid); print(json.dumps(dict(device_uid=args.device_uid,state='READY')))
                else:
                    result=bootstrap.tick() if args.command=='bootstrap-tick' else bootstrap.status()
                    print(json.dumps(result))
                    if args.command=='bootstrap-tick' and result['exit_code']: raise SystemExit(result['exit_code'])
            finally: bootstrap.close()
        return
    if args.command.startswith('group-'):
        from .resource_group import ResourceGroup,GroupRankWorker
        if args.command=='group-rank-tick':
            data=GroupRankWorker(args.control_dir,args.state,args.rank,args.policy,matrix=args.matrix).run(args.group_id)
        else:
            control=ResourceGroup(args.control_dir)
            try:
                if args.command=='group-submit':data=dict(group_id=control.submit(json.loads(args.request.read_text()),[json.loads(p.read_text()) for p in args.policies],args.ready_timeout))
                else:data=control.poll(args.group_id);data['metrics']=control.metrics(args.group_id)
            finally:control.close()
        print(json.dumps(data))
        return
    if args.command=='fleet-clear-device':
        from .agent.fleet import FleetWorker
        FleetWorker(args.state,args.policy,None).clear_device(args.device_uid)
        print(json.dumps(dict(device_uid=args.device_uid,state='READY')))
        return
    if args.command.startswith('fleet-'):
        from .agent.fleet import Fleet, FleetWorker
        from .agent.storage import Center
        fleet=Fleet(args.control_dir); center=None
        try:
            if args.command=='fleet-submit': data=dict(run_id=fleet.submit(json.loads(args.request.read_text())))
            elif args.command=='fleet-status': data=fleet.status(args.run_id)
            elif args.command=='fleet-retry': data=dict(dispatch_id=fleet.retry(args.dispatch_id,args.retry_id))
            else:
                center=Center(args.center_dir)
                data=FleetWorker(args.state,args.policy,fleet,center.import_bundle).tick()
            print(json.dumps(data))
        finally:
            fleet.close()
            if center: center.close()
        return
    if args.command in ('agent-tick','agent-status','agent-clear-device','ingest-bundle','center-entry'):
        from .agent import Agent, Center
        from .agent.transport import HTTPTransport
        if args.command in ('ingest-bundle','center-entry'):
            center=Center(args.center_dir)
            try:
                data=center.import_bundle(args.bundle) if args.command=='ingest-bundle' else center.entry(args.observation_id)
                if getattr(args,'output',None): args.output.write_text(json.dumps(data,indent=2)+'\n')
                else: print(json.dumps(data))
            finally: center.close()
            return
        agent=Agent(args.state,getattr(args,'policy',''),getattr(args,'plan',''),resource_root=getattr(args,'resource_root',None),cache_root=getattr(args,'cache_root',None))
        center=None
        try:
            if args.command=='agent-status':
                data=agent.status()
                if args.output: args.output.write_text(json.dumps(data,indent=2)+'\n')
                else: print(json.dumps(data))
            elif args.command=='agent-clear-device':
                agent.clear_device(args.device_uid); print(json.dumps(dict(device_uid=args.device_uid,state='READY')))
            else:
                transport=None
                if args.center_dir:
                    center=Center(args.center_dir); transport=center.import_bundle
                elif args.upload_url: transport=HTTPTransport(args.upload_url,args.token_env)
                print(json.dumps(agent.tick(transport)))
        finally:
            agent.close()
            if center: center.close()
    elif args.command == "parse-cann-add":
        from .profile_parser import parse_add
        def select(pattern):
            paths = list(args.exports.glob(pattern))
            return paths[0] if len(paths) == 1 else args.exports / "MISSING"
        result = parse_add(select('op_summary_*.csv'), select('msprof_[0-9]*.json'),
                           select('msprof_tx_*.json'), args.sidecar, args.device,
                           args.repeats, args.warmup)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(dict(valid=result['valid'], quality=result['quality'])))
        raise SystemExit(0 if result['valid'] else 1)
    elif args.command in ("collect-cann-add","collect-library"):
        from .runner import collect
        result = collect(args.output, server_id=args.server_id, device=args.device,
                         window_start=args.window_start, window_end=args.window_end,
                         authorization_id=args.authorization_id, warmup=args.warmup,
                         repeats=args.repeats, timeout=args.timeout,
                         **(dict(adapter_library=args.library,case_index=args.case_index) if args.command=="collect-library" else {}))
        print(json.dumps(result))
        raise SystemExit(0 if result['valid'] else 1)
    elif args.command == "probe":
        if args.timeout <= 0:
            parser.error("timeout must be positive")
        snapshot = probe(args.server_id, not args.include_identities, args.timeout)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(dict(output=str(args.output), devices=len(snapshot["devices"]),
                              inventory_status=snapshot["device_inventory"]["status"])))
    else:
        value = json.loads(args.path.read_text())
        if args.command == "case-key":
            print(case_key(value))
        else:
            validate(args.entity, value)
            print("valid " + args.entity)


if __name__ == "__main__":
    main()
