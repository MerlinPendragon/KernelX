# KernelX

Ascend 性能采集工程。已交付协议 v1、环境探测，并实现 issue #2 的原生 CANN Add 单机 msprof 采集/导出/归因 Runner；默认 20 次预热。已实现 issue #3 的预约窗口 Agent、SQLite WAL 恢复和幂等中心导入；离线发布器属于后续 issue #4。

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
