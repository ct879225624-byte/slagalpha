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

## 下一项

第 34 项应让只读输入语义审计识别 replacement lineage，但仍要求所有独立研究输入门禁通过；
它只能证明生命周期替代规则可消费，不能放行 AERGO、历史规则、1m、Funding 或 ATR/history
seed，也不能开始参数研究。

整体保持 P0–P8 完成、P9 研究仍阻断（约 25%，工程阶段 9/14 约 64%）。
