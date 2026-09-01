# P9 研究与冻结契约

状态：P9.1 与 P9.2a 已完成；P9.2b 真实执行阻断  
版本：0.1.0；日期：2026-08-31

## 阶段与边界

1. P9.1：冻结全局时间切分，审计真实输入，不执行策略收益研究。
2. P9.2：默认参数为中心的 DEV 逐项敏感度计划与执行；禁止笛卡尔积搜索。
3. P9.3：VALIDATION 邻域稳定性筛选与版本冻结。
4. P9.4：一次性 LOCKED_TEST、Walk-forward、成本/置信区间与晋级报告。

研究未通过不得进入 V0.2。缺少输入应标记 `BLOCKED`，不是策略表现失败或
`INCONCLUSIVE`；后两者必须建立在真实、有效的研究结果之上。

## P9.1 冻结时间轴

全部区间均为 UTC 日期的左闭右开区间，以同一全局时间轴分割 1,096 天，不按
symbol 单独切分；成熟期不会移动边界。

| Role | 起始（含） | 结束（不含） | 天数 |
|---|---|---|---:|
| DEV | 2023-08-01 | 2025-01-30 | 548 |
| VALIDATION | 2025-01-30 | 2025-10-31 | 274 |
| LOCKED_TEST | 2025-10-31 | 2026-08-01 | 274 |

这是按实际天数精确 50%/25%/25% 分割，不是按 18/9/9 个自然月近似。
首版拒绝不能被 4 整除的研究日数，避免默默分配余数。

## 访问与运行约束

- 参数研究入口只允许 DEV；VALIDATION 只用于后续候选筛选。
- 基础 role guard 允许 DEV/VALIDATION，始终拒绝 LOCKED_TEST；不提供布尔开关绕过。
- 冻结参数和一次性审计尚未实现，不能执行 LOCKED_TEST。
- P9.1 可以审计三个区间的元数据与输入覆盖；这不读取策略收益、不消耗锁定测试。
- 输入审计要求精确覆盖每个日期，且日度版本序列哈希必须匹配 P8 双轮验收。
- 所有真实 Trade Plan/P7 case 仍须通过历史规则区间门控。
- 1m、Funding、Aggregate Trades 按已接受 replay 请求获取，不预取全历史。
- 正式晋级报告仍须满足 RunManifest 的非空 Git commit、干净工作区与完整输入哈希。

## 当前真实输入审计

1,096 个快照完整，三个区间的成员日分别为 16,440 / 8,220 / 8,220。
32,880 个成员日全部命中 `CONTRACT_RULE_UNVERIFIED`，可执行成员日为 0，三个 role
均为 `BLOCKED`。没有执行任何参数回测或锁定测试。

- split hash：`b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca`
- audit hash：`d58f1cc2adcad91ebbc33bc3864e1f78388b2c9c8f6adb83f1b88f5b3d99e864`
- 重现命令：`.venv\Scripts\python.exe scripts\p9_research_input_audit.py`

该审计是工程输入检查，不是可用于晋级的策略回测报告。

## P9.2a 默认敏感度计划

默认值保持 Pivot 2/2、MA 压缩阈值 0.75、Stop 缓冲 0.15 ATR、Entry TTL 4 根、
最大持仓 32 根。候选顺序固定：默认组，然后依次只改变 Pivot、压缩阈值、Stop
缓冲、TTL、最大持仓。总数为 1 + 1 + 2 + 2 + 2 + 2 = 10 组，而非全部组合。

每组保留原 P7 ZERO/BASELINE/STRESS 成本费率，共 30 个计划评估配置；这不是
30 笔交易或已完成的回测。SMA、时间周期职责和策略家族不进入搜索空间。

- 计划仅允许 DEV，拒绝 VALIDATION/LOCKED_TEST。
- split、audit、candidate、plan 在重新读取时校验内容哈希；修改 readiness 不能
  继续沿用旧审计哈希。
- `require_dev_execution_inputs` 只检查 DEV 前置输入，不是实际 replay 执行器，
  也不替代 RunManifest、1m/Funding 完整性或锁定测试的一次性授权。
- 计划不等于参数冻结，`strategy_executed=false`、`locked_test_consumed=false`。
- plan hash：`c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9`
- 重现命令：`.venv\Scripts\python.exe scripts\p9_sensitivity_plan.py`

真实 DEV 仍因 `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS` 阻断，不能启动
P9.2b 或越过本阶段执行验证集/锁定集。外部证据调查见
`docs/p9-historical-rule-evidence-gap.md`。

## P9.2b 输入准备补充

已实现 DEV 历史规则取证清单与单区间本地证据验收器，说明及重现命令见
`docs/p9-rule-evidence-intake.md`。清单按受阻成员日排序，未使用策略收益；验收器只
检查已提交材料，最高状态为 READY_FOR_REVIEW，仍为 UNVERIFIED，不能直接进入研究。

248 个 DEV 合约、16,440 个成员日继续受阻。当前快照外推 DEV 的真实反例被拒绝，
原注册表与 P9.1/P9.2a 哈希未变。此项属于输入准备，不改变 P9 完成 1/4 的口径。

DEV RunManifest、不可覆盖的结果保存和只读前检也已实现，见
`docs/p9-run-provenance.md`。本地环境精确版本锁与实际包版本核对通过；规则证据仍未
补齐，Git 基线验收见 `docs/git-baseline.md`。这些都是 P9.2 工程准备，尚未运行真实研究。
