# KernelX

Ascend 性能采集工程。已交付协议 v1、环境探测，并实现 issue #2 的原生 CANN Add 单机 msprof 采集/导出/归因 Runner；默认 20 次预热。已实现 issue #3 的预约窗口 Agent、SQLite WAL 恢复、幂等中心导入与全局分派参考控制端；更新后的全库/生产多服务器范围尚未全部完成，#3 保持开放；已实现 issue #4 的签名离线发布、稳定启动器和原子回退，启动/采集不依赖 Codex。

Python ≥3.9，运行与测试不需安装依赖。probe / validate / case-key 是独立 CLI，运行不依赖 Codex、AI agent 或在线服务，可直接从仓库执行：

```sh
python3 -m unittest discover -s tests -v
python3 -m kernelx validate case examples/case.json
python3 -m kernelx case-key examples/case.json
# 在 runner 实际使用的 Python / shell / 容器环境中执行。
. /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
python3 -m kernelx probe \
  --server-id 55dcc47c-f8a8-4f3f-ab2d-c02bd385c470 \
  --output environment.json
python3 -m kernelx validate environment environment.json
```

示例 server ID 是本次 910B1 测试注册 UUID；其他服务器必须注册各自 UUID，不以 hostname、IP 或逻辑 device ID 代替。默认脱敏；`--include-identities` 仅用于私有本地证据。每个外部命令默认超时 15 秒，可用 `--timeout` 调整。

- [签名离线发布、启动器与 systemd 安装](docs/RELEASE.md)
- [全局计划、多 Agent 模拟与剩余范围](docs/FLEET.md)
- [预约 Agent、持久恢复、回传协议与数据库查询](docs/AGENT.md)
- [Agent 910B1 截止、撤销与真实入库验收](docs/ISSUE3_910B1_VALIDATION.md)
- [CANN Runner、默认 20 次预热及独立 CLI](docs/CANN_RUNNER.md)
- [CANN 闭环与超时/中断实机验收](docs/ISSUE2_910B1_VALIDATION.md)
- [协议、字段语义与升级规则](docs/PROTOCOL.md)
- [910B1 验收、命令与环境摘要](docs/910B1_VALIDATION.md)
- [设计](DESIGN.md) / [效率模型](EFFICIENCY_REPORT.md)

`kernelx/schemas/` 为 Draft 2020-12 JSON Schema，`kernelx/types.py` 为配套 TypedDict。`kernelx.protocol.validate` 还校验事实状态、证据哈希与来源引用等业务约束，消费者必须调用它，不能只做字段类型检查。

### 算子库版本与源码来源

环境快照的 `software.operator_libraries` 分别记录 `cann-opp`、`ops-nn`、
`ops-transformer`、`sgl-kernel-npu`、`tile-kernels`、`deepgemm-ascend` 和
`deepep-ascend` 的版本、Git commit、仓库 URL 和制品哈希。
安装包版本从包元数据读取；Git 安装的 commit 从 `direct_url.json` 读取。
源码部署可通过 `KERNELX_LIBRARY_ROOTS` 指定每个库的仓库根目录，例如：

```sh
export KERNELX_LIBRARY_ROOTS='{"ops-nn":"/path/to/ops-nn","ops-transformer":"/path/to/ops-transformer","sgl-kernel-npu":"/path/to/sgl-kernel-npu","tile-kernels":"/path/to/tile-kernels"}'
```

源码仓库记录 HEAD、origin 和工作区是否有修改。没有独立包版本元数据的
源码库版本保持未知，不能用 Toolkit 版本代填。Git 来源、未知原因、证据 ID
及工作区状态保存在 `extensions.library_provenance`。这些信息声明源码来源，
实际运行的库仍由 Runner 的加载路径和制品哈希验证；不修改历史采集快照。

## 扩展库目录、联合 rank 与效率报告

`library-inventory` 不导入 torch 或初始化 NPU。四个扩展库各冻结一个性能入口，
`core` 为 128 tokens，`full` 为 128/1024/4096 tokens 的有限目录；不表示上游所有 API。
目录同时锁定上游 commit、输入/输出、依赖与资源需求。ops-nn、ops-transformer 保持独立版本行；
尚无执行目录的组件返回 `ADAPTER_UNCONFIGURED`。

```sh
python3 -m kernelx library-inventory --environment environment.json \
  --device-uid DEVICE_UUID --scope core --matrix support-state --output inventory.json
```

将输出中的逐库 capability 放入 `fleet-submit` 的服务器 `capabilities`。扩展库的
`VERIFIED` 必须匹配冻结 manifest、环境 tuple 和实际 profile/provider 证据；未知或不支持
组合不会排队。独立入口通过同一 FleetWorker/Agent/Runner 进行 case 绑定、监督、释放确认、
封包和中心导入。首次兼容性 pilot 使用 `collect-library`，参数与 `collect-cann-add` 相同，
另加 `--library` 和 `--case-index`，只允许已安装、未判定 UNSUPPORTED 的组合。
`SupportMatrix.attest_bundle` 校验完整 sealed bundle 和加载来源；`DECLARED_ONLY` 不能升级为已验证。
多 case full 目录需要逐 case 证据，单个 bundle 不证明全部 case。

DeepEP 使用 `group-submit / group-rank-tick / group-status`，与独立 shard/paired dispatch 分开。
请求必须包含 `mode=communication-group`、`library=deepep-ascend`、有效期、pilot 上界、
重复协议、冻结 case_index、全部 rank 的 server/device/logical_id/environment_tuple_sha256，
以及显式 rendezvous master_addr/master_port。`group-submit --policies` 校验每个 rank 的人工预约。
当前冻结 case 为两个 rank；全组在最早截止时间前退出。任一 rank 失败、预约撤销或就绪超时，
其他 worker 取消自己的进程组；确认所有 rank 释放后才解除逻辑 lease。没有释放证据时保留
CANCELLING/lease，不能用部分 profile 报成功。CPU fault injection 独立标记，不产生 NPU 成功结果。
此 SQLite/filesystem 控制面是参考实现；跨服务器需接入 #3 的中心控制 API，不能把 SQLite WAL
文件放到网络共享目录当作已验证的跨机部署。910B1 不支持冻结的 DeepEP 组合。

CANN 可通过 `KERNELX_NATIVE_CACHE_ROOT` 使用私有不可变编译缓存。key 包含源码、编译器、
CANN 头文件、运行库/工具指纹、CPU/SoC/BIN、flags 和 manifest；每次命中校验制品。
扩展 adapter 的 `cache_key` 接口另绑定框架/JIT/通信依赖、上游 commit 和 compiler fingerprints。
上游自身 JIT 缓存未建立加载来源证据时仍为 `DECLARED_ONLY`；不宣称已验证跨环境缓存复用。
JIT 首次调用和 20 次预热均在 profile 边界外。

```sh
python3 -m kernelx efficiency-report --bundles RUN1 RUN2 RUN3 FAILED_RUN \
  --inventory inventory.json --control-dir fleet-control --center-dir center \
  --output efficiency
python3 -m kernelx schedule-pilot --tasks candidate-tasks.json \
  --budget-seconds 3600 --cleanup-seconds 10 --rotation-quota .5 --output proposal.json
```

报告输出 Markdown、HTML、逐 attempt/库/容量 CSV 和输入哈希。成功输入必须通过 sealed bundle 校验；
失败成本可从全局分派中的持久化 Agent 账本恢复。时间并集用于全局 wall-clock，资源小时逐资源累计。
严格 cohort 分组保留 case、BIN、库版本/制品、框架、preset 和重复规则。跨服务器配对需要同一
`paired` global run 和相同 cohort；至少三个独立配对窗口后才给窗口级 bootstrap 比值区间。
同日多轮不替代跨日稳定性。未知成本为 null；建议不会自动增加窗口或设备。

910B1 实测摘要和限制见 [EFFICIENCY_REPORT.md](EFFICIENCY_REPORT.md)，可审查快照位于
`tests/fixtures/libraries_910b1/`。
