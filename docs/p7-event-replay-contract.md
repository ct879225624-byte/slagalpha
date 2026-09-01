# P7 Event-Driven Replay Contract

状态：P7 实施契约  
策略版本：`ma-trend-pullback/0.1.0`  
Replay 版本：`event-replay/0.1.0`

## 1. 范围

P7 使用已验收 P6 TradePlan 和按交易所时间排序的 1m 数据，依次完成：

```text
ARMED
→ ENTRY_FILL / INVALIDATED / MISSED / EXPIRED
→ SL / TP1 / TP2 / BREAKEVEN / TIME_EXIT
→ Gross/Net PnL、R、成本、Funding、MAE/MFE
```

P7 不重新计算 Setup、Trigger、Entry、Stop、TP 或 Score，不访问真实账户，不下单，不使用当前系统时间或随机路径猜测。

## 2. 输入与时间

- 1m Candle 必须 UTC、严格升序、唯一、全部收盘，并满足完整 1 分钟网格和 OHLC/非负 volume 校验。
- 正式路径只读取当前事件时间及以前数据；禁止向量化使用未来 high/low 决定当前状态。
- Candle 区间为 `[open_time, close_time_exclusive)`；事件按 `exchange_time` 升序。
- Entry 最早处理 `open_time >= confirmation_close` 的第一根 1m。
- Entry 窗口是 `[confirmation_close, expires_at)`；`open_time >= expires_at` 时先 EXPIRED，不再读取该 Candle 的价格触发 Entry。
- 最大持仓参数默认 32 根 15m，只允许 `{16,32,48}`。
- entry 所在 15m bucket 以 UTC floor 到 15 分钟；TIME_EXIT deadline 为 `bucket_open + max_holding_bars×15m`，在 deadline 后第一根可用 1m open 执行。
- 数据缺口、乱序、重复冲突或计划版本不匹配 Fail Closed，不补造事件。

## 3. 状态与事件

Signal 状态：`ARMED`、`TRIGGERED`、`INVALIDATED`、`MISSED`、`EXPIRED`、`CLOSED`。

持仓阶段：`FULL`（100%）、`HALF_AFTER_TP1`（50%）、`FLAT`。

事件类型最低集合：

- `ARMED`、`ENTRY_FILL`、`INVALIDATED`、`MISSED`、`EXPIRED`。
- `TP1_FILL`、`BREAKEVEN_ACTIVATED`、`STOP_FILL`、`TP2_FILL`、`TIME_EXIT`、`CLOSED`。
- `FUNDING`、`FUNDING_DATA_MISSING`、`LATE_EVENT_IGNORED`。

事件日志只追加。相同确定性 event ID 再次应用不得产生第二次 Fill、成本或状态转换。

```text
event_id = sha256(
  replay_version | entity_id | event_type | exchange_time | source | source_ref
)
trade_id = sha256(replay_version | logical_signal_id)
```

时间使用 canonical ISO-8601 毫秒；event type 和 source 使用固定枚举值。

## 4. ARMED 路径

LONG 边界；SHORT 价格轴镜像：

- Entry 触及：`high >= entry`；SHORT 为 `low <= entry`，等号成交。
- 入场前失效：`low <= invalidation`；SHORT 为 `high >= invalidation`，等号失效。
- 若 1m open 已在 Entry 有利一侧，LONG 为 `open >= entry`、SHORT 为 `open <= entry`：
  - `gap = abs(open-entry)`。
  - `gap >0.15×confirmation ATR`：MISSED。
  - 等于阈值：按 open 理论成交。
- 非 gap 的正常穿越按计划 Entry 理论成交。
- 一根 1m 只触及 invalidation、未触及 Entry：INVALIDATED。
- 同一 1m 同时可能 Entry 与 invalidation：若无可用 Aggregate Trades，固定为 `ENTRY_FILL → STOP_FILL → CLOSED/SL`；不能无损 INVALIDATED。
- 已 INVALIDATED/MISSED/EXPIRED 的迟到 Entry 只记 `LATE_EVENT_IGNORED`，终态不变。

## 5. Aggregate Trades 与 OHLC 回退

- 若当前 1m 有完整 Aggregate Trades，则按 `(trade_time, aggregate_trade_id)` 升序，用逐笔 CONTRACT_PRICE 首次穿越顺序决定关键价位事件。
- Aggregate Trades 必须完整覆盖 Candle 区间、ID 唯一、价格/数量有限且非负；不完整时整根退回 OHLC 不利顺序，不能混用半根逐笔与 OHLC。
- 无逐笔时固定不利顺序：
  - ARMED 同触 Entry/失效：Entry 后 Stop。
  - FULL 同触 SL/TP1：SL 优先。
  - FULL 同触 TP1/TP2 且未触 SL：TP1 后 TP2。
  - HALF 同触 active breakeven/TP2：breakeven 优先。
  - TP/SL 与 TIME_EXIT 同时：比较完整候选路径的 Net PnL，选择更差者；相等时风险退出优先。

## 6. 持仓路径

- FULL 触及 initial Stop：100% `STOP_FILL`，随后 CLOSED/SL。
- FULL 触及 TP1：50% `TP1_FILL`，状态变为 HALF；breakeven 只安排在下一根 1m `open_time` 生效。
- 同一根先 TP1 后回落到拟议 breakeven 不退出剩余仓位。
- FULL 同根触及 TP1/TP2 且未触 Stop：依次各退出 50%，随后 CLOSED/TP2。
- HALF 触及 active breakeven：剩余 50% STOP_FILL，CLOSED/BREAKEVEN_AFTER_TP1。
- HALF 触及 TP2：剩余 50% TP2_FILL，CLOSED/TP2。
- 到 TIME_EXIT deadline 尚有 100% 或 50%：按 deadline 后第一根 1m open 退出剩余数量，CLOSED/TIME_EXIT。

触及定义：LONG Stop `low<=stop`、TP `high>=tp`；SHORT 镜像。等号均触发。

## 7. 成本模型

成本场景：

| 模型 | fee/fill | slippage/fill | Funding |
|---|---:|---:|---|
| ZERO | 0 bps | 0 bps | 0 |
| BASELINE | 6 bps | 2 bps | 实际 |
| STRESS | 6 bps | 5 bps | 实际 |

- 每个 Entry、TP1、TP2、Stop、breakeven、TIME_EXIT 独立计费。
- BUY simulated price = theoretical × `(1+slippage_rate)`；SELL = theoretical × `(1-slippage_rate)`。
- fee = `abs(simulated_price × quantity_fraction) × fee_rate`。
- Gross PnL 使用 theoretical fill；Net PnL 使用 simulated fill，减全部 fee，加 Funding cash flow；Slippage cost另存为 Gross 与 simulated execution PnL 的差。
- LONG Funding cash flow = `-notional×rate×open_fraction`；SHORT 镜像为正。负 rate 自然反转。Funding 使用结算时 mark price；同一结算时间只应用一次。
- 跨 Funding 时间但 rate 或 mark price 缺失：保留 Gross 结果并记 `FUNDING_DATA_MISSING`，`net_statistics_eligible=false`，不得以 0 冒充。

## 8. 成本保本 Stop

TP1 后对剩余 50% 使用“该剩余腿独立覆盖 Entry 与未来退出 fee/slippage”的理论 breakeven，不使用 TP1 已实现利润补贴：

```text
LONG:
breakeven = entry_simulated × (1+fee_rate) /
            ((1-slippage_rate) × (1-fee_rate))

SHORT:
breakeven = entry_simulated × (1-fee_rate) /
            ((1+slippage_rate) × (1+fee_rate))
```

LONG 向上对齐 tick，SHORT 向下对齐 tick。Funding 不进入该已知成本保本价。激活事件时间为 TP1 所在 1m 的 `close_time_exclusive`，最早下一根 1m 生效。

## 9. MAE、MFE 与 R

- 单位仓位起始数量为 1；TP1/TP2 各 0.5。
- `1R = risk_per_unit`，Gross/Net R = 对应 PnL / 初始 `1R`。
- MAE/MFE 使用 Entry 后到最终退出前实际可见的 1m high/low，相对 theoretical Entry 的最差/最好价格偏移，再除以 1R。
- Entry 所在 Candle 从 Entry 事件之后的可确定路径开始计；无逐笔时整根 high/low 可用于不利 MAE/MFE，并标记 OHLC fallback。
- 部分退出不重置 trade-level MAE/MFE。

## 10. 输出与确定性

每笔 PaperTrade 保存：

- trade/signal/plan/version IDs、direction、状态、entry/exit 时间。
- 完整有序 Event 与 Fill；每个 Fill 保存 side、fraction、theoretical/simulated price、fee、slippage、source。
- initial Stop、TP1/TP2、breakeven、TIME_EXIT deadline。
- Gross/Net PnL 与 R、Fee、Slippage、Funding、MAE/MFE、持仓时长、终止原因。
- `ordering_source=AGG_TRADES|OHLC_ADVERSE`、fallback 次数、`net_statistics_eligible`、reason codes。

相同计划、1m/agg-trade/funding 输入和成本模型必须产生相同 Event/Fill 序列、canonical hash 与核心指标。不得依赖输入行原始顺序以外的不稳定容器顺序。

## 11. 验收矩阵

- GS-037–041：正常 Entry、gap 等号、右开到期、同分钟 Entry/失效与明确先失效。
- GS-042–045：breakeven 下一分钟、SL/TP1 不利优先、TP1/TP2 同分钟、TIME_EXIT。
- GS-046：Funding 缺失保留 Gross、排除正式 Net。
- GS-050：相同输入 Event/Fill/Trade canonical hash 一致。
- LONG/SHORT 镜像；所有触及等号与终态幂等。
- ZERO/BASELINE/STRESS 每次 Fill 成本可手工核对，部分退出不重复扣除数量。

P7.1 只冻结以上契约；P7.2 才开始实现 ARMED 生命周期。
