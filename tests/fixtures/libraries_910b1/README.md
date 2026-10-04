# 910B1 issues #5/#6 evidence

2026-10-04 的真实 device 5 采集：三个有效独立窗口、一个保留的缓存校验失败窗口，
20 次预热 / 10 次测量，90 条观测、3 个已导入 bundle。三个有效窗口只有一个日期。
`report.json` 包含输入 SHA、实际资源账本、全局分派、中心收据、失败损耗、容量与稳定性。
`database-row.json` 是中心数据库实际 join 样例；`target-tests.log` 是目标机测试原始日志。

`pilot-inventory.json` 保留试采时的冻结目录快照；`inventory-final.json` 是交付版本重新只读
计算的矩阵（包含加强的依赖、完整输出和 dirty-tree 绑定）。二者不能当作同一个清单 hash。
试采只运行 CANN seed，其他组合均过滤，未产生四个扩展库的伪实机观测。

三个 pilot Python 脚本与 report 脚本保留当时的操作过程；第一份 retry 脚本已记录读取
ACK 后本地 spool 的错误，最后一份改为读取持久化 center bundle。不要在 CI 直接运行这些
实机脚本，也不要复用已过期窗口；复采必须新建有限授权窗口与 submission_id。

完整原始 profile、sealed bundles、SQLite 数据库和源码归档保存在本次工作区
`artifacts/issues56/acceptance-evidence.tar.gz`，并同步提交到本目录的 `acceptance-evidence.tar.gz`，校验值见 `acceptance.json`。归档未包含凭据。
跨机部署和并行效率没有实机证据，仍为 #3 集成及后续适用资源组验收范围。
