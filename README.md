# KernelX

Ascend 性能采集工程。当前交付 issue #1：协议 v1、只读环境探测、910B1 真实 fixture。采集 Runner、窗口 Agent 与发布器将在后续 issue 实现。

Python ≥3.9，运行与测试不需安装依赖，可直接从仓库执行：

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

- [协议、字段语义与升级规则](docs/PROTOCOL.md)
- [910B1 验收、命令与环境摘要](docs/910B1_VALIDATION.md)
- [设计](DESIGN.md) / [效率模型](EFFICIENCY_REPORT.md)

`kernelx/schemas/` 为 Draft 2020-12 JSON Schema，`kernelx/types.py` 为配套 TypedDict。`kernelx.protocol.validate` 还校验事实状态、证据哈希与来源引用等业务约束，消费者必须调用它，不能只做字段类型检查。
