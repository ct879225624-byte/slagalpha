# P3 Indicator Contract

状态：P3 实施契约  
依据：冻结的 `strategy-rules-v0.1.md` 与 `data-contracts-v0.1.md`

## 1. 范围

P3 只提供确定性指标和结构事件：

- SMA30/60/90/180。
- Wilder ATR14。
- Quote Volume 前置窗口统计。
- 确认型 Pivot 2/2，并允许单变量敏感度 3/3。
- 同类型 Pivot 的结构区域合并。

P3 不解释趋势、回踩或交易方向，不创建 Setup、Signal 或 TradePlan。

## 2. 输入契约

- 输入为一个 symbol、一个 interval 的已收盘 Candle，按 `open_time` 严格升序。
- `open_time` 必须唯一，`close_time_exclusive` 必须严格升序。
- high、low、close、quote_volume 必须是有限数值。
- high/low/close 缺失或非有限时整个计算 Fail Closed，不用前值填充。
- 指标核心不读取系统时间、网络、数据库或未来 K 线。
- 输出与输入保持相同行索引；warm-up 不足使用 `NaN`，不是 0。

## 3. SMA

```text
SMA_n[t] = mean(close[t-n+1 : t+1])
```

- 当前已收盘 Candle 包含在窗口内。
- `min_periods=n`。
- 只允许 30、60、90、180 四个正式窗口。
- 第 `n-1` 个零基索引首次产生有效值。

## 4. True Range 与 Wilder ATR14

```text
TR[0] = high[0] - low[0]
TR[t] = max(
  high[t] - low[t],
  abs(high[t] - close[t-1]),
  abs(low[t] - close[t-1])
)

ATR14[13] = mean(TR[0:14])
ATR14[t] = (ATR14[t-1] × 13 + TR[t]) / 14
```

- 前 13 行 ATR 为 `NaN`。
- 不是 pandas `ewm` 的自由变体，必须严格按上述种子和递推计算。
- ATR=0 可以被计算并保存；后续策略 Gate 负责将其标记为无效风险尺度。

## 5. Quote Volume 统计

候选 Candle 为 `t` 时输出：

```text
quote_volume_previous[t] = volume[t-1]
quote_volume_mean3_prev[t] = mean(volume[t-3:t])
quote_volume_median20_prev[t] = median(volume[t-20:t])
quote_volume_median20_before_prev3[t] = median(volume[t-23:t-3])
```

- 所有切片右端不包含 `t`。
- 窗口必须完整，否则为 `NaN`。
- 不允许把候选触发 Candle 的 volume 放入 baseline。

## 6. Pivot 事件

默认 `left=2, right=2`。

Pivot Low 的中心索引 `c`：

```text
low[c] < 每一个左侧 low
and low[c] <= 每一个右侧 low
```

Pivot High 镜像：

```text
high[c] > 每一个左侧 high
and high[c] >= 每一个右侧 high
```

- 事件只在索引 `c+right` 收盘后发出。
- `pivot_time` 是中心 Candle open_time。
- `confirmed_at` 是第 `right` 根右侧 Candle 的 close_time_exclusive。
- `atr_at_confirmation` 取确认 Candle 的 ATR14，不取中心 Candle ATR。
- 确认 ATR 缺失时不创建可合并的正式 Pivot 事件，记录为 `NOT_READY` 测试结果。
- 同一中心 Candle 若同时满足 High/Low，允许产生两个不同类型事件。
- Pivot ID 由 interval、类型、中心时间、确认时间和价格生成确定性 SHA-256。

截断输入到任意时点 `T`，不得出现 `confirmed_at > T` 的 Pivot；补充未来数据不得改变此前已经确认的 Pivot 内容。

## 7. 结构区域合并

- HIGH 与 LOW 分开处理，不互相合并。
- 每种类型按 `confirmed_at`、`pivot_time`、pivot ID 稳定排序。
- 新 Pivot 只与当前 zone 的最后一个成员 Pivot 比较。
- 使用后一个 Pivot 的确认时 ATR：

```text
abs(new.price - previous_member.price) < 0.2 × new.atr_at_confirmation
```

- 严格小于才合并；等于阈值时创建新 zone。
- zone lower/upper 为全部成员价格的 min/max。
- zone confirmed_at 为最后一个成员的 confirmed_at。
- 成员 Pivot ID 按合并顺序完整保存。
- Zone ID 由 interval、类型和成员 ID 列表计算确定性 SHA-256。

P3 不负责 96 根/60 根回看过滤、zone 被收盘破坏或 Sweep 选择；这些属于后续策略阶段。

## 8. 输出与版本

- 指标算法版本：`indicators/0.1.0`。
- Pivot 算法版本：`confirmed-pivot/0.1.0`。
- Zone 合并版本：`pivot-zone/0.1.0`。
- 数值指标使用 float64。
- 输出不得覆盖 Candle 权威字段。
- 相同输入与参数重复执行，数值（含 NaN 位置）、Pivot ID、Zone ID 必须一致。
