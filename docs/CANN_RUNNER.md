# CANN 单机采集闭环（issue #2）

独立 Python ≥3.9 CLI + 已安装 CANN 的 C++ 编译器/API，运行不需要 AI agent、torch 或在线安装。首版固定 ACLNN Add：两路 4096 个 float32 全 1 输入，alpha=1，ND 连续布局；仅评测执行、采集完整性和性能，未做数值正确性验证。冻结清单位于 `kernelx/manifests/cann_add.json`。

## 运行

在人工允许的设备/时间范围内指定逻辑 device ID、注册 server UUID、授权 ID 和带时区的绝对窗口。以下日期/窗口需换成实际授权值，不能把空闲检测当成使用权：

```sh
. /usr/local/Ascend/ascend-toolkit/latest/set_env.sh
python3 -m kernelx collect-cann-add \
  --device 5 --server-id 55dcc47c-f8a8-4f3f-ab2d-c02bd385c470 \
  --authorization-id manual-test-001 \
  --window-start 2026-10-04T00:30:00+08:00 \
  --window-end 2026-10-04T00:35:00+08:00 \
  --output /home/lxb/kernelx-issue2/new-run --timeout 90
```

默认 **20 次预热、10 次测量**，`--warmup` / `--repeats` 可显式更改，每项限 1..1000。当前按用户要求固定 20 次；预热侧车保存每次单调 host 开始/结束与 host_elapsed_us，后续根据稳定性数据选择次数。本版不自动判断已达热稳定；这些 host 预热样本包含 workspace 准备/提交/同步，不等于 device kernel 延迟，不能直接与 profiler 下的 device duration 混合。

输出目录必须不存在，每次运行独立 case/profile/attempt；不覆盖、复用或合并旧结果。当前适配器只接受一 NPU/芯片 0 与逻辑 ID 直接映射，其他映射明确拒绝。设置 `ASCEND_RT_VISIBLE_DEVICES` 时（含空值、重排及可见设备子集），启动前直接拒绝；调用者需先确认直接设备 ID 后取消该变量。benchmark 使用已检查的环境快照，授权、profiling 和释放查询使用同一设备。多芯片卡和多 rank 是后续适配范围。

## 计时与归因

C++ 用例在 aclprofStart **之前**完成 ACL 初始化、输入分配/拷贝、全部预热、测量 executor/workspace 准备。使用 `ACL_PROF_TASK_TIME | ACL_PROF_ACL_API | ACL_PROF_MSPROFTX`（0x83）及 `ACL_AICORE_NONE`，固定为 latency-v1；不打开 AI Core 指标/pipeline 计数器。preset 内容、hash、编译命令/源文件/可执行文件 hash 全部保存，不静默更换配置。

每次测量以唯一 `kernelx:measure:N` 的 msproftx range 包围一次 Add 提交和 stream synchronize。aclprofStop/Finalize 在所有测量之后执行。官方说明要求 [msproftx API 位于 profiling start/stop 之间](https://www.hiascend.com/doc_center/source/en/canncommercial/850/API/appdevgapi/aclcppdevg_03_1264.html)；本实现还用目标机实际头文件和真实导出验证了这些边界。导出使用目标 msprof 的 `--export=on --output=<PROF>`，参见 [官方导出说明](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/920beta1/devaids/Profiling/atlasprofiling_16_0011.html)。不启动另一个从程序入口计时的 profiler，从而不会把编译/预热混入采集。

`msprof-add-v1` 解析器只接收已验证的单设备同步 Add 协议：

- CSV 需要指定列名和明确 `(us)` 单位，保存未知列/原始行；op_statistic 不用于原始迭代样本。
- CSV 和硬件 trace 按 stream/task ID、名称、时间戳/耗时交叉核对，不按文件行顺序分配迭代。trace 需明确 Ascend Hardware 层及目标 NPU 标签。
- 每个设备任务必须完整落入唯一、互不重叠、与侧车一致的测量范围；每个 invocation 必须恰好一个 Add task，目标 task 数必须等于声明 repeats。全部 20 次预热仍只应得到 10 个测量 task，不通过“删除前 N 行”排除预热。
- 使用 Decimal 处理导出的巨大微秒时间戳，CSV/trace 对照容差 0.001 us 仅覆盖目标导出的小数舍入。范围判断不使用额外容差或跨主机时钟相减。
- 缺 trace、未知单位、缺 task、归因歧义、部分 sidecar、profile 未停止或未释放，都输出无效状态，不创建有效 observation；保留原始 CSV/PROF 供离线诊断。

每个测量生成独立 task_duration_us、device_span_us、host_elapsed_us observation。对首个单 task Add，device span 与该 task duration 相等；不将此等价关系推广到融合、多 stream 或多 rank 用例，也不把设备 task duration 当端到端 host 延迟。

离线重放不占 NPU：

```sh
python3 -m kernelx parse-cann-add \
  --exports tests/fixtures/cann_add_warmup20/exports \
  --sidecar tests/fixtures/cann_add_warmup20/sidecar.jsonl \
  --device 5 --output /tmp/add-parsed.json
```

## 窗口、进程与设备释放

窗口前、软截止后均不启动任务；编译/环境探测后重新检查时间，保留至少 3 秒清理预算。preflight 前按稳定 `device_uid` 获取 `/tmp/kernelx-device-locks/` 下的非阻塞跨进程 flock，持有至实际释放检查和结果保存完成。同设备第二个 Runner 拒绝启动；锁文件不得删除或更换 inode。native 子进程继承锁描述符，父进程意外退出也不会在 native 存活时释放锁。不同账户无法访问同一锁目录时直接失败，不能通过另建锁目录绕开。

启动 benchmark 前要求 npu-smi proc-mem 明确无进程；权限不足、不识别输出或外部占用均拒绝。预约策略的持久化/撤销、重启恢复、outbox 属于 #3。

`run_owned` 使用 `start_new_session=True` 单独创建进程组，以单调时钟限制执行；超时或 SIGTERM/SIGINT 转成中断，先向本组发 SIGTERM，宽限后必要时 SIGKILL。Linux `/proc` 追踪同组非 zombie 进程：父进程退出但子进程仍活着也会清理。不会按进程名杀进程，不向外部组发信号，不调用 npu-smi reset。native `aclrtResetDevice` 仅释放本进程 ACL 设备上下文，不复位共享物理卡。

进程组退出与 NPU 释放分别记录。释放通过目标 npu-smi proc-mem 对本任务 PID 集检查；未知格式/权限错误不当成释放。保存实际检查 UTC、是否在硬截止前确认及单调资源账本；未知/残留会使任务失败，保留结果并供 #3 隔离报告。导出/解析/归档在资源释放后作为后台 CPU 工作执行，可在设备窗口之后完成。

## 环境与产物

保存 manifest/preset/preparation/authorization、plan/session/attempt/profile/observation、immutable environment、原始 PROF、export CSV/trace、sidecar、benchmark/export/build 日志和 artifact SHA256/bytes 索引。artifact:// URI 以 profile ID 和原 run 根目录内路径解析；terminal session/profile/observations 本身不放入 profile 引用的 artifact 列表，避免循环引用。构建源与二进制 hash 参与 release ID。

`dlsym + dladdr` 确认 aclnnAdd 的实际 host API 提供者，`/proc/self/maps` 记录全部实际加载的共享库路径与文件 SHA256（含自定义安装根及安装根之外的依赖）；采集后添加带来源 evidence 的运行提供者条目，随后冻结环境快照。指纹包含去重、排序后的全部映射库；缺失必要的 libopapi/libnnopbase/libmsprofiler/libascendcl、API 提供者未映射或库不可读取时为 UNVERIFIED_HOST_PROVIDER，不生成 verified-support。库指纹只证明观测到的 host 文件/提供者；设备 kernel 名称来自 profile，其二进制加载路径仍标为 DECLARED_ONLY，不冒称已验证 device kernel 制品。

CANN 的遗留 9.1.0 与 OPP 9.2.0-beta.1 声明冲突不被覆盖。用例成功只输出该固定 case 的 verified-support 结果，以实际 API/加载库指纹作为 revision/CANN key，不把遗留 Toolkit 声明当成实测运行版本。不外推到其余库、其他 SoC 或未知环境的严格比较。

原始 PROF 可能包含未脱敏机器信息，目标机保留完整文件，并在本地 `artifacts/issue2/` 保存原始归档（Git 忽略）；提交的 fixture 使用已脱敏环境和导出时间/task 数据。初次 3 次预热仅作为解析器边界的历史试采 fixture，正式交付依据是 `cann_add_warmup20`。

## 故障注入

测试专用环境变量 `KERNELX_TEST_PAUSE_AFTER_PROFILE_START=30` 在 profiler 已启动且预热完成后暂停，写明 FAULT_INJECTION 标记。它不属于正常 manifest，设置它的运行永不通过正常归因检查；生产运行应保持未设置。910B1 验收分别在此处触发 5 秒超时与 SIGTERM，检查本任务 NPU 释放、外部 sentinel 进程仍存活、0 有效观测。普通运行无此暂停。

```sh
python3 -m unittest discover -s tests -v
```

离线测试覆盖真实 CSV/trace 回放、行序变化、设备/任务身份、巨大时间戳小数精度、单位未知、缺失/重复任务、范围重叠、预热计数与协议关联；进程测试覆盖超时、外部进程存活、父退出后子清理、窗口拒绝和未知释放状态。
