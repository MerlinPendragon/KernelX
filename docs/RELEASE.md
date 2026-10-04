# 签名离线发布与稳定启动器（issue #4）

交付标准库应用源码包、RSA-PSS/SHA256 签名清单、稳定 bootstrap、systemd
入口及持久回退。启动器不在线安装 pip，不更新驱动/固件/共享 CANN。当前
应用接口是已有单 worker CANN Add Agent，本地中心是 #3 的持久导入参考实现；
#3 全库/生产跨机控制服务仍保持开放。

## 发布

目标机先在相同 Ascend 进程环境下只读探测，取得真实 CPU/SoC/BIN 和指纹：

```sh
. /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -m kernelx probe --server-id '<registered-UUID>' --output /tmp/environment.json
```

发布端使用干净、已提交的 `kernelx/` 源码；不能把开发机 venv 打进目标包。
本版 Python 依赖锁为 Python ≥3.9、标准库、零 pip 包；签名验证依赖宿主
OpenSSL ≥1.1.1。目标 CANN 与 profiler 是已有宿主依赖，兼容组冻结
CPU 架构、SoC/BIN、msprof 可执行文件、OPP version.info、libopapi、libnnopbase、
libascendcl、libmsprofiler 的 SHA256；未知字段拒绝发布/安装。这些是部署前
文件指纹，实际加载 provider 仍由 Runner 单独验证并记录，不能冒称已加载。
Native Add 在本机编译，要求现有 C++17 编译器和匹配 CANN 头文件；不随应用
安装编译器。NPU smoke 会验证 native 编译、执行、采集、解析及释放。

```sh
# 演示生成发布密钥；生产密钥应由发布端保管，不传到目标服务器。
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /private/signing.pem
openssl pkey -in /private/signing.pem -pubout -out release.pub
python3 -m kernelx release-build --source /path/to/clean/KernelX \
  --repository /srv/kernelx-repository --environment /private/environment.json \
  --device-uid '<registered-chip-UID>' --key /private/signing.pem --sequence 1
```

每个 `<release_id>/` 包含 manifest.json、manifest.sig、payload.tar.gz。ID 是
规范化清单的内容哈希；签名覆盖原始完整清单字节，清单包含 payload SHA/字节数、
解包总大小、每个文件哈希、git commit、协议/schema、case 清单哈希与依赖锁。
源码 tar 规范化 owner/mode/mtime，排除 pyc，支持从开发机打包到 aarch64 目标机。

同一兼容组发布通道的 sequence 是人工维护的递增正整数。健康版本提交后
持久保存 sequence 底线；旧签名也不能通过 feed 重放为新部署。同 sequence
不同 release ID 拒绝。内部失败回退仍可恢复上一健康版，不降低发布底线。

离线源的 latest.json 为 `{"release_id":"<64位哈希>"}`。HTTPS 源的 feed 为
`{"release_url":"https://releases.example/<release_id>"}`，其目录提供三个文件。
发现指针本身不授予信任，实际清单必须通过预置可信公钥验证。HTTPS 验证证书，
拒绝 HTTP、带明文凭据 URL 和降级重定向；私有 CA 可用 config.ca_file。
公钥必须经管理员可信渠道核对指纹并安装，不能信任 feed 自带的公钥。

## 安装与开机入口

初始 bootstrap 从管理员已审核的源码版本离线复制，建立初始信任；稳定目录
与应用 current 独立，应用更新不会自更新 bootstrap。配置和公钥由管理员维护。
下例 `kernelx` 服务账号应由管理员创建，并授予已约定 NPU 的现有访问权限：

```sh
id kernelx || sudo useradd --system --user-group --home-dir /var/lib/kernelx-bootstrap --shell /usr/sbin/nologin kernelx
sudo install -d /opt/kernelx/bootstrap /etc/kernelx /var/lib/kernelx-bootstrap /var/lib/kernelx-center
sudo cp -a kernelx deploy /opt/kernelx/bootstrap/
sudo install -m 0644 release.pub /etc/kernelx/release.pub
sudo cp configs/bootstrap.example.json /etc/kernelx/bootstrap.json
sudo cp configs/server-policy.example.json /etc/kernelx/server-policy.json
sudo cp configs/agent-plan.example.json /etc/kernelx/agent-plan.json
sudo chown -R root:root /opt/kernelx/bootstrap /etc/kernelx
sudo chown -R kernelx:kernelx /var/lib/kernelx-bootstrap /var/lib/kernelx-center
# 仅影响该服务进程的 Ascend 环境，不修改系统全局 profile：
printf '%s\n' '. /usr/local/Ascend/ascend-toolkit/set_env.sh' | sudo tee /etc/kernelx/ascend-env.sh
sudo cp deploy/systemd/kernelx-bootstrap.{service,timer} /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/kernelx-bootstrap.{service,timer}
sudo systemctl daemon-reload
```

先核对 server/device 身份、source、公钥、中心路径、实际设备权限和有限人工
预约。示例 run_agent=false，policy 默认禁用；smoke=true 会等待有足够预算的
授权窗口。需要连续执行时人工设 run_agent=true 并启用已约定 policy，再部署：

```sh
sudo systemctl enable --now kernelx-bootstrap.timer
sudo systemctl start kernelx-bootstrap.service
journalctl -u kernelx-bootstrap.service
```

timer 在开机 2 分钟后第一次执行，此后在上次服务结束 5 分钟后主动拉取。
服务一次 tick 内更新与应用执行串行，systemd 不重叠同一 oneshot；文件锁同时
防止其他进程并行更新。实际发行单位负责 HTTPS/对象存储发布和访问控制。
本次验收只启动有限 transient service，没有启用长期 timer、重启共享服务器，
也没有用一次服务启动宣称完成连续 7 天运行。

也可离线手动执行相同入口（无需 Codex 或 SSH 调度器）：

```sh
cd /opt/kernelx/bootstrap
python3 -B -m kernelx bootstrap-tick --config /etc/kernelx/bootstrap.json
python3 -B -m kernelx bootstrap-status --config /etc/kernelx/bootstrap.json
```

## 切换、session 固定与故障恢复

流程为主动读取 feed → 验证可信签名/协议/兼容组/容量 → 有界下载 → 哈希校验 →
严格解包 staging → fsync/原子发布不可变 releases/<id> → CPU self-test → 在人工
有效窗口做 NPU smoke → 提交 HEALTHY。current 原子切换，并有持久 transition
日志；在 health commit 前崩溃恢复旧指针并隔离候选，提交后崩溃保留新版本。
签名/兼容/空间拒绝不影响旧健康版，smoke 失败恢复旧版；坏版本持久隔离，不
循环重装。原始失败证据保留；修复后发布新的 sequence/ID。

每个 session 复制 plan 和 policy 快照、保存预检 environment，并绑定 release
绝对路径、git commit 和 manifest。子进程 cwd/PYTHONPATH 固定为该 release，
不会跟随运行中 current 变化。稳定 bootstrap 从已签名且逐文件校验的候选
case manifest 校验 plan；case 升级不依赖 bootstrap 自身旧 Adapter 的 manifest。
活跃 session 继承启动器锁，更新等待其结束。
实际 policy 文件仍用于撤销检查，不能因复制 policy 而屏蔽人工撤销；plan 更新
只影响下一 session。Runner 保存自身实际环境，release/git commit 写入环境
extensions.release，center-entry 展开 release 关联。

smoke 复用真实 Agent 的 budget、预热 20 次、重复策略、软/硬截止、设备锁与
释放检查，产物正常入库并计入资源账本，不是窗口外偷偷执行的 CPU self-test。
smoke 与日常 main 使用不同任务状态，但共享一个资源隔离账本和累计 spool
水位；更新不能绕过上个版本的 UNKNOWN/RESIDUAL。启动恢复分别检查 Python
launcher 与 native benchmark 的 PID/boot/start-time 所有权，不结束外部进程，
不能证明 NPU 释放则隔离；不会重置共享 NPU。外层预算包含整个有效窗口和
全部任务的准备/导出/入库余量，不以单任务 timeout 限制整个 session。外层失败
同步恢复 Runner 的独立进程组并核查设备，完成恢复或持久隔离后才回退/返回，
无需等下一 tick；外层 SIGTERM grace 为 10 秒，长于原生进程的 2 秒。

人工处置后使用相同共享资源接口解除设备隔离：

```sh
python3 -B -m kernelx bootstrap-clear-device --config /etc/kernelx/bootstrap.json \
  --device-uid '<registered-chip-UID>'
```

该操作持有启动器锁，先恢复旧任务，再核查身份/映射、设备锁和实际空闲证据。
保留旧 releases、session 日志、失败 spool；不自动删除未确认数据以腾出机时。
包大小与磁盘检查包含压缩包、解包体积和运维 reserve，提取拒绝链接、路径穿越、
重复/未索引/大小不符文件，应用启动前再验证安装内容，运行时禁止生成 pyc。

bootstrap-status 分别返回 heartbeat、last_success、last_failure、upload 积压、
release 状态、session 结果和事件。空闲 tick 只更新心跳；不能把它计为成功采集。
smoke/采集失败保留自己的状态，不被“服务进程仍活着”掩盖。CLI 输出
runnable/exit_code：首次无可用版、必需 smoke 失败、应用失败退出 1；授权窗口
等待、正常空闲和更新拒绝但旧健康版仍可运行退出 0。启动恢复先导入所有
旧 main/smoke outbox，再验证新的采集配置；损坏或失效的 policy/plan 不阻止
已完成结果回传。upload 按服务器所有角色的持久队列统计 ACKED 与 PENDING。

故障测试与 910B1 实测见 [ISSUE4_910B1_VALIDATION.md](ISSUE4_910B1_VALIDATION.md)。

PR #10 审查修复及断外网正式服务验收见 [ISSUE4_REVIEW_VALIDATION.md](ISSUE4_REVIEW_VALIDATION.md)。
