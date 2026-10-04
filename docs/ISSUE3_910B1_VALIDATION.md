# Issue #3：910B1 Agent 验收

日期：2026-10-04（Asia/Shanghai）。用户已授权全部设备可用于当前测试，本轮
只使用 device 5，注册 server UUID 为 `55dcc47c-f8a8-4f3f-ab2d-c02bd385c470`，
稳定芯片 UID 为 `1ee8d14b-2e71-5beb-aa56-e58fd4c7b6ae`。未安装长期 timer，
未修改共享 CANN/驱动/固件，未结束其他 NPU 用户任务。

## 最终代码验证

- 910B1 Python 3.9.9：72 项 unittest 全部通过（日志见 fixture），compileall 通过。
- 开发机：69 项通过，3 项 Linux /proc 专用检查跳过；compileall/diff --check 通过。
- 60/90/120 分钟、过午夜、停采日/过期/错过窗口、重复触发、预算/缓存水位、
  撤销/外部占用、登记前后崩溃、部分产物恢复、未知所有权隔离、PID/boot 复用、
  丢失 ACK/重复导入/断网退避/确认后清理均有可控 CPU 故障测试。
- 最终元数据检查增加计划/preset 指纹、预约身份、设备和重复策略一致性；
  对真实已采集 bundle 离线重复导入成功，中心仍只有 30 条观测，并导出真实
  数据库关联样例。缓存空间恢复只在原窗口继续，过期恢复完整 case 保持成功。

最终日志及结构化验收：`tests/fixtures/agent_910b1/`。

## 真实采集与回传

隔离源码目录 `/home/lxb/kernelx-issue3`，正式证据目录：

`/home/lxb/kernelx-issue3/acceptance-20261004T034643Z`

| 路径 | 配置与故障 | 结果 |
| --- | --- | --- |
| success | 180 秒窗口；20 次预热、10 次测量；中心提交后模拟 ACK 丢失 | 仅 1 次采集；30 条观测；spool 在未确认前保留；5 秒退避后重传、ACKED 后清理；中心仍为 30 条 |
| hard-cutoff | 40 秒人工窗口、3 秒清理预留；30 秒 profiler 活跃暂停注入 | TIMEOUT；FAILED；实际设备 RELEASED，硬截止前；无 outbox/有效观测 |
| revoked | 120 秒窗口；预热结束、profiler 活跃暂停后原子撤销 policy | CANCELLED；FAILED；实际设备 RELEASED，硬截止前；无 outbox/有效观测 |

三个路径均通过。故障测试的独立外部 CPU sentinel 始终存活，仅结束本任务
组。硬截止测试故意注入超出常规成本的暂停，不作为成本容量/稳定性结论。

NPU 释放确认时间（UTC）：

- success：`2026-10-04T03:47:05.818648Z`（北京时间 11:47:05）。
- hard-cutoff：`2026-10-04T03:47:54.301292Z`（北京时间 11:47:54）。
- revoked：`2026-10-04T03:48:13.203347Z`（北京时间 11:48:13）。

成功入库的 bundle ID：

`7cfcc5fa2e4e86ca1dec4d6167d97e13c4d3c2ab9eab4dec99fd6ae73f436ca2`

实际中心 SQLite 位于证据目录的 `success/center/center.db`，原始 bundle 位于
`success/center/bundles/<bundle-id>/`。`database-row.json` 由 `center-entry`
从真实持久数据库读取 observation，并关联 case、硬件、software。
software 保留 Toolkit 与 ops-nn、ops-transformer、sgl-kernel-npu、tile-kernels
等独立条目及 Git commit、状态/来源；未知项不代填、不冒称已安装或已加载。

可在目标机离线复核（不占 NPU）：

```sh
cd /home/lxb/kernelx-issue3
python3 -m unittest discover -s tests -q
python3 -m kernelx center-entry \
  --center-dir acceptance-20261004T034643Z/success/center \
  --output /tmp/agent-database-row.json
```

首次故障脚本使用了超过 native 支持上限的暂停值，因此该次仅得到正常
失败/设备释放，未算截止验收；已改为允许的 30 秒并完成上述正式测试。
早期源码归档带入的 Mac AppleDouble 文件已清理，最终归档禁止该元数据。

## 原始证据与范围

完整 SQLite、原始 PROF/CSV/trace、侧车、冻结 policy/plan、运行日志、资源
账本、中心制品和失败 spool 均封存在目标机 `acceptance-evidence.tar.gz` 与
本地 `artifacts/issue3/acceptance-evidence.tar.gz`（Git 忽略、私有文件权限）。
两端 SHA256 一致：

`d9028102ac88ae0197be3cdb22242ac6b8081db863adc7e9ad53401b7ea53054`

本轮只验收单 worker、固定 CANN Add、910B1。本地中心是持久导入协议的
可执行参考；生产 HTTPS 服务端部署与鉴权由部署环境提供，未配置真实外网
回传 endpoint。跨型号、其他库、多 rank、长期无人交互连续运行、成本 pilot
与统计稳定性分别在 #4–#6 验收。本轮默认预热仍为用户要求的 20 次，后续
依据稳定性分析决定次数，未用 10 次测量宣称达到统计稳定。

## PR #9 审查修复与更新版 issue 补充（2026-10-04）

此前 72 项日志/正式窗口证据保持原样。新增验证如下：

- 910B1 最终 88 项 unittest 全部通过；本机 84 项通过、4 项 Linux 专用跳过。
  新增执行记录写入失败的所有权回收/隔离、真实 CPU 进程回收、损坏最终记录、
  窗口前启用/预算修改及损坏/缺失配置仍回传等回归。
- 两个 CPU 模拟 Agent 的实际采集区间重叠 **0.23154 秒**；共 60 条观测，
  重复上传仍为 60 条。记录为 review-fleet-simulation.json，明确标 CPU 模拟，
  不代表两台实际 NPU 服务器。全量模拟覆盖分片/配对、单机失败、新分派重试、
  断网/重启、回执丢失、跨窗口继续且成功后不按天重测。
- 910B1 真实全局提交→本机主动拉取→CANN Add 采集→中心事务导入闭环：
  device 5、20 次预热、10 次测量、180 秒有限窗口、90 秒任务超时、3 秒清理。
  30 条观测，重复提交返回同一 run，重复 tick 无新 attempt；fleet_links 保存
  run/dispatch/server/device/Agent attempt/协议 session/attempt 关联。
- 全局状态 **PARTIAL**：1 个 CANN Add 种子 INGESTED，7 个库适配器/完整清单
  缺口 ADAPTER_UNCONFIGURED。未将其标为全库已完成，issue #3 保持开放。

真实 run：`d8e6bdbf-bfe0-4876-af74-cd8aa97620be`。
真实 bundle：`1359e798e6faa9d323a49e1b1ae2b523d1a5394a9a52942c3d4e33bdd9e5917e`。
设备释放确认 UTC：`2026-10-04T05:36:04.130101Z`（北京时间 13:36:04）。

原始 SQLite、产物和私有冻结配置在：
`/home/lxb/kernelx-issue3/fleet-acceptance-20261004T053542Z`。
公开摘要与回归日志位于 `tests/fixtures/agent_910b1/review-*`；新数据库关联
样例为 `review-fleet-database-row.json`。原始证据另封装为私有
`artifacts/issue3/review-fleet-evidence.tar.gz`，不提交原始 PROF。
生产跨机服务、所有库适配器、阶段进度事件和第二台实机验收的具体剩余范围
见 [FLEET.md](FLEET.md)。

新版私有原始归档 SHA256：
`e6ca6e44c25e05e51e716f1194cd0a5a5b3177d4434ab41723f94d2492bed33b`。


## PR #9 服务器共享资源复审修复

针对 59da5c7 的两条复审意见，所有 Fleet 分派改用同一 worker 的持久隔离账本
与累计 spool 水位。最终 93 项测试在 910B1 全部通过，本机 89 通过、4 项 Linux
专用跳过；compileall 和 diff --check 通过。完整日志为
`tests/fixtures/agent_910b1/shared-resource-review-tests.log`。

新增五项 CPU 故障回归（均为 CPU runner double，不占 NPU）：

- RESIDUAL/UNKNOWN 后，新全局 run 和显式 retry 都不启动；身份不匹配或空闲
  查询 UNKNOWN 时不能解除，人工校验成功后两分派恢复并分别入库 30 条观测。
- worker 中断后的恢复写入共享隔离，后续新分派保持 PENDING，不进入 Runner。
- 1000-byte server 配额、0.8 水位、600-byte 预计产物，首分派保留 650-byte
  失败文件后，第二分派及重启 tick 都不再调用 Runner（context 也计入占用）。
- 已完成数据和上传归档跨分派累计；后续任务暂停，持久 ACK 清理完整数据及
  对应上传归档后，同一窗口恢复，累计仍为两次采集，无重测。
- 旧分派隔离状态升级迁移后仍阻断；人工解除后重启不会重新导入旧隔离。

本轮验证服务器共享状态与缓存调度，没有新增 NPU benchmark。此前真实
910B1 采集/截止/撤销与数据库证据保留，不把 CPU 回归宣称为新增实机采集。
