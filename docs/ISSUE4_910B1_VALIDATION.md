# Issue #4：910B1 签名发布与启动验收

本页保留最初交付的实机证据。PR #10 审查后的最终 118 项回归与断外网
正式 launcher/service 验收见 [ISSUE4_REVIEW_VALIDATION.md](ISSUE4_REVIEW_VALIDATION.md)。

2026-10-04，用户授权当前设备测试；本轮只使用 device 5，20 次预热、10 次测量。
300 秒有限预约、90 秒 task timeout、3 秒 cleanup reserve。源码隔离目录为
`/home/lxb/kernelx-issue4`，没有修改共享 CANN/驱动/固件、启用长期 timer 或重启
共享服务器。

## 发布与安装

实际包的源码 commit 为 `4dd4e5be7edb9a74c1e7383f93b68598a237a440`，由开发机的
干净 tracked kernelx 源码构建；目标为真实 aarch64 / Ascend 910B / 910B1，以及
目标机 OPP、msprof 和四个核心 CANN 主机库的精确 SHA256。Python 依赖为标准库，
没有 pip 安装。签名端 OpenSSL 3.6.3、目标端 OpenSSL 1.1.1wa；RSA-PSS 验证通过。
发布私钥保留在发布端临时受限目录，没有传到目标机或提交仓库。

| 验收 | 实际结果 |
| --- | --- |
| 离线主动获取 sequence 3 | 签名、哈希、兼容/空间、自检通过，原子切换 HEALTHY |
| HTTPS 主动获取 sequence 4 | 真实 loopback TLS feed、指定受信 CA，下载/验证并完成 NPU smoke，提交 HEALTHY |
| 重复 tick | CURRENT；session 总数仍为 1，没有第二次 NPU smoke |
| 中心入库 | 30 条有效 observation，数据库展开 release ID 与真实 source commit |
| 设备释放 | RELEASED，UTC `2026-10-04T09:58:48.271551Z`（北京时间 17:58:48） |
| systemd 启动入口 | 有限 transient oneshot `kernelx-issue4-acceptance-095906`，以 lxb 执行，exit 0 |
| service/timer 模板 | 替换测试用户及测试入口路径后 systemd-analyze verify exit 0 |

sequence 3：`f4b864164243cba4f220d59806d6c87b50304585a6b19d6e385565df0206edef`。
sequence 4：`819682511cb6e4aabe33f0f8d194e7ae8e5c086ac1a50faf3503606bd6bc02d5`。
演示可信公钥 PEM SHA256：
`29f9a3945fcf3b850b078b8a3442937a3a2849f06b9aa2b82f72966d1adf7b06`。
这是验收演示信任根，不应未经生产管理员核对就用作生产发布密钥。

真实证据目录：`/home/lxb/kernelx-issue4/acceptance-20261004T095758Z`。
成功 bundle 在其 `center/bundles/`；`database-row.json` 从真实 center.db 查询，
`release` 对应已验证的 sequence 4，软件仍保留每个算子库版本/commit/来源。
TLS 服务只绑定 127.0.0.1，测试后关闭；不是已部署的公网发布服务。

systemd 测试使用 CPU-only 配置，主动拉取旧 sequence 被底线拒绝而保留当前
健康版，服务执行成功；此前真实 NPU smoke 由同一 bootstrap session 入口执行。
verify 的宿主 ksecurec 权限/legacy dbus 路径警告保留在日志，退出码为 0。
只验证入口、模板和实际服务执行，没有重启服务器或宣称连续 7 天无人交互。
生产 enable/开机命令见 [RELEASE.md](RELEASE.md)。

## 最终代码与故障验证

910B1 Python 3.9.9：114 项 unittest 全部通过。本机 110 通过、4 项 Linux 专用
跳过；compileall 与 diff --check 通过。故障为受控 CPU fixture，不冒称真实
NPU 故障：正常升级、错误签名、损坏包、CPU/BIN/指纹不兼容、磁盘不足、CPU
健康协议失败、模拟 NPU smoke 失败、staging/switch/commit 崩溃、签名旧版本
重放、安装漂移、当前 session 的 release/plan 固定与更新互斥、更新失败后旧
应用继续运行、空闲心跳与采集成功/失败/上传状态分离均通过。

新增跨 release 隔离回归使用显式 Runner guard：未知释放的旧资源账本仍阻断
新候选，产生零 Runner attempt，失败回退；即使回归也不能在 unittest 中意外
启动真实 NPU。另有回归验证：损坏新 policy 时旧 outbox 仍能导入中心并
记为 ACKED，不启动新采集。最终包的真实测量为上述有限窗口的 30 条观测。

## 复现入口

验收脚本和模板验证脚本也保留在同一公开 fixture 目录。先按 RELEASE.md
构建本文 sequence 3/4 签名包，把仓库复制到
`/home/lxb/kernelx-issue4/repository`、核对公钥后复制到
`/home/lxb/kernelx-issue4/kernelx-issue4-release.pub`。隔离目录放置对应源码和
以下两个脚本；脚本使用本文注册 server/device 身份，执行前需人工约定新有限
窗口。脚本仅建立 300 秒窗口，退出时关闭 loopback TLS，systemd 为有限 oneshot。

```sh
cd /home/lxb/kernelx-issue4
. /usr/local/Ascend/ascend-toolkit/set_env.sh
python3 -B acceptance-run.py > acceptance.log 2>&1
python3 -B verify-units.py
python3 -B -m unittest discover -s tests -v > tests.log 2>&1
```

公开验收摘要、实际数据库行、service/verify 日志与最终 tests.log 位于
`tests/fixtures/release_910b1/`。完整 SQLite/PROF、签名包、冻结配置和 session
环境/计划/所有权日志封存在 Git 忽略的私有 `artifacts/issue4/acceptance-evidence.tar.gz`，
TLS 私钥不包含在归档中。

两端私有原始归档 SHA256 一致：
`0f01f381f69c61a0d344d0d19085d47dfe8209784fbd584b1f786a1577100581`。
