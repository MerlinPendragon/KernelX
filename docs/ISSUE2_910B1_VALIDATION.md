# Issue #2：910B1 验收记录

日期：2026-10-04，Asia/Shanghai。用户授权当前测试使用全部设备，本次只使用 device 5；每次独立测试窗口自运行开始最多 5 分钟，常规设备任务 timeout 90 秒，预留 3 秒清理。没有修改共享 CANN、驱动或固件，没有结束其他 NPU 用户进程。

## 正式闭环

最终源码运行目录 `/home/lxb/kernelx-issue2/run-final`；默认 **20 次预热、10 次测量**。20 次预热完成后才启动 profiler，workspace/executor 准备也位于采集边界外。最终结果：

- valid=true；SUCCEEDED；10 个 Add task、10 个唯一测量范围。
- 30 个 observation：10 个 task duration、10 个 device span、10 个同步 host elapsed，保留逐次样本与指标边界。
- device_release=RELEASED，released_by_hard_cutoff=true；UTC 释放确认 `2026-10-03T16:40:47.195553Z`，北京时间 00:40:47。
- 原始 PROF、export CSV/trace、sidecar、环境快照、运行库证据、协议实体、最终命令和日志齐全；artifact 索引包含 URI/hash/bytes。
- `dlsym + dladdr` 确认 aclnnAdd 来自 `/usr/local/Ascend/cann-9.2.0-beta.1/aarch64-linux/lib64/libopapi.so`；保存实际加载的 Ascend host 库路径与 SHA256。设备 kernel 制品加载路径尚未验证，保留 DECLARED_ONLY；旧 Toolkit 9.1.0 声明与 OPP 9.2.0-beta.1 声明冲突仍保留。
- 首版只验证这个冻结 Add case，不能据此宣称其他 CANN 用例、ops-transformer、其他服务器或其他型号已验收。

可提交 fixture：`tests/fixtures/cann_add_warmup20/`。`fixture-origin.json` 把原 run 中的路径映射到 fixture（exports 目录），可对照 `artifacts.json` 校验除独立保留 raw archive 外的所有原始证据 hash。

完整原始归档分别保留在目标机 `/home/lxb/kernelx-issue2/run-final/raw-prof.tar.gz` 和本地 `artifacts/issue2/raw-prof-final.tar.gz`（Git 忽略，含原始机器信息，不作为公开脱敏 fixture）。两端 SHA256 一致：

`4cbabc551f4e85cea2912cb801ec0a2f90092e58854a4563bb1aaf831b145b23`

初次 3 次预热 pilot fixture 仅保留为历史解析正例；默认和正式交付依据均为用户指定的 20 次预热。本次 10 次测量用于采集与归因验收，不作为稳定性统计结论。预热侧车含逐次 host timing，后续结合数据稳定性决定次数；未实现自动判稳。

## 故障与保护

`fault-acceptance.json` 记录两次真实 NPU 故障测试，两次均先完成 20 次预热并启动 profiler，在 FAULT_INJECTION 标记后阻塞：

| 注入 | execution.reason | attempt | NPU 释放 | 有效观测 | 外部 sentinel |
| --- | --- | --- | --- | --- | --- |
| 5 秒超时 | TIMEOUT | FAILED | RELEASED，硬截止前 | 0 | 仍存活 |
| 向 Runner 发 SIGTERM | INTERRUPTED | INTERRUPTED | RELEASED，硬截止前 | 0 | 仍存活 |

只向本任务 PGID 发终止信号，未按名称杀进程、未复位共享 NPU。保留部分 raw PROF/sidecar 但不当成成功样本。Linux 离线故障测试还验证父进程已退出但忽略 SIGTERM 的子进程仍在组内时，Runner 清理该子进程，而外部进程不受影响。

## 验证与复现

910B1 Python 3.9.9：38 项测试全部通过，记录在 fixture 的 `tests.log`；compileall 通过。开发机 Python 3.14.6：37 项通过，Linux /proc 子进程组测试按平台跳过 1 项。最终 fixture 增加了逐个 artifact hash/bytes 关联校验。

```sh
python3 -m unittest discover -s tests -v
python3 -m kernelx parse-cann-add \
  --exports tests/fixtures/cann_add_warmup20/exports \
  --sidecar tests/fixtures/cann_add_warmup20/sidecar.jsonl \
  --device 5 --output /tmp/add-parsed.json
```

独立离线 CLI 在开发机和 910B1 都返回 valid=true。实际设备运行/窗口参数、低开销 preset、环境未知值及故障测试入口详见 [CANN Runner](CANN_RUNNER.md)。重复实机采集必须使用新的输出目录和当时有效的人工设备授权。


### 算子库版本与 Git 来源增量验证

逐库来源探测支持安装包版本、Git 安装 commit 和显式源码仓库的 HEAD/origin；
工作区修改状态与来源证据保留在环境快照。缺失项不由 Toolkit 版本代填。

在既有 910B1 隔离目录运行 `python3 -m unittest discover -s tests -q`，
42 项测试全部通过（5.727 秒）。本地 41 项通过，1 项 Linux 专用检查跳过；
compileall 和 diff whitespace 检查通过。本次为离线增量验证，未重跑 NPU
性能采集；正式 20 次预热 fixture 保持原样。


### PR #8 审查修复验收

修复设备可见性重映射、同设备并发与自定义 CANN 安装路径指纹三项意见。
47 项测试在 910B1 全部通过（6.028 秒）；本地 46 项通过、1 项 Linux /proc
测试跳过。新增回归使用两个独立 CLI 进程，确认只有一个进入同设备 preflight
和 benchmark；验证可见性重排/子集/空值启动前拒绝、子进程继承锁、自定义路径
下改变运行库会改变指纹，及缺必要运行库不产生已验证提供者状态。

真实运行目录 `/home/lxb/kernelx-issue2/run-review-20261004T024633Z`，
沿用用户全部设备可用的授权，仅使用 device 5，固定五分钟窗口、90 秒任务
超时、20 次预热和 10 次测量。生成 30 条有效 observation；锁 ACQUIRED；
NPU 于 `2026-10-04T02:46:55.562143Z`（北京时间 10:46:55）确认释放，
在硬截止前。指纹覆盖 73 个实际加载共享库，必要库及不可读取库清单均为空。

增量证据在 `tests/fixtures/cann_add_review_fixes/`：验收结果、加载库指纹、
侧车及 observation；完整原始采集目录保留在目标机。本地原始 PROF 归档为
`artifacts/issue2/raw-prof-review-fixes.tar.gz`（Git 忽略），SHA256：

`a1d55415400d8bda6d8b297c05961b7562eee201f27eadb869cfaa19197871cf`

这是固定 Add case 的实机验收；并发/自定义根的故障回归使用 CPU 模拟，
不制造真实 NPU 并发干扰，不外推到其他硬件或库。旧正式 fixture 保持不变。
