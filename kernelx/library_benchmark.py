"""Performance-only upstream entry; initialization/JIT/warmup precede ACL profiling.

Invoked only by a manually reserved, compatible Runner. Import does not load
frameworks or touch NPU. A missing dependency fails explicitly, never falls back
to a same-named torch/CUDA operator.
"""
import argparse,ctypes,hashlib,importlib,json,os,time
from pathlib import Path


def invoke(spec):
    cleanup=[]
    try:return _invoke(spec,cleanup)
    finally:
        failures=[]
        for function in reversed(cleanup):
            try:function()
            except Exception as exc:failures.append(str(exc))
        if failures:raise RuntimeError('communication cleanup failed: '+'; '.join(failures))


def _invoke(spec,cleanup):
    import torch
    import torch_npu
    device=spec['device'];torch.npu.set_device(device)
    row=spec['case'];case=row['case'];lib=spec['library'];shape=case['inputs'][0]['shape'];n,h=shape
    x=torch.ones((n,h),dtype=torch.bfloat16,device='npu')
    if lib=='sgl-kernel-npu':
        module=importlib.import_module('sgl_kernel_npu.activation.fused_sigmoid_mul')
        gate=torch.ones_like(x);function=lambda:module.fused_sigmoid_mul(x,gate)
    elif lib=='tile-kernels':
        module=importlib.import_module('tile_kernels.quant.per_token_cast_kernel')
        if x.device.type!='npu' or not module.is_ascend():raise ValueError('Ascend backend required')
        function=lambda:module.per_token_cast(x,case['attributes']['fmt'],case['attributes']['num_per_channels'])
    elif lib=='deepgemm-ascend':
        module=importlib.import_module('deep_gemm');b=torch.ones(tuple(case['inputs'][1]['shape']),dtype=torch.bfloat16,device='npu');out=torch.empty((n,b.shape[0]),dtype=torch.bfloat16,device='npu')
        function=lambda:module.bf16_gemm_nt(x,b,out)
    elif lib=='deepep-ascend':
        module=importlib.import_module('deep_ep');import torch.distributed as dist
        dist.init_process_group('hccl');cleanup.append(dist.destroy_process_group);world=dist.get_world_size()
        if world<2 or world%2:raise ValueError('even all-rank communication group required')
        attrs=case['attributes'];buffer=module.EPBuffer(dist.group.WORLD,num_max_tokens_per_rank=n,hidden=h,num_topk=attrs['num_topk'],use_fp8_dispatch=False,allow_multiple_reduction=True,explicitly_destroy=True)
        cleanup.append(buffer.destroy)
        idx=torch.arange(n*attrs['num_topk'],device='npu',dtype=torch.int64).reshape(n,attrs['num_topk'])%attrs['num_experts'];weights=torch.full((n,attrs['num_topk']),1/attrs['num_topk'],device='npu',dtype=torch.float32)
        def function():
            dispatched=buffer.dispatch(x,topk_idx=idx,topk_weights=weights,num_experts=attrs['num_experts'],do_expand=True,do_zero_padding=True,async_with_compute_stream=True,defer_epilogue=True)
            result=dispatched.wait();recv_x,handle=result[0],result[-1]
            return buffer.combine(recv_x,handle=handle,topk_weights=result[2],async_with_compute_stream=True,defer_epilogue=True).wait()
    else:raise ValueError('unknown upstream performance entry')
    # First invocation forces lazy/JIT compilation; warmups are not profiled.
    torch.npu.synchronize();function();torch.npu.synchronize()
    path=Path(spec['sidecar']);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w') as sidecar:
        for i in range(spec['warmup']):
            start=time.monotonic_ns();function();torch.npu.synchronize();end=time.monotonic_ns()
            sidecar.write(json.dumps(dict(phase='WARMUP',iteration=i,rank=spec['rank'],profile_active=False,start_monotonic_ns=start,end_monotonic_ns=end))+'\n')
        acl=ctypes.CDLL(str(Path(os.environ['ASCEND_HOME_PATH'])/'lib64/libmsprofiler.so'))
        acl.aclprofCreateConfig.restype=ctypes.c_void_p;acl.aclprofCreateConfig.argtypes=[ctypes.POINTER(ctypes.c_uint32),ctypes.c_uint32,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint64]
        acl.aclprofCreateStamp.restype=ctypes.c_void_p;acl.aclprofCreateStamp.argtypes=[]
        acl.aclprofDestroyStamp.restype=None
        acl.aclprofInit.argtypes=[ctypes.c_char_p,ctypes.c_size_t]
        acl.aclprofRangeStop.argtypes=[ctypes.c_uint32]
        acl.aclprofFinalize.argtypes=[]
        for name in ['aclprofStart','aclprofStop','aclprofDestroyConfig','aclprofDestroyStamp']:getattr(acl,name).argtypes=[ctypes.c_void_p]
        acl.aclprofSetStampTraceMessage.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
        acl.aclprofRangeStart.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
        raw=str(Path(spec['raw']).resolve()).encode();device_list=(ctypes.c_uint32*1)(device)
        def checked(value):
            if value!=0:raise RuntimeError('ACL profiling API failed: '+str(value))
        checked(acl.aclprofInit(raw,len(raw)));config=acl.aclprofCreateConfig(device_list,1,255,None,spec['preset']['data_type_mask'])
        if not config:raise RuntimeError('ACL profiling config failed')
        sidecar.write(json.dumps(dict(phase='PROFILE_START',rank=spec['rank'],warmup_completed=spec['warmup']))+'\n');sidecar.flush();checked(acl.aclprofStart(config))
        try:
            for i in range(spec['repeats']):
                stamp=acl.aclprofCreateStamp()
                if not stamp:raise RuntimeError('ACL stamp allocation failed')
                name=('kernelx:measure:'+str(i)).encode();checked(acl.aclprofSetStampTraceMessage(stamp,name,len(name)));rid=ctypes.c_uint32()
                start=time.monotonic_ns();checked(acl.aclprofRangeStart(stamp,ctypes.byref(rid)));function();torch.npu.synchronize();checked(acl.aclprofRangeStop(rid));end=time.monotonic_ns();acl.aclprofDestroyStamp(stamp)
                sidecar.write(json.dumps(dict(phase='MEASURE',iteration=i,rank=spec['rank'],profile_active=True,range_name=name.decode(),start_monotonic_ns=start,end_monotonic_ns=end))+'\n');sidecar.flush()
        finally:
            try:checked(acl.aclprofStop(config))
            finally:
                try:checked(acl.aclprofDestroyConfig(config))
                finally:checked(acl.aclprofFinalize())
        sidecar.write(json.dumps(dict(phase='PROFILE_STOP',rank=spec['rank'],measured_iterations=spec['repeats']))+'\n')
    mapped=[]
    if Path('/proc/self/maps').is_file():mapped=sorted({line[line.find('/'):].strip() for line in Path('/proc/self/maps').read_text().splitlines() if '/' in line and '.so' in line})
    # Module identity is evidence; imported Python alone does not prove JIT kernel
    # provenance. Verification remains DECLARED_ONLY until JIT paths are linked.
    Path(str(path)+'.providers.json').write_text(json.dumps(dict(certification_status='UNIMPLEMENTED',certification_reason='observed module/mappings only; backend extension/JIT source association is not implemented',loaded_files=[module.__file__]+mapped,version=getattr(module,'__version__',None),git_commit=None,provider_confirmed=False,jit_artifacts=[],framework=dict(torch=torch.__version__,torch_npu=getattr(torch_npu,'__version__',None)),communication=dict(backend='hccl') if lib=='deepep-ascend' else None)))



def main():
    parser=argparse.ArgumentParser();parser.add_argument('--spec',type=Path,required=True);args=parser.parse_args();invoke(json.loads(args.spec.read_text()))
if __name__=='__main__':main()
