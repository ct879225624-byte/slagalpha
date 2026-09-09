# P9 authorized lifecycle boundary-month execution

2026-09-09，用户明确批准读取本地真实主/SETTLED ZIP，并把 lifecycle boundary-month
derivative 写入独立 namespace。授权仅限本地 normalization，不包含下载、研究、locked
test、交易、部署或 push。

## 内容寻址输入与授权

- remediation plan：`fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79`
- executor contract：`e1033ff7559b3116e6825471911718dc16828ec6d99acc95d7e3d66d5a914fa3`
- execution authorization：`2ca444ef55f3b4854f6caa32a074ff6c8b1afc54753c5166141bfeaf0521e42c`
- execution receipt：`3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e`

授权记录明确允许读取本地 archive 和 materialize lifecycle derivative，同时保持 frozen
normalization mutation、市场下载、ATR reset、history seed、历史规则放行、research、
strategy 和 locked test 授权为 false。

## 执行结果

32 个边界月 action 全部完成：31 个 Parquet、1 个 CTKUSDT/1d exclusion receipt。

| 项目 | 数值 |
|---|---:|
| 主/SETTLED ZIP | 64 |
| source rows | 30,877 |
| excluded rows | 20,724 |
| retained / Parquet rows | 10,153 |
| derivative files | 32 |
| derivative bytes | 1,107,438 |

每项输出绑定 action、主/SETTLED archive 与 rows hash、normalized content hash、相对路径和
输出 SHA-256。completion receipt 最后发布；同一命令复跑 hash 不变。所有输出 SHA-256、
Parquet 总行数和 receipt 模型已复验，staging 为空。

## 隔离与剩余门禁

输出只位于 `data/normalized/lifecycle_scoped/v0.1.0`。授权时间后 frozen
`data/normalized/klines` 没有文件被修改，旧 normalization result 保持不变。

本结果只修复已验证的生命周期边界月，不是完整研究输入。AERGO 仍 unresolved；历史规则
证据、1m 和 Funding 仍缺失；`research_authorized=false`、`strategy_executed=false`、
`locked_test_consumed=false`。
