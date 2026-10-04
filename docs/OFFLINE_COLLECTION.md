# KernelX 离线 8 小时采集

仅复制 [`deploy/offline/kernelx_overnight.py`](../deploy/offline/kernelx_overnight.py) 到服务器即可；不访问网络，不安装或更新依赖。
Python >=3.9，宿主必须已有 CANN、c++17、msprof。多库性能采集另需已安装的
Torch/torch_npu 和对应算子库；执行上游 pytest CI 另需已有 pytest 及上游测试依赖。

```bash
source /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
unset ASCEND_RT_VISIBLE_DEVICES ASCEND_VISIBLE_DEVICES
python3 kernelx_overnight.py --self-test
python3 kernelx_overnight.py --inventory > installed-ci-versions.json
nohup python3 -u kernelx_overnight.py --device 0 --hours 8 \
  --ci-hours 4 --ci-level 2 --data-dir "$HOME/kernelx-night" \
  > kernelx-night.log 2>&1 < /dev/null &
```

总预算是从该运行开始的 **8 小时**，包含探测、CI、JIT/编译、性能测试、导出和后处理。
CI 阶段最多用其中 4 小时；若匹配的 CI 队列提前完成，剩余时间全部用于性能采集。
最后预留清理时间，不启动跨越预算的新命令；磁盘不足、释放未确认或基础采集流水线
持续失败会提前停止，并记录原因。8 小时是采集上限，不承诺全部 Cartesian product 都能完成。
Linux 文件处理/最终写盘耗时可能让进程最后退出稍晚于预算；设备工作在截止前停止。

## 与服务器版本匹配

先读取已安装 distribution 版本、direct_url.json Git commit、构建版本的 commit 后缀，
以及包附近的 Git 源码目录。优先使用服务器本地对应测试。已安装 commit 与源码 commit
不一致时拒绝执行；相同 commit 的内嵌测试快照才可作为离线替代。
仅有一个语义版本字符串而没有源码/commit，无法可靠区分同号的不同构建，此时记
`VERSION_UNMATCHED`，不会套用当前上游测试。版本清单仍记录实际安装的版本号。

已有本地源码时可沿用仓库约定的来源配置：

```bash
export KERNELX_LIBRARY_ROOTS='{"ops-nn":"/path/to/ops-nn","ops-transformer":"/path/to/ops-transformer","sgl-kernel-npu":"/path/to/sgl-kernel-npu","tile-kernels":"/path/to/TileKernels","deepgemm-ascend":"/path/to/DeepGEMM-Ascend","deepep-ascend":"/path/to/DeepEP-Ascend"}'
```

也可逐库传 `--ci-root sgl-kernel-npu=/path/to/sgl-kernel-npu`。
显式指向的干净本地源码如果缺少安装包 commit，只能标记 `LOCAL_SOURCE_DECLARED`；
这是部署者提供的源码对应关系，不等同于实际加载 extension/JIT 的认证。
ops-nn/ops-transformer 的 C++ 测试配置会进入来源清单，但这个脚本没有其原生测试
二进制的通用执行适配器，不会借用 Torch 分派冒称独立库采集。
DeepEP 多 rank 通信同样登记形状和参数来源，并标记需要联合 rank Runner。

内嵌四库测试、辅助生成器、CI workflow、许可证和来源索引：

| 库 | 快照 commit |
|---|---|
| sgl-kernel-npu | 653e519cf9556b0cc8d9e7966aa4551eb65143ae |
| TileKernels | 66258df6175d2f630ffecb04c5ab66bff8a2ae6a |
| DeepGEMM-Ascend | 8491bbb4b8c02a094a2318965f50c70438a3e73c |
| DeepEP-Ascend | 3b25377d04b24fc6154698ded78a2bcb2c59afff |

## CI 与性能数据

* `ci-shape-inventory.json` 保存所有已找到的参数装饰器、形状表达式、循环生成器、
  来源文件哈希、版本和 commit；动态生成器不伪装成已展开的具体形状。
* 匹配的 CI 测试按上游函数/脚本执行，pytest 参数完整保留，默认 TK_TEST_LEVEL=2。
  正确性检查、非连续布局、空输入、无效参数和边界测试保留上游行为。
* CI 采集是 **CI_TEST_TRACE**：包括参考实现、冷启动和初始化；不生成 latency-v1
  observation，不把整个 test 的时间当单算子延迟。DeepGEMM 脚本禁用其内层 profiler，
  保留其原有形状/转置/累加/grouped 枚举，避免重复启动 profiler。
* 可执行隔离入口另外加入匹配 CI 的正向 shape/layout 种子。TileKernels 使用 Ascend
  32 channels/scale，覆盖 8001 等非整齐 token 数、65536 hidden 和正向小输入；
  SGL 保留 3D、stride2、转置和 broadcast；DeepGEMM 包含 unaligned、投影和梯度维度。
  形状种子不是原 CI 全参数组合的等价替代，完整参数变体属于上游 CI trace。
* 模型补充矩阵有 714 个候选，覆盖 Add、GEMM、RMSNorm、SwiGLU、GQA attention、
  RoPE、卷积和路由组件，以及三个扩展库的独立入口。实际数量受已安装库、版本、
  内存预算和时限影响。输入为常量 ones；隔离性能采集不做数值正确性检查。
* 20 次预热和 30 次测量；首调/JIT 和预热在性能 profiler 边界外。
  首轮优先未访问的候选，随后重复；失败 case 最多尝试两次。
* 自动保存新 run 目录；默认剩余磁盘低于 5 GiB 时停采，不删除原始证据。
  每 case 内存预算是估计过滤器，不能保证上游 CI 的实际 HBM 峰值。
* 扩展库加载来源保持 DECLARED_ONLY，不冒称完成 JIT/制品认证。

模型维度锚点来自官方配置，并补充 decode/prefill 的代表性 token/context 轴：
[Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B/raw/main/config.json)、
[Qwen3.5-35B-A3B](https://huggingface.co/Qwen/Qwen3.5-35B-A3B/raw/main/config.json)、
[Qwen3.5-27B](https://huggingface.co/Qwen/Qwen3.5-27B/raw/main/config.json)、
[DeepSeek-V3](https://huggingface.co/deepseek-ai/DeepSeek-V3/raw/main/config.json)。
只测独立组件，不代表完整模型、GatedDeltaNet、MLA attention、tensor parallel 或多卡性能。

## 结果

输出 `kernelx-night/night-*/`：

| 文件 | 内容 |
|---|---|
| summary.json | 截止时间、停采原因、实际覆盖数、跳过与未覆盖数 |
| ci-summary.json | 上游 CI 文件执行情况及 passed/failed/skipped test 数 |
| ci-shape-inventory.json | 匹配状态、CI shape/参数生成表达式和来源 |
| matrix.json | 实际性能队列、来源、内存过滤项 |
| attempts.jsonl | 每次隔离性能采集的持久记录 |
| latencies.csv | 完整有效采集的 device span median/p95 和同步 host latency |
| ci-*/tests.jsonl | 每个 pytest nodeid 的原始参数和结果 |
| ci-*/raw/ | 上游 CI 原始 trace/导出；包含参考实现与初始化 |
| run-*/ | 隔离性能样本、原始 PROF、环境、构建/运行/导出日志和释放证据 |

本机仅完成 CPU 自检、真实导出 fixture 回放和匹配/shape 回归，没有在 950DT 运行。
现有采集器要求 logical_id==npu_id 且 chip_id==0；未知 npu-smi/export 格式会拒绝结果。
v0.1.1 修复 950DT 的五列表头（含 Slot ID、Chip Phy-ID）被旧解析器错位读取的问题。
此表头仅支持无可见设备重映射、Chip ID 为 0 且 NPU ID 与 Chip Phy-ID 相等的直连布局；
不将任意物理 ID 表视为逻辑 ID 映射。此次修复使用服务器提供的输出回放验证，
不代表已完成 950DT 的 profiler、编译和设备释放格式验证。

## 从源码重建单文件

```bash
python3 scripts/offline_overnight/build.py
python3 -m unittest discover -s scripts/offline_overnight -p 'test_offline.py' -v
python3 deploy/offline/kernelx_overnight.py --self-test
```

构建器只使用标准库。固定的上游测试与许可证保存在
`scripts/offline_overnight/ci_sources/`，来源索引为该目录的 `sources.json`。
