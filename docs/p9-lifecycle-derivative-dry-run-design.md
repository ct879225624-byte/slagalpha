# P9 lifecycle derivative real-input dry-run design audit

第 27 项只审计未来真实 derivative normalization 的输入与路径设计，不执行
derivative executor，不读取原始行情，不生成 Parquet。

## 审计边界

设计审计只重读第 25 项的内容寻址 remediation plan：

- plan：`fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79`
- frozen normalization：`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`
- lifecycle audit：`a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818`

冻结 normalization 的持久化 namespace 是 `data/normalized/klines`；未来 derivative
只能使用独立的 `data/normalized/lifecycle_scoped/v0.1.0`。审计使用纯路径比较，拒绝
相同路径或父子路径，防止覆盖、混入或伪装成既有 Parquet。

## Lineage 与行数守恒

32 个 action 全部具备主/SETTLED archive hash、rows hash、identity source ref 和
settled symbol。审计重新汇总每个 action 的边界月行数：

| 项目 | 数值 |
|---|---:|
| action | 32 |
| lineage 完整 action | 32 |
| source rows | 30,877 |
| excluded rows | 20,724 |
| retained rows | 10,153 |
| SETTLED evidence rows | 341 |

守恒条件为 `source = excluded + retained`，并由模型 validator 和设计报告再次核验。
报告本身保存为：
`data/manifests/lifecycle_derivative_dry_run_design/124c2c1ed41ff3008c61b39fb8f02b70e30e8a650c3d49961368255483fb1898.json`。

## 门禁结论

设计报告状态固定为 `BLOCKED`。`raw_market_data_read`、`derivative_executor_called`、
`output_materialized`、`normalization_execution_authorized`、`research_authorized`、
`strategy_executed` 和 `locked_test_consumed` 均为 `false`。阻断原因保留真实执行未授权、
冻结 normalization 不可变、历史规则门禁和研究授权关闭。

第 27 项没有下载行情、连接账户、交易、部署或 push。下一步即使实施真实输入执行器，
也必须先获得单独批准，并继续使用新 namespace；本审计不构成执行授权。

冻结 CPython 3.12.13 验证：专项 `3 passed`；全量 `851 passed, 1 skipped`；Ruff
全仓通过；mypy 全仓 `156 source files` 通过。skip 为既有 Windows symlink 场景。
