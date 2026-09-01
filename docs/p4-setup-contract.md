# P4 Setup Context Contract

状态：P4 实施契约  
依据：冻结的策略规则、决策表与 P3 指标契约

## 1. 范围

P4 只计算多周期 Setup 上下文：

```text
4H direction/regime
→ 1D context
→ 1H pullback snapshot/episode
```

P4 不读取 15m，不创建 Trigger、Entry、Signal、Score 或 Paper Trade。

## 2. 通用输入

- 每个周期输入独立的已收盘 Candle 与同索引 P3 indicator frame。
- Candle 必须按 UTC `open_time` 严格升序、唯一且全部 `is_closed=true`。
- SMA、ATR、TR 等当前判断所需值必须有限。
- ATR 必须 `>0`；否则 Fail Closed。
- 输入不足时抛出 `SetupNotReadyError`，数据非法时抛出 `SetupInputError`。
- 所有函数无网络、数据库、系统当前时间或随机数依赖。

## 3. 4H Direction

最新行 `t`：

```text
LONG:
SMA30 > SMA60 > SMA90 > SMA180
and SMA180[t] > SMA180[t-5]

SHORT:
SMA30 < SMA60 < SMA90 < SMA180
and SMA180[t] < SMA180[t-5]
```

任一相等、排列混合或斜率不符均为 NEUTRAL。

每行 MA order：

- 严格多头：BULL_ORDER。
- 严格空头：BEAR_ORDER。
- 其他：MIXED_ORDER。

每行 close 相对整个 MA band：

- close 严格高于四条 MA 最大值：ABOVE_BAND。
- close 严格低于四条 MA 最小值：BELOW_BAND。
- 其他或等于边界：INSIDE_BAND。

## 4. 4H Regime

使用最近 12 根已收盘 4H：

```text
band_width = abs(SMA30[t] - SMA180[t]) / ATR14[t]
sma180_change = SMA180[t] - SMA180[t-5]
```

以下任一成立即 COMPRESSED，并保留全部原因：

1. `band_width < compression_threshold`，默认 0.75；等于不触发。
2. `abs(sma180_change) <= 0.10 × ATR14[t]`；等于触发。
3. 最近 12 根 MA order 相邻变化次数 `>=3`。
4. 去除 INSIDE_BAND 后，ABOVE/BELLOW 相邻翻转次数 `>=2`。

只允许 compression threshold 0.50、0.75、1.00。

Direction 与 Regime 独立保存：可出现 direction=LONG、regime=COMPRESSED，但这种组合不得继续形成正式 Setup。

## 5. 1D Context

输入 4H 的 LONG 或 SHORT：

- LONG 遇到完整 1D 空头且 SMA180 下降：BLOCK_LONG。
- SHORT 遇到完整 1D 多头且 SMA180 上升：BLOCK_SHORT。
- 1D 完整排列和斜率与 4H 同向：ALIGNED。
- 其他完整但斜率不符或混合排列：MIXED。

NEUTRAL 不进入 1D 评估。

## 6. 1H Pullback Snapshot

LONG 首先要求当前 `SMA30>SMA60`，SHORT 镜像。

状态严格按以下优先级，命中即停止：

### LONG

1. low `<=SMA180`：RESET。
2. close `<SMA90`：DAMAGED。
3. range 与 SMA60–SMA90 闭区间相交：STANDARD。
4. range 与 SMA30–SMA60 闭区间相交：SHALLOW。
5. close `>SMA30` 且 `close-SMA30 <=0.50×ATR14`：WATCHING。
6. 其他：NONE。

### SHORT

1. high `>=SMA180`：RESET。
2. close `>SMA90`：DAMAGED。
3. range 与 SMA60–SMA90 闭区间相交：STANDARD。
4. range 与 SMA30–SMA60 闭区间相交：SHALLOW。
5. close `<SMA30` 且 `SMA30-close <=0.50×ATR14`：WATCHING。
6. 其他：NONE。

同一 Candle 同时触及 STANDARD/SHALLOW 时 STANDARD 优先。wick 越过 SMA90 但 close 未越过，不构成 DAMAGED。

## 7. 放量加速过滤

使用最近两根已收盘 1H 和它们之前恰好 20 根：

- LONG adverse：两根均 `close<open`。
- SHORT adverse：两根均 `close>open`。
- 两根 quote volume 均严格高于此前 20 根中位数。
- 第二根 TR 严格高于第一根 TR。

四项全部成立才是 `PULLBACK_ACCELERATING`。等于 baseline 或 TR 相等均不触发。

## 8. Episode

- 仅当前状态为 SHALLOW/STANDARD 时创建或延续 episode。
- 从当前行向前扫描连续 SHALLOW/STANDARD，最早一行是 episode start。
- episode start 的紧邻前一根 1H close 必须位于当时 SMA30 的趋势一侧；否则 `PULLBACK_EPISODE_NOT_NEW`。
- LONG 趋势一侧为 close>SMA30；SHORT 为 close<SMA30。
- Episode ID 使用 strategy version、symbol、direction 和 episode start open_time 的 canonical SHA-256。
- DAMAGED、RESET、NONE、WATCHING 不返回 episode ID。

只有 SHALLOW/STANDARD、非 accelerating、MA direction 有效且 episode new 才 `eligible_for_trigger=true`。

## 9. 计算短路

整合函数按顺序短路：

1. 4H NEUTRAL：不计算 1D/1H。
2. 4H COMPRESSED：不计算 1D/1H。
3. 1D BLOCK：不计算 1H。
4. 其他情况才计算 1H。

被短路的结果使用 `None`，不能伪造 MIXED 或 NONE。

## 10. 版本

- Setup context version：`setup-context/0.1.0`。
- 相同输入与 compression threshold 必须产生完全相同的枚举、计数、原因和 episode ID。
- 本阶段不持久化状态转换；只输出可重放的时间点快照。
