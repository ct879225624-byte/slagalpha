# P9 生命周期范围 normalization remediation plan

核验日期：2026-09-08。此文件记录第 25 项的只读、不可执行计划；它不是新的
normalization result，也不覆盖任何 frozen result、Parquet 或原始 ZIP。

## 输入与内容寻址

- 冻结 normalization result：`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`
- lifecycle-boundary audit：`a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818`
- identity registry（由 audit 绑定）：`739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36`
- 计划 manifest：`data/manifests/lifecycle_normalization_remediation_plan/fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79.json`
- 计划内容哈希：`fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79`

生产脚本固定绑定上述两个输入哈希，并在写入前从 trusted audit 重建计划。仅修改
JSON 后重新计算自身哈希，不能替换可信输入。

## 边界规则

对每个已确认标的的 15m、1h、4h、1d 主 symbol 月分区：

1. 使用 identity registry 的 `effective_from`；audit 的 floor 桶只用于证据核验。
2. 若 `effective_from` 落在桶中，排除该部分桶，并从下一个完整桶
   `retain_from_open_time` 开始保留；若恰好对齐，则保留该边界桶。
3. 过滤语义是 `open_time >= retain_from_open_time`，区间为半开边界；`*SETTLED`
   ZIP 始终是 `EVIDENCE_ONLY_NEVER_MERGE`。
4. 若边界月没有可保留行（CTK/1d），只记录整主分区排除，不调用空帧 normalizer。
5. AERGO 没有 verified identity，保持 `UNRESOLVED`，不生成任何 cutoff action。

## 结果摘要

| 项目 | 数值 |
|---|---:|
| 目标 symbol | 9 |
| 计划 symbol | 8 |
| 排除 symbol | 1（AERGOUSDT）|
| action | 32（8 × 4 周期）|
| 已对齐边界桶 | 9 |
| 部分边界桶排除 | 23 |
| 整个边界月主分区排除 | 1（CTK/1d）|
| 边界月源行 | 30,877 |
| 计划排除行 | 20,724 |
| 预计保留行 | 10,153 |
| SETTLED 证据行 | 341 |

各已确认标的的边界月统计如下：

| symbol | period | source | excluded | retained | SETTLED evidence | 1d retain-from |
|---|---|---:|---:|---:|---:|---|
| AIAUSDT | 2026-01 | 3,879 | 2,416 | 1,463 | 38 | 2026-01-21 00:00Z |
| CTKUSDT | 2025-04 | 3,757 | 3,686 | 71 | 54 | 2025-05-01 00:00Z |
| CVCUSDT | 2025-05 | 3,893 | 1,908 | 1,985 | 47 | 2025-05-17 00:00Z |
| CVXUSDT | 2025-07 | 3,878 | 2,797 | 1,081 | 35 | 2025-07-24 00:00Z |
| LITUSDT | 2025-12 | 3,846 | 2,797 | 1,049 | 38 | 2025-12-24 00:00Z |
| MAVIAUSDT | 2025-03 | 3,848 | 3,177 | 671 | 90 | 2025-03-27 00:00Z |
| PUMPUSDT | 2025-07 | 3,899 | 1,146 | 2,753 | 4 | 2025-07-11 00:00Z |
| SLPUSDT | 2025-07 | 3,877 | 2,797 | 1,080 | 35 | 2025-07-24 00:00Z |

## 门禁状态

计划状态为 `BLOCKED`。`normalization_execution_authorized`、`output_materialized`、
`atr_reset_authorized`、`history_seed_authorized`、`historical_rule_gate_relaxation_authorized`、
`research_authorized`、`strategy_executed`、`locked_test_consumed` 均为 `false`；
replacement result/dataset hash 均为 `null`。阻断原因保留：

- `ATR_RESET_OR_HISTORY_SEED_NOT_AUTHORIZED`
- `DERIVATIVE_NORMALIZATION_NOT_EXECUTED`
- `HISTORICAL_RULE_GATE_REMAINS_BLOCKED`
- `UNRESOLVED_LIFECYCLE_SYMBOL_EXCLUDED:AERGOUSDT`

本项没有下载行情、连接账户、交易、部署或 push。下一项只能在单独批准并实现新的
lifecycle-scoped derivative namespace 后进行；不得把本计划当作执行授权。
