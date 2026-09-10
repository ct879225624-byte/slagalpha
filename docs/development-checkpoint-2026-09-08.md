# 开发续接记录：2026-09-08

续接提交 `2aa61a9`，第 23 项已提交为 `4716682`。第 24 项开始时确认工作区干净、
HEAD 为 `4716682`；第 22、23 项均不重复执行。本轮继续 P9 真实输入解除阻断；研究、
下载和交易授权保持关闭。

## 第 23 项：真实输入与首个可解决缺口复核（完成）

冻结环境的只读 preflight 通过 Git、提交和环境检查，报告
`7d478d73372cb63610adbdd102b27f1b7814eb1ac6b982c5279b48ec8b669f81`；唯一前置阻断仍为
`NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`。语义报告保持
`095a2f2e7d33e2e5247a5656a919c9fec15b38daa4e86e320331b0dd4f9f20fd`：11 类通过，
多周期 Candle、1m Candle、Funding 三类 deferred，18 个 ATR 递归历史依赖未解决。

### 规则证据

- 规则注册表 649 条全部为 `UNVERIFIED`；DEV 的 548 天、16,440 个 member-days、
  248 个 symbol、2,564 个连续缺口窗口均因规则未验证而阻断，0 eligible。
- 248 个 DEV symbol 均有 identity/lifecycle，但规则草案只来自 2026-08-28 当前
  exchangeInfo；仓库没有可直接升级任何 DEV member-day 的历史 filters 或连续性材料。
- 官方公告确认 BTC/ETH minimum notional 在 DEV 内变更，且 BTC 当前值来自 2026 年
  的再次调整；这强化了禁止回填当前快照的结论。公告检索没有生成或批准历史证据。
- 已有 intake 入口最高只到 `READY_FOR_REVIEW`；真实材料到达后仍缺显式人工复核后的
  非重叠 registry promotion，但在没有材料时不先把合成流程冒充解锁。

### 多周期与市场输入

- 27 个失败文件仍来自 9 个 symbol-month × 15m/1h/4h；直接和 185 根有限回看重叠均为 0。
- 18 个递归阻断属于 AIA/CVC/CVX/LIT/MAVIA/PUMP 的三个周期，共 411 个文件—入选日组合。
- 本地复核发现 AIA、CTK、CVC、CVX、LIT、MAVIA、PUMP、SLP 的断层右边界与已验证的
  重上线生命周期起点一致，并存在对应 `*SETTLED` 原始及规范化旧生命周期文件。
  这些真实本地材料足以进入独立的生命周期边界审计，但尚不能批准 ATR reset。
- AERGO 没有相同 identity 材料；1d 可能跨旧/新生命周期，也必须纳入审计。
- 仓库没有真实 accepted request set、1m/Funding 响应或聚合工件；这些数据必须等待
  合法 P6 请求生成，不能提前手选或下载。

## 第 24 项：重上线生命周期边界审计（完成）

新增独立的 lifecycle-boundary-audit，只读比较 normalization failure 对应的主 symbol 与
`*SETTLED` 原始 ZIP，并绑定 contract identity registry。内容寻址报告为
`a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818`。

- AIA、CTK、CVC、CVX、LIT、MAVIA、PUMP、SLP 的 15m/1h/4h 断层右端均与
  `effective_from` 所属周期桶一致，SETTLED 行只位于主断层内；8/8 标记为
  `CONFIRMED_RELIST_BOUNDARY`。
- AERGO 虽有相似断层及 SETTLED ZIP，但没有 registry identity 边界，保持
  `UNRESOLVED`，未从行情反推生命周期。
- 8 个已确认标的的主 1d 月文件均包含边界日前旧行和边界日起新行，并与 SETTLED
  共享一个内容不同的边界日桶；全部标记为 `MIXED_OLD_LIFECYCLE` 并继续阻断。
- 报告保存 72 个 ZIP 的文件/行内容哈希、时间范围、断层和重叠计数；重复发布不可变。
- 合成测试覆盖确认正例、缺 identity 反例、registry 与断层右端不一致反例。
- normalization result 未修改；ATR reset、history seed、历史规则放行、研究、策略执行、
  locked test 消费均为 false。完整结论见 `docs/p9-lifecycle-boundary-audit.md`。

验证结果：

- 冻结 CPython 3.12.13：完整 pytest `839 passed, 1 skipped`；skip 为 Windows 环境不可
  创建 symlink 的既有场景。
- 第 24 项专项 pytest：`3 passed`；Ruff 全仓通过；mypy 全仓 `130 source files` 通过。
- lifecycle audit 用冻结环境复跑仍得到同一报告哈希 `a8caec...c818`，预期返回码为 1。
- 默认 `.venv` 当前为 CPython 3.12.14，完整套件为 `838 passed, 1 skipped, 1 failed`；
  唯一失败是 environment-lock 合成测试拒绝非冻结解释器。同一用例在冻结 3.12.13 环境
  单独及完整运行均通过，未修改该既有门禁测试。

## 第 25 项：生命周期范围 normalization remediation plan（完成）

新增 `lifecycle-normalization-remediation-plan/0.1.0` 的纯计划 builder/writer 与冻结
输入脚本。计划严格绑定 normalization result
`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2` 和 lifecycle audit
`a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818`，并在 writer 写入前
重建 trusted plan；计划自身重算 hash 不能替换可信输入。输出为：
`data/manifests/lifecycle_normalization_remediation_plan/fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79.json`。

- 8 个已确认 symbol 生成 32 个 15m/1h/4h/1d action；AERGOUSDT 保持 `UNRESOLVED`，
  进入 `excluded_symbols`，不生成 cutoff。
- 9 个边界桶恰好对齐、23 个部分桶排除；CTKUSDT/1d 无可保留行，整边界月主分区
  排除。边界月源 30,877 行，排除 20,724 行，预计保留 10,153 行；SETTLED 341 行
  全部为 `EVIDENCE_ONLY_NEVER_MERGE`。
- 15m/1h/4h/1d 均按 exact `effective_from` 推导首个完整桶，过滤条件为
  `open_time >= retain_from_open_time`；未对齐边界桶不进入新生命周期。
- frozen normalization、原始 ZIP、Parquet 均未修改；没有下载行情、连接账户、交易、
  部署或 push。ATR reset、history seed、历史规则放行、研究、策略执行、locked test
  消费均为 false，计划状态保持 `BLOCKED`。

验证结果：

- 冻结 CPython 3.12.13 完整 pytest：`845 passed, 1 skipped`；skip 为既有 Windows
  symlink 场景。
- 第 25 项专项 pytest（含正例、AERGO 反例、部分桶/整分区、可信 hash 与 writer 篡改）：
  `6 passed`；生命周期相关回归合计 `9 passed`。
- Ruff 全仓通过；mypy 全仓 `151 source files` 通过；计划脚本按预期返回码 `1`（因为
  计划必须保持 BLOCKED），重复生成 hash 稳定。

## 第 26 项：synthetic-only lifecycle derivative acceptance（完成）

新增独立 `lifecycle-scoped-normalization-acceptance/0.1.0` receipt 与纯内存 executor。
它只接受合成 `RAW_COLUMNS` DataFrame 和第 25 项 trusted action，复核主 ZIP hash、行
内容 hash、exact cutoff、输入/排除/保留行数，再调用既有纯 `normalize_klines()` 做内存
校验；CTK 型空边界分区直接 `EXCLUDED_EMPTY_BOUNDARY_PARTITION`，不调用 normalizer。
输出不写 Parquet；临时 acceptance writer 只在测试目录验证不可变写入。

- receipt 的 `execution_scope` 固定为 `SYNTHETIC_ONLY`，plan hash、主/SETTLED symbol、
  source hashes、cutoff、行数和 derivative content hash 均内容寻址。
- `output_materialized`、normalization execution、ATR reset、history seed、历史规则
  放行、research、strategy、locked test 等字段全部保持 false。
- 未下载行情、连接账户、交易、部署或 push；frozen normalization 和生产数据未修改。

验证结果：冻结 CPython 3.12.13 下第 26 项专项 `3 passed`；相关生命周期回归合计
`12 passed`；Ruff 和 mypy（相关 2 个文件）通过。

最终全量验证：冻结 CPython 3.12.13 `848 passed, 1 skipped`；Ruff 全仓通过；mypy
全仓 `153 source files` 通过；skip 为既有 Windows symlink 场景。

## 第 27 项：real-input dry-run design audit（完成）

新增 `lifecycle-derivative-dry-run-design/0.1.0` 设计审计。它只重读第 25 项内容寻址
plan，不调用 derivative executor、不读取原始行情、不生成 Parquet。冻结 namespace
`data/normalized/klines` 与未来 derivative namespace `data/normalized/lifecycle_scoped/v0.1.0`
通过纯路径隔离校验；32/32 action 的主/SETTLED hash、identity ref 和 settled symbol
lineage 完整。全量输入行守恒为 source 30,877 = excluded 20,724 + retained 10,153，
SETTLED evidence 341。

报告状态固定 `BLOCKED`，输出 `raw_market_data_read=false`、`derivative_executor_called=false`、
`output_materialized=false` 及全部研究/策略/交易授权 false。没有下载行情、连接账户、
交易、部署或 push。设计报告不构成真实执行授权。

内容寻址报告为
`124c2c1ed41ff3008c61b39fb8f02b70e30e8a650c3d49961368255483fb1898`。验证结果：冻结
CPython 3.12.13 下第 27 项专项 `3 passed`；全量 `851 passed, 1 skipped`；Ruff 全仓
通过；mypy 全仓 `156 source files` 通过。skip 为既有 Windows symlink 场景。

## 第 28 项：production executor 安全接口冻结（完成）

新增 `lifecycle-derivative-executor-interface/0.1.0` 内容寻址契约，只重读第 25、27 项
可信 manifest，不实现或调用 executor，不读取原始行情，不生成 Parquet。契约 hash 为
`e1033ff7559b3116e6825471911718dc16828ec6d99acc95d7e3d66d5a914fa3`，重复生成稳定。

接口固定本地主 archive 为唯一未来输入，SETTLED 为 `EVIDENCE_ONLY_NEVER_MERGE`；frozen
namespace 保持 `data/normalized/klines`，derivative namespace 固定为
`data/normalized/lifecycle_scoped/v0.1.0`。发布策略固定为 stage、validate、atomic rename；
恢复只允许复用内容完全一致的输出，冲突永不覆盖，空分区只允许 exclusion receipt。
32 个 action 和 `30,877 = 20,724 + 10,153` 行数守恒已绑定契约。

状态保持 `BLOCKED`；executor implementation、真实 normalization execution、ATR reset、
history seed、历史规则放行、research、strategy、locked test 授权全部为 false。没有下载
行情、连接账户、交易、部署或 push。冻结 CPython 3.12.13 专项测试 `5 passed`；Ruff 与
mypy 相关文件通过。最终验证为全部 lifecycle 回归 `20 passed`、Ruff 全仓通过、mypy
全仓 `159 source files` 通过、`git diff --check` 通过。第 27 项提交前完成的全量结果
`851 passed, 1 skipped` 仍是当前最近一次全量基线。

## 第 29 项：synthetic archive executor core（完成）

在既有 synthetic-only 内存 executor 外增加 ZIP 字节适配层。新入口不接受文件路径或输出
目录，只接收调用方提供的内存 ZIP 字节；先重验冻结 executor contract、可信 plan 与 action
membership，再核对主/SETTLED archive hash、CSV member、rows hash 和 SETTLED evidence
行数。只有主 symbol DataFrame 会进入既有 cutoff/normalization 路径，SETTLED 永远不合并。

合成正例确认 exact cutoff 后结果只含主 symbol 价格，且没有创建 `data/normalized`；反例
覆盖主 ZIP 篡改、SETTLED ZIP 篡改和不可信 contract。既有 acceptance 继续固定
`output_materialized=false`、normalization/ATR reset/history seed/历史规则放行/research/
strategy/locked test 授权全部为 false。本项没有读取真实 ZIP、写入 Parquet、下载行情、
连接账户、交易、部署或 push。

验证结果：冻结 CPython 3.12.13 下第 29 项合并专项 `7 passed`；Kline 与全部 lifecycle
扩大回归 `35 passed`；Ruff 全仓通过；mypy 全仓 `159 source files` 通过；
`git diff --check` 通过。最近一次完整 pytest 基线仍为第 27 项的 `851 passed, 1 skipped`。

## 第 30 项：synthetic staged publication acceptance（完成）

新增 synthetic-only 发布函数，先复核 contract/action/acceptance lineage、UTC 毫秒级
`normalized_at`、行数及 normalized content hash，再在调用方测试工作区的独立 staging
目录生成 Parquet。staged 文件通过既有 Parquet metadata/row-count/source-hash 复验和
文件 SHA-256 复验后，才以不可变发布原语暴露最终路径；重复同内容复用，任何既有内容冲突
均 Fail Closed 且 staging 自动清理。Windows 分区名沿用现有 normalization 约定，使用
action SHA-256 的 16 位前缀，避免临时文件名越过传统路径长度上限。

空边界分区只发布同分区路径下的 `.exclusion.json` acceptance，不生成 Parquet。函数显式
拒绝目标落入本仓库 `data/normalized`，合成验收只写 pytest 临时目录；frozen namespace
保持不存在/未修改。真实 ZIP、真实 derivative、下载、账户、交易、部署及 push 均未发生，
所有研究和策略授权继续为 false。

验证结果：冻结 CPython 3.12.13 下合并专项 `11 passed`，覆盖 stage/validate/publish、
幂等恢复、冲突不覆盖、staging 清理、空分区 receipt-only 及仓库路径拒绝；Kline 与全部
lifecycle 扩大回归 `39 passed`；Ruff 全仓通过；mypy 全仓 `159 source files` 通过；
`git diff --check` 通过。最近一次完整 pytest 基线仍为 `851 passed, 1 skipped`。

## 第 31 项：synthetic batch orchestration 与 completion receipt（完成）

新增 canonical action 顺序的 synthetic batch executor。调用方必须提供与可信 plan action
集合完全相等的主/SETTLED 内存 ZIP 集；batch 先复核 contract 与 plan 的 action 数及总行数，
再逐项复用第 29、30 项执行/发布路径。只有全部 action 成功后，才发布
`lifecycle-derivative-synthetic-batch/0.1.0` 内容寻址 receipt。

receipt 绑定 contract、plan、每项 acceptance hash、相对输出路径和输出 SHA-256，并核对
action 状态数及 source/excluded/retained/derivative 全量行守恒。它显式区分
`synthetic_output_materialized=true` 与 `real_output_materialized=false`，所有真实 normalization、
ATR reset/history seed、历史规则、research、strategy、locked test 授权保持 false。

合成正例覆盖 4 个 15m/1h/4h/1d action 的完整 batch 与幂等复跑；末项 ZIP 篡改反例证明
可能保留可恢复的已验证逐项输出，但绝不会发布 batch completion receipt。未读取真实 ZIP、
写入仓库 normalized、下载行情、连接账户、交易、部署或 push。

验证结果：冻结 CPython 3.12.13 下合并专项 `14 passed`；Kline 与全部 lifecycle 扩大回归
`42 passed`；Ruff 全仓通过；mypy 全仓 `159 source files` 通过；`git diff --check` 通过。
最近一次完整 pytest 基线仍为 `851 passed, 1 skipped`。

## 第 32 项：已授权真实 boundary-month derivative execution（完成）

用户在当前任务中明确批准读取本地真实主/SETTLED ZIP，并写入独立
`data/normalized/lifecycle_scoped/v0.1.0`；授权不包含下载、研究、locked test、交易、部署
或 push。新增内容寻址 authorization
`2ca444ef55f3b4854f6caa32a074ff6c8b1afc54753c5166141bfeaf0521e42c`，固定 32 个 action、
可信 contract/plan、输入行数和全部保留门禁；执行脚本还要求显式命令行确认开关。

真实执行逐项重验 64 个本地 ZIP 的 archive/rows hash、SETTLED evidence 行数、exact cutoff
和 plan membership。最终生成 31 个 Parquet 与 1 个 CTKUSDT/1d exclusion receipt，共
1,107,438 bytes；行数为 `30,877 = 20,724 excluded + 10,153 retained`。完成 receipt 为
`3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e`。

同一命令复跑得到相同 authorization/receipt hash；逐项输出文件 SHA-256、Parquet 总行数
和 receipt 全部复验通过，staging 已清理。授权后 frozen `data/normalized/klines` 修改文件数
为 0，原 normalization result 保持 `c86dd5...955d2`。`research_authorized=false`、
`strategy_executed=false`、`locked_test_consumed=false`，ATR reset/history seed 与历史规则
放行均未授权。

## 第 33 项：replacement normalization lineage（完成）

新增 `lifecycle-replacement-normalization/0.1.0` 内容寻址清单，把冻结 normalization result、
remediation plan 和真实 execution receipt 严格串联。overlay policy 固定为
`DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION`，不复制 6,900 万行基线数据，也不修改旧 Parquet。
结果 hash 为 `7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b`。

- 原 73,313 个可用分区中遮蔽 8 个受污染日线分区，叠加 31 个 materialized derivative，
  replacement 可用分区为 73,336；不可用 4 个分别是 AERGO 的 3 个原失败与
  CTKUSDT/1d exclusion。
- 原 27 个失败中，24 个已由 15m/1h/4h derivative 解决；AERGO 没有 verified identity
  boundary，3 个失败原样保留。
- replacement 行数按 `69,189,525 - 247 + 10,153 = 69,199,431` 守恒；清单不冒充已
  materialize 的第二份全量视图。
- writer 重复发布幂等、冲突拒绝；可信输入 hash、lineage、action coverage、分区数和行数均
  Fail Closed。脚本按预期返回码 1，因为状态必须保持 `BLOCKED`。
- frozen normalization、真实 derivative 和原始 ZIP 均未修改；研究、策略和 locked test
  未运行。历史规则、1m、Funding、ATR/history seed 和 AERGO 仍阻断。

验证结果：冻结 CPython 3.12.13 下第 33 项专项 `4 passed`；Kline 与全部 lifecycle 扩大
回归 `48 passed`；全量 `873 passed, 1 skipped`，skip 为既有 Windows symlink 场景；
Ruff 全仓通过；mypy 全仓 `141 source files` 通过；`git diff --check` 通过。清单脚本复跑
得到相同 result hash，并按设计返回码 1。

## 第 34 项：replacement 输入语义复验（完成）

`dev-execution-semantics/0.2.0` 可选识别第 33 项 replacement lineage。审计重新读取并验证
真实 completion receipt，逐个约束输出只能位于 lifecycle namespace，并复算 32 个输出文件
的 SHA-256；本地实测 32/32、1,107,438 bytes 通过。报告 hash 为
`c00ef3a90750581fc6ad2c490df8c2e5ae4ff143ba3e23d74081ee3c04f3f47e`。

- replacement 的可信 result hash、source normalization、dataset、remediation plan、receipt、
  action 状态数和 retained rows 全部交叉绑定；任何输出缺失、路径越界或字节改变均 Fail
  Closed。
- 已被 replacement 取代的 18 条旧 lifecycle gap 诊断不再重复报告，改由 replacement 自身
  5 个 blocker 表达当前状态。
- `CANDLE_MULTI_TIMEFRAME` 仍为 deferred：replacement 可用 73,336/73,340，AERGO 3 个失败与
  CTKUSDT/1d exclusion 未解决。1m、Funding、历史规则和 ATR/history seed 仍独立阻断。
- v0.1 报告保持可解析且 hash 兼容；未提供 replacement 时原语义与不可变 writer 字节保持
  不变。脚本按预期返回码 1，research/strategy/locked test 均未执行。

验证结果：冻结 CPython 3.12.13 下 execution input/semantics 与 replacement 专项
`40 passed, 1 skipped`；全量 `876 passed, 1 skipped`，skip 为既有 Windows symlink
场景；Ruff 全仓通过；mypy 全仓 `141 source files` 通过；`git diff --check` 通过。真实语义
脚本复跑得到相同报告 hash，并按设计返回码 1。

## 第 35 项：ATR/history seed 生命周期决策边界（完成）

新增 `atr-history-seed-audit/0.1.0`。它将当前策略实际使用的最长 SMA 窗口（180 根）与
Wilder ATR14 的 14 根初始化要求固定为每个 lifecycle stream 的最低同生命周期预热要求，
并按 exact lifecycle cutoff 给出第一个可用时间。任何 stream 只有同时具备 cutoff 后的完整、
连续同生命周期前缀且行数不少于 180 时才会得到 `SUFFICIENT`；这个技术结论仍不授予
ATR reset、history seed 或研究权限。

真实只读复跑得到内容寻址报告
`0f23e8abbf00ccd8b2911d002eac96d04d3fa5943912b6c88e06be7e6ebc839b`：32/32 个 lifecycle
stream 均保持 `BLOCKED`。已授权 derivative 只覆盖边界月，无法证明跨月的完整同生命周期
prefix；部分 1d/4h stream 另少于 180 根，CTKUSDT/1d 为 empty exclusion。AERGO 的三个
未解析 lifecycle failure 同样独立阻断。未读取旧生命周期数据作为 ATR seed，未修改 frozen
normalization，未下载、研究、执行策略或消费 locked test。

验证：新增合成审计专项 `7 passed`；Ruff 与 mypy 通过。脚本
`python scripts/p9_atr_history_seed_audit.py` 按设计返回码 `1`，因为阻断仍存在。

## 第 36 项：跨月 lifecycle warm-up 实证复验（完成）

将第 35 项审计升级为 `atr-history-seed-audit/0.2.0`，保留 v0.1 清单的解析与内容哈希兼容。
脚本不再把“边界月 derivative”误当作全部可用证据，而是从 lifecycle cutoff 开始，按连续
自然月选择达到 SMA180 所需的最小后续分区，并复用既有 Candle 输入边界逐项重验：原始 ZIP
SHA-256、download receipt、normalization receipt、原始数据重算、Parquet SHA-256、物理 schema、
metadata 与逐值一致性。边界 derivative 仍限定在独立 lifecycle namespace，并复验 action hash、
输出 hash、行数及 exact interval grid；未使用 cutoff 前旧生命周期数据。

真实只读复跑生成内容寻址报告
`552445ff085024194a57b565a8f17088b549bd9df4269fa56d604e20f7fcd53f`：已解析的 32/32 个
stream 全部为 `SUFFICIENT`、0 个已解析 stream blocked。报告仍为 `BLOCKED`，唯一 blocker
类别是 AERGOUSDT 的 15m/1h/4h 三个未解析 lifecycle identity；`atr_reset_authorized=false`、
`history_seed_authorized=false`、`research_authorized=false`、`strategy_executed=false`、
`locked_test_consumed=false`。脚本返回码 1 属预期。

同时修正非 interval 对齐的 identity effective time：v0.2 显式记录保守 ceil 后的
`warmup_prefix_start`，第一个可用 open time 从该网格起点计算；不会把边界桶的旧生命周期片段
纳入 180 根预热。冻结 CPython 3.12.13 下新增/更新专项 `8 passed`、lifecycle 扩大回归
`20 passed`、全量 `884 passed, 1 skipped`；skip 为既有 Windows symlink 场景。Ruff 全仓、
mypy `src scripts`（98 source files）与 `git diff --check` 均通过。

## 第 37 项：DEV normalization 角色范围收窄（完成）

AERGO 本地证据复核没有发现新的可信 identity 来源：2025-04 的主/SETTLED K 线只能证明价格
断层形态，保存的 exchangeInfo、contract registry 与 identity registry 均没有 AERGO 的
`effective_from`；因此不重复第 25 项审计，也不从行情反推身份边界。

随后修正 DEV 语义门禁的范围错误。冻结 split 的 DEV 为 `2023-08-01` 至 `2025-01-30`，
548 天、1,578,210 个预定扫描记录；27 个 normalization failure 最早为 `2025-03`。新增
`DevNormalizationScopeReference`，只有 normalization、Universe、split 与 gap audit 全部内容
绑定，27/27 失败均有可解释的原始 gap evidence，且所有 direct/lookback/recursive dependency
日期都不在 DEV 时，才给出 `NO_DEV_SCAN_DEPENDENCY`。合成反例把一个 recursive dependency
放入 DEV，门禁会恢复多周期 Candle blocker。

真实语义报告升级为 `dev-execution-semantics/0.3.0`，hash 为
`faacfca9fc1b6398f7b021a7448ea14f466eba24ef77d0f7684e752922673f54`。多周期 Candle 从
deferred 转为 validated；全局 replacement lineage 仍保留 73,336/73,340 与 AERGO/CTK 边界
状态，但这些 2025-03 之后的失败不再错误阻断 DEV。当前 DEV blocker 精确收敛为 3 项：

- `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`
- `RUN_INPUT_MISSING_CANDLE_ONE_MINUTE`
- `RUN_INPUT_MISSING_FUNDING`

报告仍为 `BLOCKED`，未运行研究、策略或 locked test。v0.1/v0.2 旧语义报告继续可解析且哈希
兼容。冻结 CPython 3.12.13 下专项 execution semantics 与 lifecycle replacement 回归
`26 passed`，全量 `885 passed, 1 skipped`；skip 为既有 Windows symlink 场景。Ruff 全仓、
mypy `src scripts`（98 source files）与 `git diff --check` 均通过。

## 下一项

实施计划没有定义 P10–P13；P9.2 DEV 当前只剩历史合约规则、1m 与 Funding 三项独立门禁。
历史规则是最上游 blocker：649 个 contract rule 全部 `UNVERIFIED`，16,440 个 DEV member-day
没有 1 个可用规则日。在规则证据解除前不能生成可信策略请求集合，因此也不能确定所需 1m 与
Funding 的精确请求范围；真实参数回测与 locked test 继续禁止。

整体保持 P0–P8 完成、P9 研究仍阻断（约 25%，工程阶段 9/14 约 64%）。

## 第 38 项：公开历史规则源点时存档审计（完成）

用户已授权联网查找、下载和审阅公开历史 Binance 合约规则证据；未访问账户、密钥、
付费接口或交易接口。官方文档继续确认 `/fapi/v1/exchangeInfo` 只返回当前规则。对
Internet Archive CDX 的 2023–2025 官方 URL 查询只取得 1 份去重后的 HTTP 200 JSON 捕获。

新增 `historical-rule-source-audit/0.1.0` 与只读脚本，固定原始文件 SHA-256、CDX digest、
存档/响应时间、回放 URL 和 DEV rule-gap hash；逐 symbol 解析避免一个无关坏条目遮蔽其他
可用点时观察，同时把坏条目完整计入阻断。真实报告 hash 为
`01b28cea5c13569cdc70ee3645eae4b2e0f4d22f051313c57b9d379217f5ef4d`：

- 快照 272 个 symbol，248 个 DEV 目标中观察到 186 个，缺少 62 个；
- 最高优先级 BTC/DOGE/ETH/SOL/XRP 五项均提取了精确点时 filters；
- `BTCSTUSDT` 的空 `contractType` 被显式记录为唯一不可解析条目；
- 响应时间为 `2023-11-02T08:47:08.849Z`，存档捕获时间晚 2,700,151 ms；
- BTC/ETH minimum notional 在响应时间已经是 100/20，早于官方公告所述的 10:00 UTC
  完成时间，进一步证明公告计划时间不能冒充精确切换时间；
- 点时快照不能证明区间连续性，第三方存档真实性仍待复核，新增 eligible member-day 为 0。

报告保持 `BLOCKED`、`registry_modified=false`、`research_authorized=false`、
`strategy_executed=false`、`locked_test_consumed=false`。专项历史源与既有 intake 回归
`41 passed`；冻结 CPython 3.12.13 下全量 `892 passed, 1 skipped`，skip 为既有 Windows
symlink 场景；Ruff 全仓与 mypy `src scripts`（100 source files）通过。真实脚本复跑 hash
一致，返回码 1 属预期。项目根目录 `.venv` 当前已漂移到 CPython 3.12.14，环境门禁会按
设计拒绝；本次没有修改 lock，也没有用漂移环境替代冻结验收。详细结论见
`docs/p9-historical-rule-evidence-gap.md`。

## 下一项（更新）

公开免费来源已实证到达其证据边界，尚未发现覆盖 DEV 的连续历史完整 filters。继续 P9
需要新的外部材料：带可信原始采集时间的连续 `exchangeInfo` 快照序列，或能同时提供
tick、step、min/max quantity、minimum notional 及精确生效区间的可审计数据源。没有该材料
时，不能生成可信 P4–P6 accepted plan，也不能确定 1m/Funding 的精确请求集。P9 参数研究、
VALIDATION 与 LOCKED_TEST 均继续禁止；实施计划仍没有正式 P10–P13 定义。

### 第 38 项补充：Common Crawl 公开索引核验（完成）

第 38 项提交后，补完了中断的第二公开存档索引核验。覆盖 DEV 的 Common Crawl 索引均返回
无 `exchangeInfo` 捕获；四个初次网关超时的索引重试后同样为无捕获。唯一找到的记录是
DEV 后的 2025 HTTP 451，并非 JSON 快照。没有新增文件下载或规则材料，因此不改变
`01b28cea...` 历史源审计、0 个 eligible member-day、注册表或研究授权；详细来源边界见
`docs/p9-historical-rule-evidence-gap.md`。

## 第 39 项：P9 验证基线与付费来源资格审计（完成至外部材料边界）

将 `test_execution_semantics` 的核心清单交叉绑定用例与宿主解释器解耦；测试注入已经由
独立 environment-lock 套件负责验证的锁结果，生产 preflight 的 CPython 3.12.13、精确包
版本和 wheel 哈希检查未改。默认 CPython 3.12.14 与冻结 CPython 3.12.13 全量均为
`892 passed, 1 skipped`；冻结环境 Ruff、mypy `171 source files`、CLI help 及 29 个 wheel
（78,976,028 bytes）哈希核验全部通过。恢复提交为 `a962bb1`。

随后新增 provider-neutral `rule-source-qualification/0.1.0`，以 12 项强制条件和三态结果
审计 Tardis.dev、Kaiko、Amberdata 的公开 schema、coverage、subscription 与 license 文档。
未购买、未登录、未使用凭证或付费 API。三个真实报告分别为：

- Tardis.dev `edd15e1f58a70e989de25edeead722687426b6363ac9a0629200097cfa5984c5`
- Kaiko `ba2d2619122c4b5dd5c895177095889ef90ba9923e8168b945f6bb5a9aa801b6`
- Amberdata `d286a057144bbd17ae9205f0125b4c82c0184aa42a7623f1c635a2957a09c0c6`

三者均为 `VENDOR_CONFIRMATION_REQUIRED`，固定不修改 registry、不授权研究、不消费
LOCKED_TEST。Tardis 非 multiplier 历史变化明确为 best-effort；Kaiko 未公开历史 filters；
Amberdata 只公开当前 reference schema，未提供完整历史变更保证。按批准计划，单区间 pilot、
人工 promotion、248 symbol 批量处理和 DEV 研究停在此处，等待供应商书面完整性保证及本地
原始 BTCUSDT/ETHUSDT 样例。资格专项 `16 passed`；最终默认与冻结全量均为
`901 passed, 1 skipped`，Ruff 全仓、冻结 mypy `173 source files`、CLI help、依赖 wheel
哈希和报告幂等复跑全部通过。详细报告见 `docs/p9-paid-rule-source-qualification.md`。
