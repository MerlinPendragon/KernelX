# 人工预约 Agent、恢复与中心导入（issue #3）

Agent 为独立 Python ≥3.9 程序，只使用标准库，固定接入已验证的 CANN Add
Runner。部署时由外部 timer 定期执行 `agent-tick`；本 issue 不安装常驻服务、
修改 systemd 或设置新的设备预约，离线发布和启动器属于 #4。

## 策略与计划

`configs/server-policy.example.json` 是**默认禁用**、有明确到期时间的示例。
人工确认 reservation_id、注册 server/device 身份、逻辑映射、时区、有效期、
星期、窗口、停采日期、清理预留、任务超时与 spool 容量后才可启用。
`configs/agent-plan.example.json` 冻结 manifest SHA256、preset、case、预热/重复
次数、pilot 总成本上界和预计产物大小。示例预算是保守声明，不是已测容量。
修改任何计划/策略字段都视为撤销当前冻结窗口，不在同一窗口重放任务。

计划不能扩大允许设备或并发，首版只接受 `max_workers=1`。窗口按 IANA
时区解释，时间戳及 SQLite 时间均存 UTC；过午夜窗口沿用开始日的 ID。
窗口 ID 绑定 server、人工预约 ID、开始日期和 schedule ID，独立于 revision，
防止修改版本后在同日重跑。相交窗口和 DST 歧义/不存在的边界明确拒绝。
过期、停采日、错过的窗口均不补跑；不同日期产生新的窗口和观测 ID。

```sh
python3 -m kernelx agent-tick \
  --state /var/lib/kernelx \
  --policy /etc/kernelx/server-policy.json \
  --plan /etc/kernelx/agent-plan.json
python3 -m kernelx agent-status --state /var/lib/kernelx --output /tmp/status.json
```

CLI 读取文件配置。临时写入新 JSON 后原子 rename，可撤销正在执行的配置。
Runner 在准备/启动边界及 benchmark 监管循环检查配置；撤销停止当前本任务
进程组并检查设备释放，随后不下发其他 case。无效配置同样按撤销处理。
已有外部占用不终止其进程，仅拒绝采集。`ASCEND_RT_VISIBLE_DEVICES` 仍在
启动前拒绝，注册 device_uid 与实际逻辑 ID 必须一致。

只有剩余约定时间严格大于 task 预算加清理预留才启动。预算取声明的 pilot
总成本上界与至少 5 条同计划任务历史 p95 × 1.2 的较大值，不自动降低上界。
成本包括准备、编译、采集及本地后处理的墙钟耗时；原始 session 的单调资源
账本另外保存，不能用总成本替代实际 NPU 小时。任务自身由 Runner 使用
单调时钟超时，至软截止结束采集，并在硬截止前检查实际释放。

## 持久状态与恢复

`state.db` 使用 SQLite WAL + synchronous=FULL；独立记录 windows、tasks、
attempts、outbox、devices 和状态事件。单 Agent state 的跨进程 flock 防止
重复 tick；Runner 另按稳定芯片身份互斥，覆盖不同 Agent state 的争用。

完整 case 校验协议实体、引用、质量、产物 SHA256/bytes 和每个原始观测，
全部文件与目录 fsync 后发布 `sealed.json`；之后才在同一数据库事务登记
SUCCEEDED 与 PENDING outbox。`spool/<agent-attempt>/context.json` 保存冻结
策略/计划，成功 bundle 内也保存 agent-context.json。

启动扫描 RUNNING case：已完成但登记前崩溃的产物重新校验并登记；其余
标记 INTERRUPTED、保留部分数据，不将部分采集算成功，也不自动重试该
窗口中的同一 task。下次 tick 从该窗口下一个尚未完成 task 继续。

Runner 在 benchmark 启动时及运行中持久保存进程组、PID、Linux boot ID 与 start-time
证据。重启仅终止仍有匹配身份的本任务组；PID/boot 已变化或证据不足时不发信号。
进程终止后仍单独查询 NPU，不能把 CPU 退出当作设备释放。未知/残留设备
持久隔离；不会复位共享 NPU。人工处置后可显式解除隔离，CLI 先重新校验
身份、设备锁和空闲证据：

```sh
python3 -m kernelx agent-clear-device --state /var/lib/kernelx \
  --policy /etc/kernelx/server-policy.json --plan /etc/kernelx/agent-plan.json \
  --device-uid '<registered-device-uid>'
```

SIGKILL 到进程所有权证据发布之间的极短启动区间可能缺乏可验证身份；该
情况下恢复拒绝猜测所有权，未知释放时隔离并报告，不能承诺强制回收。
原始文件、窗口/attempt 事件、资源账本和 artifact 索引均保留供审计。

## 回传与真实数据库样例

上传独立于采集任务状态，失败不产生新 observation，不重新占用 NPU。
至少一次传输使用 bundle 内容哈希作为幂等键；中心端按 observation_id 和
各协议实体 ID 去重，同 ID 内容不一致则拒绝整个导入事务。

本地文件系统中心是可执行参考实现，包含 `center.db` 和完整原始 bundle：

```sh
python3 -m kernelx agent-tick --state /var/lib/kernelx \
  --policy /etc/kernelx/server-policy.json --plan /etc/kernelx/agent-plan.json \
  --center-dir /srv/kernelx-center
# 也可独立导入已封包 case：
python3 -m kernelx ingest-bundle --center-dir /srv/kernelx-center \
  --bundle /var/lib/kernelx/spool/<agent-attempt>/run
```

中心 SQLite 的 `observations` 保存 observation_id、内容哈希和完整协议 JSON；
`entities(kind, identity, sha256, payload)` 保存 case、environment、plan、session、attempt、
profile、artifact。environment JSON 中逐库保存版本、Git commit 和来源，查询
时通过 observation.environment_id 关联，不能只保留 Toolkit 版本。
`center-entry` 将实际已入库的 observation 与 case/环境关联展开，不改变底层存储：

```sh
python3 -m kernelx center-entry --center-dir /srv/kernelx-center \
  --output /tmp/database-row.json
```

`bundle_profiles` 关联 profile 与内容寻址 bundle，`artifact_path` 按该索引定位并校验原始文件。`imports` 保存持久 receipt；中心原始文件先 fsync/原子发布再提交元数据。
ACK 丢失时重传同一 bundle，中心结果不增加重复观测。

生产 HTTPS endpoint 的接入协议为 `POST application/x-tar`，传输已校验文件
和 sealed.json，附 `X-KernelX-Bundle-ID` 与 `X-KernelX-Manifest-SHA256`。文件
从磁盘流式发送。凭据仅通过环境变量名称引用，不放入 policy/plan：

```sh
python3 -m kernelx agent-tick --state /var/lib/kernelx \
  --policy /etc/kernelx/server-policy.json --plan /etc/kernelx/agent-plan.json \
  --upload-url https://center.example/v1/bundles --token-env KERNELX_UPLOAD_TOKEN
```

HTTPS 服务端部署、认证和对象存储配置需由部署环境提供；本 PR 交付 uploader
和可执行本地中心导入器，不公开网络服务、不发放凭据。服务端必须拒绝路径
穿越、链接与损坏文件，并在原始制品和中心数据库均持久提交后返回：

```json
{
  "bundle_id": "<sealed-bundle-content-hash>",
  "manifest_sha256": "<sealed-manifest-canonical-hash>",
  "durable": true,
  "observations": 30
}
```

Agent 校验 ACK 的 ID、manifest hash、durable 和观测数量后才登记 ACKED、
清理本地 case/上传归档。确认后清理前崩溃，重启完成清理。失败采用 5 秒起、
最多 3600 秒的指数退避；spool 字节水位/剩余磁盘不足时窗口保持 DRAINING，暂停新任务；空间恢复且原窗口仍有效时可继续，过期不补跑。保留
未确认数据及失败 case。孤立或失败数据的手动保留期限需运维决定，不自动
删除证据以换取机时。

验证：`python3 -m unittest discover -s tests -q`。测试覆盖 60/90/120 分钟、
过午夜、DST 拒绝、停采日/过期/错过窗口、重复触发、撤销、占用、预算、
缓存满、登记前后崩溃、不完整恢复、未知释放隔离、重复上传/丢失 ACK/恢复、
确认后清理与 PID 复用保护。测试 raw archive 为显式合成 fixture；真实 NPU
验收记录另见 `ISSUE3_910B1_VALIDATION.md`。
