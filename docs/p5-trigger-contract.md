# P5 15m Trigger Contract

状态：P5 实施契约  
策略版本：`ma-trend-pullback/0.1.0`  
Trigger 版本：`trigger/0.1.0`

## 1. 范围

P5 只负责：

```text
有效 P4 Setup
→ Trigger A: MA Reclaim
→ Trigger B: Liquidity Sweep + MA Reclaim
→ A/B 合并、确定性 logical signal ID
```

P5 不计算 tickSize 舍入、Entry、SL、TP、Score，不创建 Paper Trade，也不访问数据库或账户。`ARMED` 生命周期从 P6 生成完整 TradePlan 后开始。

## 2. 输入与 Fail Closed

- 只接收已收盘、UTC、严格升序且无缺口的 15m Candle；候选确认 K 线始终是最新行 `t`。
- `close_time_exclusive == open_time + 15m`，所有指标与 Candle 等长、同索引。
- 当前 P4 Setup 必须满足 `eligible_for_trigger=true`，方向只能为 LONG/SHORT，状态只能为 SHALLOW/STANDARD，并提供稳定 `episode_id`、`episode_start`。
- 当前 1H 回踩区域作为冻结证据输入：SHALLOW 为当前 1H SMA30–SMA60 闭区间，STANDARD 为 SMA60–SMA90 闭区间；保存排序后的 `region_lower <= region_upper`。
- 所有参与判断的 SMA、volume 统计、OHLC 必须有限；quote volume 非负。
- Trigger A 至少需要候选前 23 根 15m；Trigger B 的结构筛选至少需要候选前完整 96 根 15m。
- 合法但历史不足抛出 `TriggerNotReadyError`；非法或上下文矛盾抛出 `TriggerInputError`。
- A/B 独立评估：B 的 96 根结构历史不足不得抹掉已经合格的 A。

## 3. 通用量价确认

候选为 `t`，全部窗口严格排除候选：

```text
pullback_mean     = mean(volume[t-3:t])
pullback_baseline = median(volume[t-23:t-3])
trigger_baseline  = median(volume[t-20:t])
close_location    = (close[t] - low[t]) / (high[t] - low[t])
```

边界与原因：

1. `high == low`：`ZERO_RANGE_TRIGGER`，`close_location=None`，不再附加收盘位置原因。
2. `volume[t] <= trigger_baseline`：`TRIGGER_VOLUME_LOW`。
3. `volume[t] <= volume[t-1]`：`TRIGGER_VOLUME_NOT_EXPANDING`。
4. LONG 要求 `close_location >=0.65`；SHORT 要求 `<=0.35`。等号通过，不进行四舍五入；失败为 `VOLUME_WITH_ADVERSE_CLOSE`。
5. Trigger A 额外要求 `pullback_mean < pullback_baseline`；等号失败为 `PULLBACK_VOLUME_NOT_CONTRACTING`。
6. Trigger B 不以 pullback contraction 阻断，但仍保存两个统计值。

公共量价输出保留原始 float64 统计、每个条件布尔值和按上述顺序排列的失败原因。

## 4. Trigger A — MA Reclaim

LONG 条件及固定判断顺序：

1. P4 上下文有效，否则 `NO_VALID_EPISODE`。
2. 候选前最近 4 根中，至少一根的 `close <` 该根 15m SMA30，或该根 `[low, high]` 与冻结的 1H 回踩闭区间有交集；否则 `NO_MA_PULLBACK_TOUCH`。边界触及算重叠。
3. 候选 `close >` 当前 15m SMA30；等于失败，`MA_RECLAIM_FAILED`。
4. 候选 `close >` 前一根 high；等于失败，`PREVIOUS_BAR_BREAK_FAILED`。V0.1 不接受“等价确认结构”。
5. 通用量价和 Trigger A contraction 全部通过。
6. 存在 episode 内、候选确认收盘时已经生效的 15m Pivot Low；否则 `STOP_STRUCTURE_MISSING`。

SHORT 完全镜像：前 4 根 `close>SMA30`、候选 `close<SMA30`、候选 `close<` 前一根 low，结构使用 Pivot High。

结构 Pivot 必须满足：

- `interval=15m`、类型与方向相符。
- `pivot_time >= episode_start`。
- `confirmed_at <= candidate.close_time_exclusive`；候选收盘后才确认的 Pivot 不可使用。
- 多个可用 Pivot 按 `confirmed_at`、`pivot_time`、`pivot_id` 降序取最新者。

全部通过输出 `TRIGGER_A_CONFIRMED`，结构失效点为所选 Pivot price。失败原因按 A1→A6 顺序保存；依赖缺失的条件不伪造额外失败原因。

## 5. Trigger B — Sweep Reclaim

### 5.1 可用 zone

LONG 只使用 LOW zone，SHORT 只使用 HIGH zone。zone 必须：

- `interval=15m`。
- `confirmed_at <= candidate.open_time`，即 Sweep 开盘时已经生效。
- 最后一个成员 Pivot 的 `pivot_time` 位于候选前完整 96 根区间内。
- LONG：zone 生效后到 Sweep 开盘前，没有任何已收盘 15m `close < zone.lower`；SHORT 镜像为 `close > zone.upper`。等于边界不算破坏。

多个可用 zone 的稳定选择顺序：

1. `confirmed_at` 最新者优先。
2. 时间相同时，取与 Sweep 前一根 close 到 zone 闭区间距离最小者；close 在区间内时距离为 0。
3. 仍相同时按 `zone_id` 升序。

无可用 zone 为 `SWING_ZONE_MISSING`。

### 5.2 Sweep 条件

LONG 条件及固定顺序：

1. P4 上下文有效，否则 `NO_VALID_EPISODE`。
2. 存在第 5.1 节选出的支撑 zone。
3. 候选 `low < zone.lower`；等于失败，`SWEEP_NOT_BEYOND_ZONE`。
4. 候选 `close > zone.upper`；等于失败，`SWEEP_RECLAIM_FAILED`。
5. 候选 `close >` 当前 15m SMA30；等于失败，`MA_RECLAIM_FAILED`。
6. 通用量价通过；不要求前三根缩量。

SHORT 镜像为 `high > zone.upper`、`close < zone.lower`、`close < SMA30`。全部通过输出 `TRIGGER_B_CONFIRMED`；结构失效点为候选 Sweep 极值（LONG low，SHORT high）。

## 6. A/B 合并与逻辑去重

| A | B | primary_trigger | all_triggers |
|---|---|---|---|
| 否 | 否 | `None` | `[]` |
| 是 | 否 | `MA_RECLAIM` | `[MA_RECLAIM]` |
| 否 | 是 | `SWEEP_RECLAIM` | `[SWEEP_RECLAIM]` |
| 是 | 是 | `SWEEP_RECLAIM` | `[SWEEP_RECLAIM, MA_RECLAIM]` |

只有合并后存在 Trigger 时生成 logical signal ID：

```text
sha256(
  trigger_version | strategy_version | symbol | direction |
  episode_id | confirmation_15m_open_time
)
```

- 时间使用带时区的 canonical ISO-8601 毫秒格式。
- 同一输入重放产生相同 ID；调用方按 ID 幂等写入，不能创建第二条逻辑信号。
- `ACTIVE_TRADE_EXISTS`、`ARMED_SIGNAL_EXISTS` 属于后续持久化/生命周期协调层；P5 纯函数不查询或伪造外部状态。

## 7. 输出

每个 A/B evaluation 至少保存：

- Trigger 版本、symbol、direction、episode、confirmation candle 时间。
- `confirmed`、有序 reason codes、所用 volume/MA/价格证据。
- A 的 structure Pivot ID/price；B 的 zone ID/bounds 和 Sweep 极值。

合并结果保存 `primary_trigger`、有序 `all_triggers`、logical signal ID 和 `eligible_for_plan`。无 Trigger 时 ID 为 `None`。

## 8. 验收矩阵

- Trigger A：GS-021–026，包括严格 contraction、0.65/0.35、零振幅和缺失 Pivot。
- Trigger B：GS-027–031，包括 Pivot 确认时点、zone 合并边界、zone 破坏与完整 reclaim。
- 合并：GS-032，A+B 只输出一个 ID，Sweep 为 primary。
- LONG/SHORT 镜像；所有严格不等式均覆盖等号失败。
- 截断未来 Candle 不改变已确认 Trigger、Pivot、zone 选择与 logical signal ID。
- 同一输入重复运行的完整模型和 JSON 输出一致。

P5.1 只冻结以上契约；P5.2 才开始实现 Trigger A。
