# Issue #1：910B1 验收

测试日期：2026-10-03（Asia/Shanghai）。fixture 采集时间：23:30:52，北京时间；JSON 内保存 UTC `2026-10-03T15:30:52.055008Z`。测试目录 `/home/lxb/kernelx-issue1`，注册 server UUID `55dcc47c-f8a8-4f3f-ab2d-c02bd385c470`，SSH alias `910B1`。原始身份字段已脱敏。

## 已验证

- 目标机 Python 3.9.9 上 15 项协议/探测测试全部通过，schema 校验通过，`compileall` 通过。本地 Python 3.14.6 同样通过。
- 8 个 NPU，均为一个 accelerator chip（chip 0）和一个 MCU（chip 1，逻辑 ID 为 `-`）；MCU 不进入设备 inventory。
- mapping 输出 `Ascend 910B1`，board 输出 `Chip Type: Ascend`、`Chip Name: 910B1`；两路证据确认完整 BIN `Ascend 910B1`，映射表版本 `910b1-observed-v1`。
- 每个 chip HBM 65536 MB；npu-smi 与驱动版本均为 25.5.0。取得 VDie ID，用服务器作用域伪名构造 device UID；物理 card UID 仅有位置置信度。
- 取得 msprof help 和可执行制品指纹（SHA256 `75541a2535f1dc9b39484b833b0454a002d0eabd68d7673006f52cb00e73dc82`）；不把 help 中 flags 当作采集 preset 已验收。

| NPU / logic ID | accelerator chip | PCIe 位置 |
| --- | --- | --- |
| 0 / 0 | 0 | 0000:C1:00.0 |
| 1 / 1 | 0 | 0000:01:00.0 |
| 2 / 2 | 0 | 0000:C2:00.0 |
| 3 / 3 | 0 | 0000:02:00.0 |
| 4 / 4 | 0 | 0000:81:00.0 |
| 5 / 5 | 0 | 0000:41:00.0 |
| 6 / 6 | 0 | 0000:82:00.0 |
| 7 / 7 | 0 | 0000:42:00.0 |

## 未知与限制

- `latest` 解析为 `/usr/local/Ascend/cann-9.2.0-beta.1`。遗留 Toolkit 安装文件声称 9.1.0；OPP `version.info` 声称 9.2.0-beta.1。两个值均标为 DECLARED_ONLY，记录版本冲突；未猜测或覆盖真实 runtime Toolkit 版本。
- `/usr/local/Ascend/firmware/version.info` 返回 Permission denied；board 的 Firmware Version 为 NA。快照记录 PERMISSION_DENIED/UNKNOWN。严格同组比较拒绝该快照。
- msprof `--version` 返回 unrecognized option，记录 UNSUPPORTED；不把 Toolkit 版本当作 msprof 版本。没有加载 CANN 环境时还曾出现 libascendalog.so 缺失，因此复现命令显式加载现有 `set_env.sh`。
- 该 Python 3.9.9 解释器未安装 torch、torch_npu 或其余适配库的 distribution metadata；它们标为 NOT_FOUND，范围仅限该解释器。不会推断其他虚拟环境、容器或原生源码库也不存在。
- 6 类库支持矩阵均为 UNVERIFIED；本 issue 仅交付真实库存与版本来源证据，benchmark/preset 验证属于 #2 / #5。
- 原始 `npu-smi info` 显示部分卡已有 vLLM/其他进程。本次只读探测及 CPU 协议测试，不启动 NPU benchmark，不改共享软件/驱动/固件，不终止其他任务。

## 复现

将仓库放入目标机隔离目录，在实际 runner shell / Python 环境中执行：

```sh
ssh 910B1
cd /home/lxb/kernelx-issue1
. /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
python3 -m kernelx probe \
  --server-id 55dcc47c-f8a8-4f3f-ab2d-c02bd385c470 \
  --output environment.json
python3 -m kernelx validate environment environment.json
python3 -m unittest discover -s tests -v
python3 -m compileall -q kernelx
```

提交的 fixture 在 `tests/fixtures/910b1/environment.json`；所有读命令、退出码、脱敏 stdout/stderr、权限与解析状态以及 SHA256 在其 evidence 数组中，可按 Fact.source 回查。对其他目标机更换注册 UUID，新增实机输出 fixture 和版本化 BIN 映射，不能直接沿用本机判定。

最终目标机测试日志：`tests/fixtures/910b1/validation-tests.log`（15 tests，0.392s，OK）。测试传输包在本地与目标机的 SHA256 均为 `3039fcd50ba4415abb9d7a8e60a834dc86bbec20122a98764635ef1a063750ac`；该包包含测试时源码，不包含后来补入的本段日志说明。

测试覆盖：对象键顺序稳定及关键参数变化、版本/required/类型拒绝、非有限值、质量/完整性、证据篡改/悬空引用、未知元数据拒绝分组、MCU 排除、完整 board/mapping 回放、未知规格拒绝猜测、超时/权限/缺命令/错误格式、身份脱敏及跨服务器作用域。

## PR #7 评论修复验收（2026-10-04）

解析器更新为 `ascend-probe-v2`。保留上面的初次 v1 fixture/日志，新增 `tests/fixtures/910b1/environment-review.json`、`review-tests.log` 与 `review-identity-check.json`，避免修改历史证据。

- 型号识别：按固定版本的 Ascend 官方型号定义增加 910B2、910B2C、910B3、910B4 白名单（来源见 PROTOCOL.md）。回归用例将 910B1 board/mapping 名称一致替换，检查完整 BIN 与支持矩阵型号；未知 910B99、冲突仍降级。新增型号仅完成模拟回放，没有其他型号真机验收，库支持仍为 UNVERIFIED。
- 版本脱敏：保留 key/value、带引号版本声明、JSON package version 和 npu-smi/msprof 版本头中的四段版本；IP 地址仍脱敏。8.0.0.1 与 8.0.0.2 在快照和比较分组中保持不同。
- 稳定身份：统一原始 Die ID 到服务器作用域伪名的计算；默认脱敏、未脱敏展示、脱敏证据回放生成相同 device_uid。零值/NA/UNKNOWN 的位置降级也不受开关影响。原有默认脱敏 v1 UID 不变；旧未脱敏 UID 的迁移规则见 PROTOCOL.md。
- 910B1 Python 3.9.9 和本地 Python 3.14.6 均通过 20 项测试，目标机 compileall 通过。
- 在 910B1 普通 shell 直接运行 `python3 -m kernelx probe`、`validate`、`case-key` 均成功，执行过程不需 AI agent 或在线服务。新快照采集于北京时间 00:00:21，仍发现 8 个 910B1。
- 实机比较两种展示模式：8 个芯片 device_uid 和 identity_confidence 完全一致，结论见 `review-identity-check.json`。临时未脱敏快照只在目标机用于比较，随后删除，未回传或提交。
