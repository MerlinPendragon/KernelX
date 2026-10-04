# 全局计划与多 Agent 参考控制端（PR #9 补充）

更新版 issue #3 尚未全部交付。本 PR 修复三条可靠性审查意见，并增加可运行的
文件系统参考控制端与多 Agent 集成。真实测试范围为 910B1 的 CANN Add 种子
用例；七个库的全量枚举、生产跨服务器 API 和第二台实机证据仍是 #3 的剩余工作。

## 提交、执行和查询

```sh
python3 -m kernelx fleet-submit --control-dir /srv/kernelx/control \
  --request /etc/kernelx/fleet-request.json
python3 -m kernelx fleet-agent-tick --control-dir /srv/kernelx/control \
  --state /var/lib/kernelx/fleet --policy /etc/kernelx/server-policy.json \
  --center-dir /srv/kernelx/center
python3 -m kernelx fleet-status --control-dir /srv/kernelx/control --run-id '<global-run-UUID>'
```

`configs/fleet-request.example.json` 默认没有 VERIFIED 能力，不会启动 NPU。
提交选择 `libraries: "all"` 或库名数组、`scope: core/full`、明确服务器集合、
`mode: shard/paired`、有限有效期与采集预算。服务器 UUID 与稳定 device_uid
显式绑定，不猜测 SSH 别名。submission_id 相同且内容相同返回同一 run_id；
同 ID 修改内容拒绝。SQLite WAL 事务持久保存不可变请求和分派。

库名为 cann-opp、ops-nn、ops-transformer、sgl-kernel-npu、tile-kernels、
deepgemm-ascend、deepep-ascend。当前只有冻结 CANN Add 种子可执行，另外
六库标 ADAPTER_UNCONFIGURED。CANN 的 core/full 全库枚举也单独保留一个
ADAPTER_UNCONFIGURED 缺口，不把种子用例代表整个库。`core/full` 已进入
不可变请求，但当前不会生成不同的全库用例集合；该能力须接入 #5 清单。

CANN 种子选机使用提交中的能力快照：VERIFIED 记录必须包含 soc、bin、cann、
library_version、library_commit、preset、manifest_sha256、environment_sha256、
evidence_uri；preset 和 manifest 必须匹配冻结版本。未知版本/commit 保留 null，
不能凭目录名补写。缺失/过期 manifest 或未提供验证依据标 UNVERIFIED；明确
不适用标 UNSUPPORTED。快照是控制端选择依据，生产部署仍需可信支持矩阵
服务验证证明及目标机环境新鲜度；当前 Runner 自己执行原有设备/能力预检，
并保留实际采集环境。示例或模拟声明不能作为真实支持矩阵证明。

shard 将每个已枚举用例绑定到一台适用服务器；paired 则有意绑定到每台适用
服务器。当前仅有一个种子用例，因此 shard 仅产生一个执行分派；不能据此
宣称已经完成多用例的负载分片。两个模拟 Agent 的并行验收采用 paired。

## 进度、恢复和数据库关联

每个 Agent 主动拉取绑定到自身的队列，遵循本机人工窗口和设备锁，每机一个
worker。控制端短事务有有界等待，中心导入也串行提交短事务；采集本身不持有
控制端或中心导入锁，不会使不同服务器的采集串行化。运行代码无 SSH 调度。

`fleet-status` 返回全局、逐库、逐服务器统计与分派明细/原因。状态包括 PENDING、
RUNNING、PENDING_UPLOAD、INGESTED、FAILED、INTERRUPTED、UNSUPPORTED、
UNVERIFIED、ADAPTER_UNCONFIGURED；有部分入库而有缺口时为 PARTIAL。
不能将异常/未验证/未配置计作成功覆盖。目前 Runner 同步完成采集和导出，
独立 CAPTURE_COMPLETE/PENDING_EXPORT 阶段上报尚待 #3 接入 Runner 事件。

Agent state 以 dispatch_id 隔离。未启动且窗口预算不足的分派留在 PENDING，
下一有效窗口继续；已采集的分派只恢复/上传，不开启新一天的重复测量。
断网与控制端回执丢失不会重测。每个本机进度报告先原子写入 report.json，
再上报，重启重发相同序号。中心按 dispatch_id/序号/内容哈希幂等接收；旧序号、
同序号不同内容、服务器不匹配或终态回放均拒绝。某机失败不阻塞其他机器。

失败重试必须显式提出新的 retry_id，生成新的分派和 attempt，保留原分派：

```sh
python3 -m kernelx fleet-retry --control-dir /srv/kernelx/control \
  --dispatch-id '<failed-dispatch>' --retry-id '<operator-retry-ID>'
```

成功 bundle 的 agent-context.json 含 global_run_id/dispatch_id/mode/library。
中心在同一导入事务中保存 fleet_links，关联 bundle、全局 run、分派、服务器、
设备、Agent attempt、协议 session/attempt；`center-entry` 可查关联、实际软件
版本/commit 和原始产物索引。失败/未配置/未验证分派及 Agent attempt 摘要在
fleet.db 中查询，失败不会产生有效 observation。不同服务器配对采集保留各自
session/environment/observation 身份，中心不会把同 case 不同机器的数据合并。

## 证据与剩余责任

`tests/test_fleet.py` 验证两个独立连接/状态目录的 CPU 模拟 Agent 采集区间实际
重叠，两个 bundle 共 60 条观测，重复上传保持 60 条。还覆盖重复提交、分片
与配对、单机失败、显式新分派重试、上传断网/重启、控制回执丢失、无效策略
仍上传、跨窗口推进且成功后不按天重测。原始 archive 为合成测试数据。
`tests/fixtures/agent_910b1/review-fleet-simulation.json` 明确标注 CPU 模拟。

910B1 的新版真实闭环在 `ISSUE3_910B1_VALIDATION.md` 追加记录；一机实际采集
30 条观测、中心事务入库和全局关联成功，全局状态 PARTIAL，七个清单/适配器
缺口明确保留。这不是七库全量成功或两台实机并行的证据。

issue #3 保持开放，剩余责任为：

- 接入 #5 的所有库 core/full 枚举、统一 Runner/解析器及真实支持矩阵；deepep
  多机资源组按 #5 全组协调，当前没有把组任务降为独立单机任务执行。
- 将本地控制 API 接入生产 HTTPS 拉取/上报、鉴权与可靠服务部署。当前 CLI
  使用同一主机的本地 SQLite 参考服务，不是可直接跨 SSH 主机共享的生产队列。
  上传客户端已有 HTTPS 合约，但本 PR 不提供远程控制服务。
- 接入采集/导出阶段事件，并在服务断开时继续已有本机队列；本参考 worker
  控制端不可读时只能保存本机完整采集及待发报告，不能继续拉取新分派。
- 获得第二台服务器的注册身份/别名及有效预约后，补交真实两机并行证据。
  未配置第二台机器；模拟结果不能替代该验收。#4 提供离线启动器/部署。
