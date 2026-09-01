# P7 Event-driven Replay Report

状态：已验收  
版本：`event-replay/0.1.0`

## 已完成

- P7.1：冻结 1m 时间推进、同分钟不利成交顺序、成本场景、Funding、MAE/MFE 与输出契约。
- P7.2：实现 ARMED 信号的 Entry、INVALIDATED、MISSED、EXPIRED 确定性状态重放。
- Entry 普通穿越、`0.15 ATR` 跳空边界、右开过期时间、入场与失效同分钟、LONG/SHORT 镜像均有测试。
- 输入要求完整 UTC 1m 网格、精确一分钟区间、已收盘且 OHLC 合法；异常输入 Fail Closed。
- 事件 ID 由版本、逻辑信号、事件类型、交易所时间与来源引用确定性生成。
- P7.3：实现入场后的 FULL、HALF_AFTER_TP1、FLAT 状态，以及 SL、TP1、TP2、保本和 TIME_EXIT 理论成交。
- 无逐笔数据时，FULL 同触 SL/TP1 固定止损优先；未触 SL 而同触 TP1/TP2 时依次各退出 50%。
- TP1 后保本仅在下一根 1m 生效；TIME_EXIT 使用 entry 所在 15m bucket 推导确定性 deadline。
- P7.4a：实现 ZERO、BASELINE、STRESS 三种逐 Fill Fee/Slippage 账本与成本保本价。
- 已平仓交易输出 Gross PnL、模拟执行 PnL、Funding 前 Net PnL，以及 Gross/Net R；未平仓结果不伪造完整 PnL。
- BUY/SELL 滑点方向、部分退出的 0.5 数量和 LONG/SHORT 手续费均按每个 Fill 单独核对。
- P7.4b：Funding schedule 由外部版本化数据显式提供，不硬编码固定结算时刻；按结算时实际剩余仓位计算现金流。
- Funding rate、mark price、覆盖范围或整套数据缺失时保留 Gross，设置 `net_statistics_eligible=false`，不以零代替。
- MAE/MFE 使用交易级 1m 可见极值，TP1 后不重置，并覆盖 LONG/SHORT 镜像。
- P7.4c1：完整 Aggregate Trade 批次按 `(trade_time, aggregate_trade_id)` 排序，直接决定 Entry/失效和持仓关键价位先后。
- 逐笔批次必须与对应 1m 的 Open/High/Low/Close 一致；不完整批次整根回退 OHLC，矛盾的完整批次 Fail Closed。
- 输出记录 `ordering_source` 与 `ohlc_fallback_count`，关键逐笔事件保存 `source=AGG_TRADE` 和 aggregate trade ID。
- P7.4c2：完整事件、Fill、Funding、成本和 MAE/MFE 输出序列化为规范 JSON，并生成稳定 trade ID 与 SHA-256 canonical hash。
- P7.5：TIME_EXIT 同分钟终止型 SL/TP 候选按成本场景比较 Net 退出现金流；迟到 Entry 只追加忽略事件；事件 reducer 支持 checkpoint 恢复和重复 ID 去重。
- P7.6：多标的 case 按交易所时间、symbol、logical signal ID 确定性排序，并阻止同标的活跃交易重叠。

## 当前验证

```text
P7 replay/cost/analytics/ordering/recovery tests: 28 passed
full pytest: 147 passed
ruff: All checks passed
mypy: Success: no issues found in 41 source files
CLI help: passed
```

## 当前边界

- P7.3 理论生命周期由 P7.4a 注入场景对应的成本保本价。
- 只有经过上游完整性断言且能复现 Candle OHLC 的批次才使用逐笔顺序；完整性断言本身仍依赖未来 P8 数据清单与下载验证。
- Funding schedule 必须由版本化外部数据提供；系统不硬编码可能变化的结算时刻。
- 当前验证使用确定性 synthetic fixture，尚未用真实 36 个月数据证明成交覆盖率、性能或策略收益。

P7 验收矩阵已通过本地自动化验证，并由用户确认进入 P8。后续如改变 P7 事件或成本语义，必须提升 Replay 版本。
