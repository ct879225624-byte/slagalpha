# P6 Trade Plan and Score Contract

状态：P6 实施契约  
策略版本：`ma-trend-pullback/0.1.0`  
Trade Plan 版本：`trade-plan/0.1.0`

## 1. 范围

P6 只负责：

```text
已确认 P5 TriggerDecision
→ Decimal Entry / initial Stop / 1R
→ 前方障碍与 TP1 / TP2
→ 0–100 审计评分
```

P6 不读取账户余额，不计算真实仓位，不模拟 1m 成交，不改变 ARMED/TRIGGERED 生命周期，不创建 Paper Trade。Score 只排序，不能挽救任何失败的强制条件。

## 2. 数值与输入契约

- Entry、Stop、TP、tickSize、ATR 和所有价格边界进入 P6 时统一转换为 `Decimal(str(value))`；禁止直接 `Decimal(float)`。
- `tick_size > 0`，确认时 15m `ATR14 > 0`，所有价格有限且非负；否则分别为 `TICK_SIZE_INVALID`、`ATR_INVALID` 或 `PRICE_RULE_INVALID`。
- TriggerDecision 必须 `eligible_for_plan=true` 且有 logical signal ID、primary trigger、确认 K 线 high/low 和结构失效点。
- LONG/SHORT 只共用镜像算法。所有舍入均通过 `price / tick_size` 的整数 floor/ceiling 完成，支持非十进制 tick（例如 `0.25`），不能仅按小数位量化。
- 默认 Stop ATR multiplier 为 `0.15`；只允许单变量敏感度 `{0.10, 0.15, 0.20}`。
- 默认 Entry TTL 为 4 根 15m；只允许 `{2,4,6}`。计划保存 `confirmation_close` 与右开 `expires_at = confirmation_close + ttl×15m`，但成交生命周期由 P7 处理。
- 任何强制条件失败时返回可审计的 rejected plan，不计算正式 Score。

## 3. Entry

### LONG

```text
raw_entry = confirmation_high + tick_size
entry = ceil_to_tick(raw_entry)
```

### SHORT

```text
raw_entry = confirmation_low - tick_size
entry = floor_to_tick(raw_entry)
```

- 加/减 1 tick 后仍需再次按方向对齐，不能假设确认价格本身在 tick 网格。
- LONG 向上、SHORT 向下是对交易者保守的入场舍入。
- 舍入结果必须严格位于确认 K 线突破方向，否则 `PRICE_RULE_INVALID`。

## 4. Initial Stop 与风险

```text
buffer = max(stop_atr_multiplier × ATR14, 2 × tick_size)
```

- Trigger A 的 invalidation 是所选结构 Pivot price；Trigger B 是 Sweep 极值。P6 使用 P5 primary trigger 对应的 invalidation。
- LONG：`raw_stop = invalidation - buffer`，`stop = floor_to_tick(raw_stop)`。
- SHORT：`raw_stop = invalidation + buffer`，`stop = ceil_to_tick(raw_stop)`。
- `risk_per_unit = abs(entry-stop)`。
- LONG 必须 `stop<entry`，SHORT 必须 `stop>entry`；否则 `STOP_SIDE_INVALID`。
- 只允许 `0.5×ATR <= risk_per_unit <=2.0×ATR`；两个等号都通过，超出为 `STOP_DISTANCE_INVALID`。
- 保存 raw entry、raw stop、buffer、每步舍入方向、invalidation、ATR、tick 和 normalized risk。

## 5. 结构目标输入

- 只使用确认时已经生效的 PivotZone：`zone.confirmed_at <= confirmation_close`。
- 15m zone 的最后成员 Pivot 必须位于确认时最新 96 根已收盘 15m 内；1H zone 必须位于最新 60 根已收盘 1H 内。
- LONG 只使用 HIGH/resistance zone；SHORT 只使用 LOW/support zone。
- LONG 保守目标 raw price 为 `zone.lower - tick_size`，再向下对齐 tick。
- SHORT 保守目标 raw price 为 `zone.upper + tick_size`，再向上对齐 tick。
- 舍入后不严格位于盈利方向的 zone 丢弃。
- 目标按“距 Entry 的可实现距离升序、confirmed_at 降序、15m 优先于 1H、zone_id 升序”稳定排序。

## 6. 前方障碍与 TP

令 `R=risk_per_unit`，结构目标距离取趋势方向上的绝对可实现距离。

1. 若最近的有效结构目标距离 `<1R`，拒绝计划：`OBSTACLE_LT_1R`；等于 `1R` 允许。
2. TP1 使用最近的 `>=1R` 结构目标；不存在时使用扩展距离 `max(1R, 1×ATR)`。
3. TP2 使用不同 zone 且严格远于 TP1 的最近 `>=2R` 结构目标；不存在时使用扩展距离 `max(2R, 2×ATR)`。
4. 扩展 LONG 目标向下对齐 tick，SHORT 向上对齐 tick，保持利润估计保守。
5. 同一个 zone ID 不得同时作为 TP1/TP2；相同目标价不能充当两个目标。
6. 若扩展 TP2 舍入后不严格远于 TP1，不自行增加未冻结缓冲，拒绝为 `TP_PLAN_INVALID`。
7. 最终 TP1 距离必须 `>=1R`、TP2 距离必须 `>=2R` 且 TP2 严格远于 TP1；否则 `TP_PLAN_INVALID`。等于最低 R 边界允许。

来源枚举：`STRUCTURE_15M`、`STRUCTURE_1H`、`ATR_EXTENSION`。计划保存 zone ID、raw/rounded target、gross RR 和选择/丢弃证据。

## 7. Score 输入

Score 只接受已经通过全部价格计划 Gate 的不可变证据：

- P4 FourHourEvaluation、DailyEvaluation、1H Pullback state。
- 4H 方向归一化 SMA180 slope（LONG 为 `change/ATR`，SHORT 为 `-change/ATR`）。
- 1H close 相对 SMA30/SMA60 的位置与前三根/此前 20 根 volume contraction 布尔值。
- P5 primary trigger、volume evidence，以及 Trigger B 的 sweep depth/zone reclaim 证据。
- P6 risk、TP 来源和 gross RR。

输入证据与已通过计划矛盾时 Fail Closed，不输出 Score。

## 8. Score 决策表

### 4H，最多 25

- 完整正式方向：15。
- band width：`>=1.50` 得 5；`>=1.00` 得 4；`>=0.75` 得 3；否则 0。
- 顺方向 normalized slope：`>=0.50` 得 5；`>=0.25` 得 4；`>0.10` 得 3；否则 0。`0.10` 等号不得 3 分。

### 1D，最多 15

- ALIGNED：15；MIXED：7；BLOCK 属于上游失败，不评分。

### 1H，最多 25

- SHALLOW：12；STANDARD：10。
- close 严格回到 SMA30 趋势侧：8；否则位于 SMA30–SMA60 闭区间：6；更深但仍为有效 SHALLOW/STANDARD：4。
- 最近 3 根 quote volume 均值严格小于此前 20 根中位数：5；等号为 0。

### 15m，最多 20

- 按 primary trigger 评分：A contraction 成立得 5；B sweep depth `<=0.50 ATR` 且收回整个 zone 得 5。A+B 使用 primary B，不叠加两者。
- `trigger_volume / baseline >=1.50` 得 5；`>1.00` 得 3；否则 0。
- trigger volume 严格高于上一根得 2。
- LONG close_location `>=0.80`、SHORT `<=0.20` 得 4；否则通过基础 0.65/0.35 Gate 得 2。
- primary trigger 的严格结构 reclaim 完成得 4。

### 风险收益，最多 15

- normalized risk 位于闭区间 `[0.75,1.25]` 得 5；其余已合格 `[0.5,2.0]` 得 3。
- TP1：结构来源 4，ATR extension 2。
- TP2：结构来源 4，ATR extension 2。
- TP2 gross RR `>=3.0` 得 2，否则 0。

输出整数 `score_total` 以及固定五项：`four_hour`、`daily`、`one_hour`、`fifteen_minute`、`risk_reward`。总分必须等于五项之和且在 0–100；不设置拒绝门槛，不描述成胜率。

## 9. 输出与确定性

TradePlan 至少保存：

- 版本、logical signal ID、symbol、direction、primary trigger。
- tick、ATR、raw/rounded Entry、invalidation、buffer、raw/rounded Stop、risk。
- TP1/TP2、来源、zone ID、gross RR。
- confirmation close、TTL、expires_at、完整 reason codes 与 evidence。
- `accepted`；只有 accepted plan 才有正式 Score。

相同 Decimal 输入、zone 集合和参数必须产生完全相同的计划、Score 与 JSON。P6 不调用网络、数据库、系统当前时间或随机数。

## 10. 验收矩阵

- GS-033：非网格价格的 LONG/SHORT 保守舍入。
- GS-034：0.5/2.0 ATR 风险等号允许，越界拒绝。
- GS-035：最近障碍 0.99R 拒绝，1R 等号允许。
- GS-036：无结构目标时 ATR extension。
- 非十进制 tick、Stop side、无效 ATR/tick、TP 舍入后低于最低 R。
- 结构目标确认时点、15m/1H 回看、同 zone 禁止复用与稳定 tie-break。
- Score 每个阈值的等号上下边界、五项上限、总分 0–100、无门槛。
- LONG/SHORT 价格轴镜像与重复 JSON 完全一致。

P6.1 只冻结以上契约；P6.2 才开始实现 Entry 与 Stop。
