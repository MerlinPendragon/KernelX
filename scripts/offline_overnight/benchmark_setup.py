"""Offline diagnostic operators, using actual upstream entry points only.

Inserted into the existing ACL range profiler by build.py. No import touches NPU.
"""
def setup(spec):
    import importlib
    import torch
    import torch_npu
    torch.set_grad_enabled(False)
    torch.manual_seed(0)
    torch.npu.set_device(spec['device'])
    case = spec['case']['case']
    attrs = case['attributes']
    dtype = getattr(torch, case['inputs'][0]['dtype'])
    lib = spec['library']
    def ones(index):
        shape = tuple(case['inputs'][index]['shape'])
        view = attrs.get('input_views', {}).get(str(index))
        if view == 'stride2':
            return torch.ones((shape[0],shape[1]*2),dtype=dtype,device='npu')[:,::2]
        if view == 'transpose':
            return torch.ones((shape[1],shape[0]),dtype=dtype,device='npu').t()
        return torch.ones(shape, dtype=dtype, device='npu')
    x = ones(0)
    module = torch_npu
    op = case['operator']
    if lib == 'sgl-kernel-npu':
        module = importlib.import_module('sgl_kernel_npu.activation.fused_sigmoid_mul')
        gate = ones(1)
        function = (lambda: module.fused_sigmoid_mul_broadcast(x, gate)) if op == 'fused-sigmoid-mul-broadcast' else (lambda: module.fused_sigmoid_mul(x, gate))
    elif lib == 'tile-kernels':
        module = importlib.import_module('tile_kernels.quant.per_token_cast_kernel')
        if not module.is_ascend():
            raise ValueError('TileKernels Ascend backend required')
        function = lambda: module.per_token_cast(x, attrs.get('fmt','e4m3'), attrs['num_per_channels'], use_packed_ue8m0=False, round_sf=False)
    elif lib == 'deepgemm-ascend':
        module = importlib.import_module('deep_gemm')
        b = ones(1)
        out = torch.empty(tuple(case['outputs'][0]['shape']), dtype=getattr(torch,case['outputs'][0]['dtype']), device='npu')
        function = lambda: module.bf16_gemm_nt(x, b, out)
    elif lib == 'torch-npu':
        if op == 'matmul-nt':
            module = torch
            b = ones(1)
            function = lambda: torch.mm(x, b.t())
        elif op == 'Add':
            module = torch
            y = ones(1)
            function = lambda: torch.add(x, y)
        elif op == 'RMSNorm':
            weight = ones(1)
            function = lambda: torch_npu.npu_rms_norm(x, weight, epsilon=1e-6)
        elif op == 'SwiGLU':
            function = lambda: torch_npu.npu_swiglu(x, dim=-1)
        elif op == 'RotaryMul':
            cosine, sine = ones(1), ones(2)
            function = lambda: torch_npu.npu_rotary_mul(x, cosine, sine)
        elif op == 'GQA-Attention':
            k, v = ones(1), ones(2)
            # Full bottom-right causal mask, prepared outside profile and warmup.
            sq, skv = x.shape[2], k.shape[2]
            mask = torch.triu(torch.ones((sq, skv), dtype=torch.bool, device='npu'), diagonal=skv-sq+1)
            function = lambda: torch_npu.npu_fusion_attention(
                x, k, v, attrs['heads'], 'BNSD', atten_mask=mask,
                scale=attrs['head_dim'] ** -0.5, keep_prob=1.0, sparse_mode=0)[0]
        elif op == 'causal-depthwise-conv1d-component':
            module = torch
            import torch.nn.functional as functional
            weight = ones(1)
            # Raw convolution component only; no claim of full GatedDeltaNet.
            function = lambda: functional.conv1d(x, weight, padding=3, groups=attrs['channels'])[..., :x.shape[-1]]
        elif op == 'sigmoid-topk-router-component':
            module = torch
            function = lambda: torch.topk(torch.sigmoid(x), k=8, dim=-1)
        else:
            raise ValueError('unimplemented torch-npu operator: ' + op)
    else:
        raise ValueError('unimplemented offline library: ' + lib)
    return torch, torch_npu, module, function
