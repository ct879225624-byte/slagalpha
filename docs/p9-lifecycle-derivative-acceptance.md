# P9 lifecycle-scoped derivative acceptance

第 26 项实现了一个 synthetic-only 的 lifecycle-scoped derivative executor 和
acceptance receipt。它用于验证第 25 项计划的边界语义，不是生产 normalization
执行器，也不产生真实 Parquet。

## 输入与行为

`execute_synthetic_lifecycle_derivative()` 只接受：

- 已通过内容寻址校验的 `LifecycleNormalizationRemediationPlan` 和其中的 action；
- 调用方提供的合成 Binance `RAW_COLUMNS` DataFrame；
- action 对应的 primary ZIP SHA-256。

它在内存中复核 source archive hash、source rows hash、边界 cutoff、输入/排除/保留
行数，然后按 `open_time >= retain_from_open_time` 筛选。保留行调用既有纯
`normalize_klines()` 做 schema、数值、网格和时间验证；结果只返回内存 DataFrame。
如果 action 没有可保留行，返回 `EXCLUDED_EMPTY_BOUNDARY_PARTITION`，不会调用
normalizer。

每个 receipt 都绑定 remediation plan hash、主/SETTLED symbol、source hashes、cutoff、
行数和 derivative content hash，并对 canonical JSON 自校验。receipt writer 只用于
调用方选择的合成临时目录，采用不可变路径；生产数据目录和 frozen normalization
不会被 executor 访问。

## 门禁

receipt 的 `execution_scope` 固定为 `SYNTHETIC_ONLY`，`output_materialized`、
`normalization_execution_authorized`、`atr_reset_authorized`、`history_seed_authorized`、
`historical_rule_gate_relaxation_authorized`、`research_authorized`、`strategy_executed`
和 `locked_test_consumed` 全部固定为 `false`。该项没有下载行情、连接账户、交易、
部署或 push。

## 验收

```text
.venv\Scripts\python.exe -m pytest -q tests\test_lifecycle_derivative.py
.venv\Scripts\python.exe -m ruff check src\slagalpha\research\lifecycle_derivative.py tests\test_lifecycle_derivative.py
.venv\Scripts\python.exe -m mypy src\slagalpha\research\lifecycle_derivative.py tests\test_lifecycle_derivative.py
```

合成覆盖：成功筛选并规范化、空边界分区排除、source rows 篡改拒绝，以及不可变
acceptance receipt 重复写入。
