# KernelX 协议 v1

本交付建立 runner / agent / analyzer 共用契约。它不启动 benchmark，不承诺任何库或 preset 在目标 NPU 可运行。

## 实体与关联

所有实体必须显式包含 `schema_version=1`、`protocol_version="latency-v1"`、`extensions`。字段全部 required；无法取得的值以规定的 null 和状态表达，不能省略字段或填零。JSON Schema 位于 `kernelx/schemas/`，Python TypedDict 位于 `kernelx/types.py`。示例位于 `examples/`，环境样例是 `tests/fixtures/910b1/environment.json`。

| 实体 | 身份与主要引用 | 语义 |
| --- | --- | --- |
| case | case key 由全部规范化内容生成 | 算子、输入/输出 shape/dtype/layout/stride、属性、生成算法版本/seed/input hash、语义版本 |
| plan | plan_id；policy_id、case_manifest_sha256 | 固定用例清单、策略版本、有效期、预算、优先级；不能覆盖服务器策略 |
| session | session_id；plan、environment、server、devices | 固定 release / 预约 / UTC 窗口；资源账本使用进程单调时钟，不跨主机相减 |
| attempt | attempt_id；session_id、task_id、case_key | 每次重测新 ID；上传重试不得新增采集 attempt；真实设备释放单列 |
| profile | profile_id；attempt、artifact_ids、case_key | 保存 preset/hash、最终 argv、各阶段版本、完整性、任务数量及迭代/rank 归因 |
| observation | observation_id；profile、attempt、session、environment | 保留原始微秒样本、iteration/round/rank、输入身份、指标边界和质量 |
| environment | environment_id；注册 server_id | 不可变环境快照，硬件、软件、支持矩阵与全部原始证据同存 |
| artifact | artifact_id；URI/hash/bytes | raw PROF、CSV、trace、sidecar、日志和环境的内容身份；不声称已持久回传 |

Session 终态、attempt 终态、profile 导出状态彼此独立。UUID 不通过逻辑编号构造全局身份。跨文件引用完整性、URI 可访问性、目录完整性和上传持久确认由后续模块处理；本模块检查环境内 evidence 引用及输出哈希。

## 未知、缺失与证据

`Fact` 含 `value/status/reason/source/confidence`：

- `KNOWN` 必须有非空值、证据 source 与置信级别。`VERIFIED` 是直接读取并解析的事实；`DECLARED_ONLY` 是版本文件/包元信息声明，不能证明 benchmark 实际加载该提供者。
- `UNKNOWN`、`PERMISSION_DENIED`、`UNSUPPORTED`、`PARSE_ERROR`、`NOT_FOUND`、`TIMEOUT`、`COMMAND_FAILED` 必须是 null 值、有原因及证据、`confidence=NONE`。
- `NOT_FOUND` 只限于所读路径或本次 Python 解释器的包元信息，不能推断整台机器未部署该库。
- 证据保留最终 argv、UTC 开始时间、单调耗时、exit code、stdout/stderr、执行状态、解析状态、权限范围、解析器版本、脱敏标识和输出 SHA256。未实现专用解析的原始证据使用 `parse_status=UNKNOWN`。
- 固件文件无权读取、版本字段为 NA、旧安装文件与当前 ops 版本不同均保留，目录名不作为版本值。msprof 不支持版本查询时保留 UNSUPPORTED，并独立保存已解析路径及可执行文件 SHA256。

脱敏在序列化及哈希之前进行：hostname、用户 home 路径和 IP 被替换；SN/Die ID 使用注册 server UUID 作用域的 SHA256 伪名。同一注册身份内可重放和关联；不同注册身份不共用标识。全零/NA Die ID 不作为身份。伪名用于减少公开身份信息，不等于对可枚举值的密码学匿名保证；需要更强匿名时由部署方使用私有注册映射并限制证据访问。PCIe 是位置数据，默认保留。fixture 中的 hash 校验脱敏后的输出，不能证明未脱敏原文相同。

## 硬件映射与 BIN

910B1 探测依次读取 `npu-smi info`、`info -l`、`info -m`，对每个真实 accelerator 行查询 `info -t board -i N -c C` 和 `info -t usages -i N -c C`。`-` 逻辑 ID 的 MCU 行不进入 accelerator inventory。board 与 mapping 的完整 Chip Name 都匹配 `910b1-observed-v1` 表才确认 `soc_family="Ascend 910B"`、`hardware_bin="Ascend 910B1"`。不会截尾 B1/B2 猜测，也不外推到其他产品。

`device_uid` 使用 server UUID + 有效 Die ID 构造，标记 STABLE_CHIP；无 Die ID 时退为位置身份并标记 LOCATION_ONLY。`card_uid` 是 server UUID 作用域下的 PCIe 位置标识，始终标记 LOCATION_ONLY；不以芯片 Die ID 冒充物理卡 SN。当前 910B1 每个 NPU 有一个 accelerator chip；其他多芯片板卡须添加实机 fixture 和物理板卡关系适配器后才能核算物理卡小时。逻辑 ID 仅是当前快照的 runner 选择器。

## key、比较与性能质量

`case_key` 是 `case-v1:` + UTF-8 canonical JSON 的 SHA256：排序 object key，不排序 array；禁止 NaN/Infinity。相同对象键顺序不影响 key。JSON 数字字面类型保留，属性中的 1 与 1.0 不主动合并；生成器应固定编码。所有 case 字段（包括语义 extensions）参加哈希；展示性注释应存储在 case 外。shape/dtype/layout/stride、属性、seed、生成算法版本及 semantic_version 改变都会改变 key。协议/schema 不支持的版本直接拒绝。

`comparison_key` 除 case 还要求已验证 SoC/BIN、驱动、固件、Toolkit、msprof 版本，以及 runtime library / framework / implementation / preset SHA256、解析器版本、协议、编译参数、并发、热状态与通信语义。通信字典由适配器填 world_size、rank 映射、拓扑、HCCL/HCOMM/URMA 版本、路由和有效字节；空字典只用于非通信算子。输入中的任何 null 或声明版本不能用于严格比较。当前 910B1 fixture 有未知固件和声明 Toolkit，严格分组会拒绝它；仍可做明确标为元数据不完整的描述性报告。

指标区分 task duration、device span、设备关键路径、同步 host elapsed、rank elapsed，单位固定 us，必须给出 boundary 和 definition_version；多 stream task 不自动求和。原始样本不得为空、负数或非有限数。COMPLETE profile 必须匹配预期 task 数。仅在 COMPLETE observation 才允许 VALID；VALID 与排除标签不得同时出现。异常/部分结果保留但不作为稳定性有效样本。缺 trace、单位未知、归因不清分别记录 TRACE_MISSING、UNIT_UNKNOWN、ATTRIBUTION_UNKNOWN。

支持矩阵固定 library revision × SoC/BIN × CANN × Python/torch_npu × preset，保存证据与验证日期。本只读探测全部输出 UNVERIFIED：检测到路径或 flags 不表示已完成 preset 归因验证，也不能因为 Python 未安装库就宣称该 NPU 不支持库。VERIFIED/UNSUPPORTED 由后续 adapter 的固定 revision 实测或明确产品依据填写。

## 兼容升级与生成

v1 无隐式字段默认值，无忽略未知顶层字段。业务语义、case key 编码、required 字段、enum 或指标解释改变须升 schema major，并更新 case/protocol 版本及 fixture；旧快照保持不可变。向后兼容的附加证据可放入 extensions，reader 不依赖它才可安全忽略。跨版本比较需显式迁移和重新确认语义，不直接复用 key。

```sh
python3 scripts/generate_schemas.py
python3 scripts/generate_types.py
python3 scripts/generate_examples.py
python3 -m unittest discover -s tests -v
```

Draft 2020-12 schema 可交给通用 JSON Schema validator 做结构验证；`kernelx.protocol.validate` 是完整入口，增加上述业务约束。内置 validator 仅支持本仓库生成 schema 使用的 vocabulary（type/const/enum/anyOf/$ref/properties/required/additionalProperties/items/minItems/uniqueItems/minLength/pattern/minimum），不接收任意用户 schema。
