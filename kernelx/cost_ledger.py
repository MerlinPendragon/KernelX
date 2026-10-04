"""Nonoverlapping monotonic phases; unknown measurements remain null."""
import resource,time
from datetime import datetime,timezone


def cpu():
    a=resource.getrusage(resource.RUSAGE_SELF);b=resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime+a.ru_stime+b.ru_utime+b.ru_stime


class CostLedger:
    def __init__(self):
        self.start=time.monotonic_ns();self.cursor=self.start;self.wall=time.time();self.cpu_start=cpu();self.phases=[]
    def mark(self,name,device_held=False,end=None):
        end=time.monotonic_ns() if end is None else end
        if end<self.cursor:raise ValueError('phase clocks must be monotonic')
        self.phases.append(dict(name=name,start_monotonic_ns=self.cursor,end_monotonic_ns=end,seconds=(end-self.cursor)/1e9,device_held=device_held));self.cursor=end
    def native(self,sidecar,begin,end):
        self.mark('prepare_and_device_preflight',end=begin)
        warm=[r for r in sidecar if r.get('phase')=='WARMUP'];measure=[r for r in sidecar if r.get('phase')=='MEASURE']
        boundaries=[('device_initialization',warm[0]['start_monotonic_ns']),('warmup',warm[-1]['end_monotonic_ns']),('profiler_setup',measure[0]['start_monotonic_ns']),('profiled_invocations',measure[-1]['end_monotonic_ns']),('cleanup_and_release',end)] if warm and measure else [('native_execution_and_cleanup',end)]
        if not all(begin<=n<=end for _,n in boundaries) or any(a[1]>b[1] for a,b in zip(boundaries,boundaries[1:])):raise ValueError('native sidecar phase clocks outside owned interval')
        for name,n in boundaries:self.mark(name,True,n)
    def snapshot(self,cache_state,cache_key,bytes_on_disk):
        end=time.monotonic_ns()
        return dict(schema_version=1,wall_start=self.wall,wall_end=self.wall+(end-self.start)/1e9,total_wall_seconds=(end-self.start)/1e9,unallocated_seconds=(end-self.cursor)/1e9,phases=self.phases,cpu_seconds=cpu()-self.cpu_start,io_bytes=None,disk_peak_bytes_lower_bound=bytes_on_disk,cache_state=cache_state,cache_key=cache_key,completed_at=datetime.fromtimestamp(self.wall+(end-self.start)/1e9,timezone.utc).isoformat(),unknown=['io_bytes','peak_between_phase_samples'])
