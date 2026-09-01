# Strategy Rules V0.1

状态：P0 已验收冻结  
策略版本：`ma-trend-pullback/0.1.0`  
适用范围：Binance USDⓈ-M USDT 永续合约离线研究  
基准时区：UTC

## 1. 规则优先级

1. 本文件把产品规格中的自然语言规则量化为可编码定义。
2. `CRYPTO_AGENT_DEVELOPMENT_SPEC.md` 决定产品边界；本文件决定 V0.1 的计算语义。
3. 本文件冻结后，任何会改变历史信号的修改都必须提升策略版本，不能覆盖旧结果。
4. 未在本文件定义、输入不足或数据校验失败时，一律不生成正式信号。

## 2. 时间与数值语义

- 所有时间使用带 UTC 时区的时间戳。
- K 线区间采用左闭右开 `[open_time, close_time_exclusive)`。
- Binance 的 `close_time` 若为区间末尾减 1 ms，规范化后另存为 `close_time_exclusive`。
- 策略在时间 `T` 运行时，只能读取 `close_time_exclusive <= T` 的 K 线。
- SMA 使用当前及此前已经收盘的 close，窗口不足时返回 `NOT_READY`。
- ATR(14) 使用 Wilder 定义：TR 的前 14 个有效值以算术平均初始化，之后使用 RMA；窗口不足时返回 `NOT_READY`。
- Quote Volume 使用 Binance Kline 的 quote asset volume 字段。
- 指标计算使用 `float64`；Entry、SL、TP、tickSize 和价格比较边界转换为 `Decimal`。
- 价格相等视为“触及”；“突破”必须严格大于，“跌破”必须严格小于。
- 所有方向规则先定义 LONG，SHORT 必须是价格轴镜像，不允许另写不同语义。

## 3. 冻结参数

以下默认值来自产品规格允许的默认候选，不在 P0 扩大搜索空间：

| 参数 | V0.1 默认值 | 允许的单变量敏感度值 |
|---|---:|---|
| Pivot 左/右窗口 | 2/2 | 2/2、3/3 |
| MA 压缩阈值 | 0.75 ATR | 0.50、0.75、1.00 |
| Stop ATR 缓冲 | 0.15 ATR | 0.10、0.15、0.20 |
| Entry 有效期 | 4 根 15m | 2、4、6 |
| 最大持仓 | 32 根 15m | 16、32、48 |

以下是为消除自然语言歧义而冻结的语义常量，不进行收益寻优；修改时必须创建新策略版本：

| 常量 | 值 |
|---|---:|
| 4H regime 观察窗口 | 12 根已收盘 4H |
| SMA180 斜率窗口 | 5 根 4H |
| SMA180 近水平阈值 | 5 根变化绝对值 `<= 0.10 × 4H ATR14` |
| MA 顺序频繁交换 | 12 根内 regime 标签变化 `>= 3` 次 |
| 全带穿越反复发生 | 12 根内带外方向翻转 `>= 2` 次 |
| 15m Pivot/结构回看 | 96 根已收盘 15m |
| 1H Pivot/结构回看 | 60 根已收盘 1H |
| WATCHING 接近距离 | `0.50 × 1H ATR14` |

## 4. 4H 正式方向与市场状态

在最新已收盘 4H K 线 `t`：

```text
LONG:
SMA30_t > SMA60_t > SMA90_t > SMA180_t
and SMA180_t > SMA180_(t-5)

SHORT:
SMA30_t < SMA60_t < SMA90_t < SMA180_t
and SMA180_t < SMA180_(t-5)
```

其他情况为 `NEUTRAL`。

`band_width = abs(SMA30_t - SMA180_t) / ATR14_t`。ATR 为 0 或缺失时状态为 `DATA_INVALID`。

每根 4H K 线还计算以下标签：

- `BULL_ORDER`：四条 SMA 严格多头排列。
- `BEAR_ORDER`：四条 SMA 严格空头排列。
- `MIXED_ORDER`：其他排列。
- `ABOVE_BAND`：close 严格高于四条 SMA 最大值。
- `BELOW_BAND`：close 严格低于四条 SMA 最小值。
- `INSIDE_BAND`：其他位置。

`COMPRESSED` 在以下任一条件成立时触发：

1. `band_width < compression_threshold`。
2. `abs(SMA180_t - SMA180_(t-5)) <= 0.10 × ATR14_t`。
3. 最近 12 根 4H 的 order 标签相邻变化次数至少为 3。
4. 最近 12 根去除 `INSIDE_BAND` 后，`ABOVE_BAND` 与 `BELOW_BAND` 的相邻翻转次数至少为 2。

`COMPRESSED`、`NEUTRAL`、`DATA_INVALID` 均禁止正式 Setup。阈值相等时不按第 1 条压缩，但仍需通过其他三条。

## 5. 1D 环境过滤

使用最新已收盘 1D K 线：

- 完整空头排列且 SMA180 低于 5 根 1D 前时，状态为 `BEAR_BLOCK_LONG`。
- 完整多头排列且 SMA180 高于 5 根 1D 前时，状态为 `BULL_BLOCK_SHORT`。
- 与 4H 同方向完整排列时为 `ALIGNED`。
- 不完整排列时为 `MIXED`。

`BEAR_BLOCK_LONG` 阻止 LONG，`BULL_BLOCK_SHORT` 阻止 SHORT。`MIXED` 不阻止，但降低 Score。1D 窗口不足时 Fail Closed，不生成正式信号。

## 6. 1H 回踩

### 6.1 回踩区域

对 LONG，必须先满足 4H LONG、非 COMPRESSED、1D 未阻止，且最新已收盘 1H 的 `SMA30 > SMA60`。

LONG 区域：

```text
SHALLOW zone = [SMA60, SMA30]
STANDARD zone = [min(SMA90, SMA60), max(SMA90, SMA60)]
```

SHORT 镜像。K 线 range 与闭区间有任何重叠即为“进入区域”。如果同一根 K 线同时触及两个区域，按更深的 `STANDARD` 归类。

### 6.2 状态优先级

LONG 使用以下从高到低优先级：

1. `RESET`：已收盘 1H 的 low `<= SMA180`。
2. `DAMAGED`：已收盘 1H 的 close `< SMA90`。这就是“有效跌破”；wick 跌破但收盘站回不算 DAMAGED。
3. `STANDARD`：range 与 STANDARD zone 重叠。
4. `SHALLOW`：range 与 SHALLOW zone 重叠。
5. `WATCHING`：价格尚未触区，close 位于趋势一侧，且距最近区域边界 `<= 0.50 × 1H ATR14`。
6. `NONE`：其他。

SHORT 完全镜像。只有 `SHALLOW`、`STANDARD` 可以进入 15m Trigger 评估。

### 6.3 回踩 episode

- episode 从首根被归类为 SHALLOW 或 STANDARD 的已收盘 1H K 线开始。
- 该 K 线之前至少一根已收盘 1H 的 close 必须位于 SMA30 的趋势一侧，避免把长期黏在线内的行情当成新回踩。
- episode ID 为方向、symbol 和首根回踩 1H open_time 的确定性组合。
- 每次新的 1H 收盘重新评估；变为 DAMAGED、RESET、方向失效、1D 阻止或数据异常时立即失效。
- 最新 1H 不再是 SHALLOW/STANDARD 时，不再创建新的 15m Trigger；已经 ARMED 的信号按入场前失效规则处理。

### 6.4 放量加速回调

LONG 的“放量加速下跌”定义为：最近两根已收盘 1H 都是 `close < open`，两根 quote volume 均严格大于这两根之前 20 根的 quote volume 中位数，且第二根 TR 严格大于第一根 TR。SHORT 镜像为连续上涨。

满足该定义时，1H 回踩不可进入 Trigger。历史不足 22 根时为 `NOT_READY`。

## 7. 15m 通用量价定义

设候选确认 K 线为 `t`：

```text
pullback_mean = mean(quote_volume[t-3 : t])
pullback_baseline = median(quote_volume[t-23 : t-3])
trigger_baseline = median(quote_volume[t-20 : t])
close_location = (close_t - low_t) / (high_t - low_t)
```

切片右端不包含 `t`。若 `high_t == low_t`，量价确认失败。

通用确认：

- 触发量 `quote_volume_t > trigger_baseline`。
- 触发量 `quote_volume_t > quote_volume_(t-1)`。
- LONG：`close_location >= 0.65`；SHORT：`close_location <= 0.35`。
- 放量但收盘位置不满足时，明确记录 `VOLUME_WITH_ADVERSE_CLOSE` 并拒绝。

Trigger A 还要求 `pullback_mean < pullback_baseline`。Trigger B 的 Sweep 本身允许放量，因此不要求前三根缩量，但仍记录该统计供后续分组。

## 8. Trigger A — MA Reclaim

LONG 必须全部满足：

1. 存在当前有效的 1H SHALLOW/STANDARD episode。
2. 候选 K 线之前最近 4 根已收盘 15m 内，至少一根 close `< SMA30`，或其 range 与当前 1H 回踩区域重叠。
3. 候选 K 线 close `> 15m SMA30`。
4. 候选 K 线 close `> 前一根 15m high`；V0.1 删除“等价确认结构”，不做主观替代。
5. 满足第 7 节通用确认和 Trigger A 缩量要求。
6. 存在 episode 内、候选 K 线确认时已经生效的 15m Pivot Low，供结构止损使用。

候选 K 线收盘后创建一条 `ARMED` 信号。SHORT 镜像。

## 9. Trigger B — Liquidity Sweep + MA Reclaim

### 9.1 Pivot

- Pivot Low：中心 K 线 low 严格小于左侧 2 根 low，且小于或等于右侧 2 根 low；至少一侧比较必须严格，避免全等平线重复 Pivot。
- Pivot High 镜像。
- Pivot 的 `confirmed_at` 为右侧第 2 根 K 线收盘时间。
- Sweep K 线开盘前未确认的 Pivot 不可使用。
- 3/3 只作为允许的单变量敏感度版本，不能与其他参数做笛卡尔积搜索。

### 9.2 结构区域合并

- 同类型、同周期的相邻 Pivot 价格距离 `< 0.2 × 该后一个 Pivot 确认时的 ATR14` 时合并。
- zone 下界为成员价格最小值，上界为成员价格最大值。
- `confirmed_at` 取最后一个成员 Pivot 的确认时间，成员列表不可丢弃。
- 15m Sweep 只看最近 96 根中的 zone；若多个可用，优先 `confirmed_at` 最新者，再取距 sweep 前一根 close 最近者。
- LONG 支撑 zone 在 Sweep 前若已有 15m close 严格低于 zone 下界，则视为已破坏，不再作为 Sweep 参考；SHORT 镜像。

### 9.3 Sweep 确认

LONG 必须全部满足：

1. 存在当前有效的 1H SHALLOW/STANDARD episode。
2. 候选 15m 的 low `< 支撑 zone 下界`。
3. 候选 15m 的 close `> 支撑 zone 上界`，使用整个合并区域的保守 reclaim。
4. 候选 15m 的 close `> 15m SMA30`。
5. 满足第 7 节通用量价确认；不强制前三根缩量。
6. Sweep 极值保存为结构失效点。

候选 K 线是确认 K 线。其收盘后创建 `ARMED`，Entry 位于其 high 上方 1 tick；后续 1m 穿越该价格即实现规格中的“随后突破确认 K 线高点”。SHORT 镜像。

同一 episode、symbol、direction、确认 K 线同时满足 A/B 时只创建一条信号：主触发为 `SWEEP_RECLAIM`，附加原因包含 `MA_RECLAIM`。

## 10. Entry、失效、过期与错过

### 10.1 Entry

- LONG raw entry = 确认 15m high + 1 tick，向上对齐合法 tick。
- SHORT raw entry = 确认 15m low - 1 tick，向下对齐合法 tick。
- 最早从确认 K 线收盘后的第一根 1m 开始判定成交。
- 有效窗口为 `[confirmation_close, confirmation_close + 4 × 15m)`；右端时刻不再成交。

### 10.2 入场前失效

- Trigger A LONG 的结构失效点是 episode 内最近一个、在确认时已生效的 Pivot Low；SHORT 镜像。
- Trigger B LONG 的结构失效点是 Sweep low；SHORT 镜像。
- 入场前先触及或越过结构失效点则 `INVALIDATED`。
- 同一 1m 同时可能 Entry 和失效时，优先 Aggregate Trades 重建顺序；不可用时采用对交易者不利的“先成交、后止损”，因此会创建交易而不是无损 INVALIDATED。

### 10.3 MISSED

- 若第一根能够成交的 1m open 已越过 Entry，gap = 趋势方向上的 `abs(open-entry)`。
- `gap > 0.15 × 确认时 15m ATR14` 时为 `MISSED`。
- 等于阈值时允许成交，模拟成交价使用该 1m open 并叠加成本。
- 若 1m 从 Entry 有利一侧正常穿越，理论成交价为 Entry。

### 10.4 EXPIRED

有效窗口右端到达时仍未成交、未失效、未错过，则标记 `EXPIRED`。

## 11. Stop Loss

```text
buffer = max(0.15 × confirmation_15m_ATR14, 2 × tickSize)
```

- Trigger A LONG：raw stop = 结构 Pivot Low - buffer，向下对齐 tick。
- Trigger B LONG：raw stop = Sweep low - buffer，向下对齐 tick。
- SHORT 镜像并向上对齐 tick。
- `risk = abs(entry - stop)`。
- 仅当 `0.5 × ATR14 <= risk <= 2.0 × ATR14` 时继续；边界相等允许。

## 12. Take Profit

### 12.1 结构目标

- 使用确认时已经生效的 15m Pivot zone（96 根回看）和 1H Pivot zone（60 根回看）。
- LONG 目标为 Entry 上方的 Pivot High/resistance zone；保守目标价为 zone 下界减 1 tick。
- SHORT 目标为 Entry 下方的 Pivot Low/support zone；保守目标价为 zone 上界加 1 tick。
- 舍入后不在盈利方向的候选直接丢弃。
- 同一区域不能同时充当 TP1 和 TP2。

### 12.2 前方障碍

最近结构目标的可实现价格距离 `< 1R` 时拒绝信号；等于 1R 时允许。

### 12.3 目标选择

- TP1：最近的、可实现距离 `>= 1R` 的结构目标；不存在时使用趋势方向上的 `max(1R, 1 × ATR14)` 扩展目标。
- TP2：TP1 之外最近的、可实现距离 `>= 2R` 的结构目标；不存在时使用趋势方向上的 `max(2R, 2 × ATR14)` 扩展目标。
- 扩展目标按对交易者保守的方向对齐 tick。
- 最终必须满足 TP1 `>= 1R`、TP2 `>= 2R`；否则拒绝。

默认管理为 TP1 50%、TP2 50%。TP1 后的剩余仓位止损从下一根 1m 开始移动到覆盖已知手续费与滑点的保本价。

## 13. 时间退出

- 从成交所在 15m 区间开始计数，满 32 个完整 15m 区间时触发 `TIME_EXIT`。
- 使用截止时刻后第一根 1m open 作为理论市价，并加入不利成本。
- 若截止前最后一分钟同时触及 TP/SL，先按真实 Aggregate Trades；不可用时采用不利结果。

## 14. Signal Score（只排序）

强制条件先判定，任何强制条件失败时都不得因 Score 生成信号。Score 不是概率。

### 14.1 4H（最多 25）

- 完整方向排列：15（强制条件）。
- band width：`>=1.50` 得 5；`>=1.00` 得 4；`>=0.75` 得 3。
- 5 根 SMA180 归一化斜率：`>=0.50 ATR` 得 5；`>=0.25` 得 4；`>0.10` 得 3。

### 14.2 1D（最多 15）

- 同向完整排列：15。
- MIXED：7。
- 强反向：阻止，不评分。

### 14.3 1H（最多 25）

- SHALLOW：12；STANDARD：10。
- 回踩 K 线 close 已回到 SMA30 趋势一侧：8；位于 SMA30–SMA60：6；更深但未 DAMAGED：4。
- 最近 3 根 1H quote volume 均值低于此前 20 根中位数：5；否则 0。

### 14.4 15m（最多 20）

- A 的前三根缩量成立：5；B 的 sweep 深度不超过 `0.50 ATR` 且收回整个 zone：5；否则 0。
- 触发量/此前 20 根中位数：`>=1.50` 得 5；`>1.00` 得 3。
- 触发量高于上一根：2。
- LONG close_location `>=0.80`（SHORT `<=0.20`）：4；通过基本 0.65/0.35 门槛：2。
- 完成严格结构 reclaim：4。

### 14.5 风险收益（最多 15）

- Stop 距离在 `[0.75, 1.25] ATR`：5；其余合格范围：3。
- TP1 来自结构：4；来自扩展：2。
- TP2 来自结构：4；来自扩展：2。
- TP2 可实现距离 `>=3R`：2；否则 0。

总分保留整数及五大项拆分。V0.1/V0.2 不设置 Score 拒绝门槛。

## 15. 状态与确定性 ID

- `WATCHING`：满足大周期条件，1H 为 WATCHING/SHALLOW/STANDARD，但尚未确认 Trigger。
- `ARMED`：15m Trigger 已确认，等待 Entry。
- `TRIGGERED`：1m 成交已发生并创建 Paper Trade。
- `INVALIDATED`：入场前结构失效且未发生成交。
- `EXPIRED`：有效期结束未成交。
- `MISSED`：gap 超过追价限制。
- `CLOSED`：Paper Trade 终结。

逻辑 signal ID 使用以下字段的规范字符串计算 SHA-256：

```text
strategy_version | parameter_version | universe_version |
symbol | direction | episode_id | confirmation_15m_open_time
```

状态转换只能前进，重复应用同一事件必须无副作用。每次转换保存事件时间、输入 K 线时间和 reason code。

## 16. 成本语义

- 无成本：fee=0、slippage=0、funding=0，仅作参考。
- 基准：每次成交 6 bps fee + 2 bps slippage，并计实际 Funding。
- 压力：每次成交 6 bps fee + 5 bps slippage，并计实际 Funding。
- 成本逐次成交计算；TP1 和 TP2 是两个独立退出成交。
- Fee 与 slippage 始终向交易者不利方向应用。
- Funding 缺失且持仓跨 Funding 时，该交易不得进入净收益正式统计，标记 `FUNDING_DATA_MISSING`。

## 17. 参数与规则变更纪律

- 只允许对第 3 节列出的五类参数逐项做敏感度测试。
- 第 3 节语义常量、第 4–16 节算法有任何修改，必须创建新策略版本。
- 修改后不得复用旧策略的锁定测试结果。
- 不添加 RSI、MACD、ADX、新闻、情绪、链上或 LLM 方向判断。
