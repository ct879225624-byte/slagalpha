# P9 研究与冻结契约

状态：P9.1 与 P9.2a 已完成；P9.2b DEV 规则门禁已解除，等待按请求补齐 1m/Funding
版本：0.2.0；日期：2026-09-10

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
DEV 的 16,440 个成员日可使用 `MEDIUM` confidence、来源与哈希完整的未验证规则，全部
记录 `APPROXIMATE_HISTORICAL_TICK_SIZE` warning；这些规则没有被改成 `VERIFIED`。
VALIDATION 与 LOCKED_TEST 的 8,220 / 8,220 个成员日仍严格阻断。没有执行参数回测或
锁定测试。

- split hash：`b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca`
- audit hash：`74e9a89210257478ba1b7ef89fd67ff3d6d12d87f6c44b6a4113369f30aab4f3`
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
- plan hash：`688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9`
- 重现命令：`.venv\Scripts\python.exe scripts\p9_sensitivity_plan.py`

DEV 历史规则 blocker 已解除，可以启动 P4–P6 扫描。规则取证与供应商资格审计保留为
可选质量提升，不再是个人 DEV 研究硬门槛；VALIDATION/LOCKED_TEST 没有放宽。

## P9.2b 输入准备补充

已实现 DEV 历史规则取证清单与单区间本地证据验收器，说明及重现命令见
`docs/p9-rule-evidence-intake.md`。清单按受阻成员日排序，未使用策略收益；验收器只
检查已提交材料，最高状态为 READY_FOR_REVIEW，仍为 UNVERIFIED，不能直接进入研究。

该 intake 现作为可选的规则质量提升入口保留。248 个 DEV 合约、16,440 个成员日已可在
不改变原注册表、不把规则伪装为 `VERIFIED` 的前提下使用 research-grade fallback；每次
使用都必须携带 confidence、来源引用和 warning。

DEV RunManifest、不可覆盖的结果保存和只读前检也已实现，见
`docs/p9-run-provenance.md`。本地环境精确版本锁与实际包版本核对通过；更高质量的规则
证据仍可后续补齐，但不再阻断 DEV。Git 基线验收见 `docs/git-baseline.md`。这些都是
P9.2 工程准备，尚未运行真实研究。

10 个计划候选现已有内容寻址的 DEV 参数版本，并绑定策略规则与敏感度计划，见
`docs/p9-parameter-version-contract.md`。这不代表选择了最优参数；正式执行器仍需验证全部
运行输入，并把 approximate 规则状态与价格取整影响写入每次运行记录。

正式 DEV 文件内容哈希门禁也已实现，当前真实报告验证 12 类必需工件和 1 份附加诊断，
仍缺少 1m、Funding，见 `docs/p9-dev-execution-inputs.md`。manifest 的业务完整性
和区间交叉引用门禁现也已实现；当前报告级硬 blocker 只剩 request-scoped 1m Candle 与
Funding。全时期 normalization 缺口仍作为诊断保留，实际扫描遇到受影响数据时继续拒绝，见
`docs/p9-dev-execution-semantics.md`。

Universe 引用的零条目 `empty-ledger/0.1.0` 现已保存为独立内容寻址工件并通过版本核对，
见 `docs/p9-exclusion-ledger-artifact.md`。这只是补齐既有输入溯源，没有新增排除规则。

现有本地环境的 29 个 wheel 工件也已按哈希冻结并完成离线 dry-run，见
`docs/p9-dependency-artifacts.md`。多周期缺口审计没有以有限窗口替代递归 ATR 依赖，
仍然失败关闭。2026-09-02 续接顺序见 `docs/development-checkpoint-2026-09-02.md`。
