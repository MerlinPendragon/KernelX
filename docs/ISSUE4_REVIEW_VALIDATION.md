# PR #10：审查修复与正式离线服务验收

对应 c855cba 上的三个审查意见。代码修复在
`74c80c4a2345a3ed26118b51a6525548dc7109b6`，另在
`41e53cad6eba7a2c7a9818f9f673b2da49a9da44` 修正干净服务环境下 CANN
set_env.sh 的 nounset 初始化顺序。应用包绑定后者，sequence 6，release ID：
`1352814729bbbb16f38aee86d92558752dc6e6ab1b652aa8ec08d4e4e065a5ac`。

## 修复与回归

| 审查问题 | 修复及验证 |
| --- | --- |
| 外层超时可能遗留 Runner 独立进程组 | 外层预算覆盖有效窗口及整个 plan 的准备、执行、导出和入库余量，SIGTERM grace 10 秒；失败时同步恢复所有 Runner 所有权、核查设备，无法确认则持久隔离，之后才回退/返回。Linux 回归实际启动两层 run_owned，内层忽略 SIGTERM，先确认外层 TIMEOUT 后内层仍活着，再确认同一 tick 已清理且 attempt INTERRUPTED/RELEASED；无真实 NPU 调用。 |
| 无可用版本和应用失败仍退出 0 | JSON 明确 runnable/exit_code。缺失首次源、必需 smoke 失败、应用配置/执行失败退出 1；授权窗口等待、正常空闲、更新拒绝但仍可用旧健康版退出 0。真实 CLI 回归覆盖这些区分；smoke 回退断言 exit_code=1。 |
| 稳定 bootstrap 错用旧 case manifest | 使用候选包经过签名及逐文件检查的 case manifest 校验 plan。回归只更新新包 semantic_version/case_key 与 plan.manifest_sha256，旧 bootstrap 可安装并调用真实候选 Agent 校验新计划；policy 禁用，保证不运行 NPU。 |

910B1 Python 3.9.9：**118 项全部通过**（78.646 秒）。开发机 113 通过、5 个
Linux 专用测试跳过；compileall、diff --check 和 launcher shell 语法检查通过。
CPU 故障回归与真实 NPU smoke 分开，未用 CPU fixture 冒称硬件故障。

## 正式入口、账号和断外网验收

使用提交的 `deploy/kernelx-bootstrap` 与 `kernelx-bootstrap.service`，实际
User/Group=kernelx，实际 ExecStart=/opt/kernelx/bootstrap/deploy/kernelx-bootstrap。
服务文件本身不替换用户或执行命令；在 /run 安装临时单元，测试用 drop-in
仅增加 PrivateNetwork=yes 和网络隔离检查 ExecStartPre。命名空间只有 lo，
对 1.1.1.1:443 的连接返回 Network is unreachable。没有依赖 SSH 进程环境、
在线 pip、外网源或 bash -lc；原包从本机离线仓库主动获取。

- 首次安装指向不存在的源：UPDATE_REJECTED、runnable=false、exit_code=1，systemd Result=exit-code / ExecMainStatus=1。
- 全新状态安装签名 sequence 6：HEALTHY，正式 launcher 内完成 device 5 smoke，20 次预热、10 次测量，中心入库 30 条有效观测，release/git commit 与实际包一致。
- 再次启动同一正式 service：CURRENT、runnable=true、exit_code=0；session 总数仍为 1，没有再次 smoke；upload pending=0 / acked=1。
- 实际 NPU 释放为 RELEASED，且 by_hard_cutoff=true。原日志保留宿主 npu-smi 部分固件查询的权限/驱动错误；对应探测 UNKNOWN，不用这些信息声明已验证其他设备/算子。

本轮使用用户已授权设备中的 device 5，创建 450 秒有限窗口。测试完停止并
移除临时 /run 单元、/opt/kernelx 和 /etc/kernelx，临时服务账号删除，设备锁
ACL 恢复原快照；/var/lib 测试数据移入隔离证据目录。随后独立核对账号、路径
及 ACL 确已恢复。没有 enable 长期 timer、重启共享服务器或修改 CANN/驱动/固件。

公开证据及复现脚本位于 `tests/fixtures/release_910b1/review/`。公开 service
journal 仅将 hostname 替换为 `<redacted-host>`；其他诊断及事件保留。私有
`artifacts/issue4/review-evidence.tar.gz` 保存未脱敏日志、SQLite、PROF、签名包、
冻结 policy/plan 和环境。最终证据目录与归档 SHA256 见下方实测摘要。

复现前按 RELEASE.md 从上述源码 commit、目标真实 environment 构建 sequence 6；
示例可信公钥的安装指纹仍为
`29f9a3945fcf3b850b078b8a3442937a3a2849f06b9aa2b82f72966d1adf7b06`，
生产须使用管理员审核的信任根。复现脚本拒绝覆盖已有 kernelx 用户和部署目录。
在 `/home/lxb/kernelx-issue4` 放置对应源码、repository、release.pub 和公开脚本，
人工约定新设备窗口后执行：

```sh
sudo -n /usr/bin/python3 formal-service-run.py \
  1352814729bbbb16f38aee86d92558752dc6e6ab1b652aa8ec08d4e4e065a5ac
```

本次实机范围仍是 910B1/CANN Add 单 worker 与本地中心。不同 CPU/SoC/CANN
兼容组必须分别构建、验证包；没有将这次结果扩展到其他型号、七个算子库或
七天无人交互运行。#3/#5/#6 对应剩余范围继续开放。

最终证据目录：`/home/lxb/kernelx-issue4/formal-20261004T105113Z`。
设备释放 UTC `2026-10-04T10:51:49.708033Z`（北京时间 18:51:49）。
两端私有归档 SHA256 一致：
`ba773c944ae014ffe60e447cfc1e1640899845eeb7807869d24fcdd500098fae`。
