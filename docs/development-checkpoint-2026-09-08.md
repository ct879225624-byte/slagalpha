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

## 下一项

第 30 项只验收 synthetic output 的 staging、完整校验、原子发布、幂等恢复与冲突拒绝；
输出仍必须限定在测试临时目录，不得读取真实 ZIP 或写入仓库 `data/normalized`。真实执行、
ATR reset/history seed、历史规则和研究门禁继续保持关闭。

整体保持 P0–P8 完成、P9 研究仍阻断（约 25%，工程阶段 9/14 约 64%）。
