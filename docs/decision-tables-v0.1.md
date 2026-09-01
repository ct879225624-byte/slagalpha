# Decision Tables V0.1

状态：P0 已验收冻结  
依赖：`docs/strategy-rules-v0.1.md`  
目的：固定计算顺序、短路条件、输出和 reason codes

## 1. 单次 15m 扫描顺序

同一 `symbol + 15m close_time` 必须严格按下表执行。任一阻断项出现后停止创建新信号，但仍保存扫描结果和 reason code。

| 顺序 | Gate | 输入 | 通过输出 | 阻断输出 |
|---:|---|---|---|---|
| 1 | G01 数据完整 | 多周期 Candle、规则、版本 | `DATA_OK` | `DATA_NOT_READY` / `DATA_INVALID` |
| 2 | G02 合约合格 | universe snapshot | `SYMBOL_ELIGIBLE` | `SYMBOL_INELIGIBLE` |
| 3 | G03 4H 方向 | 4H SMA/ATR | LONG 或 SHORT | `DIRECTION_NEUTRAL` |
| 4 | G04 压缩过滤 | 12 根 4H regime | `REGIME_TRENDING` | `REGIME_COMPRESSED` |
| 5 | G05 1D 过滤 | 1D SMA/斜率 | ALIGNED/MIXED | `DAILY_OPPOSITION` |
| 6 | G06 1H 回踩 | 1H SMA/ATR/volume | SHALLOW/STANDARD episode | `PULLBACK_NONE` / `PULLBACK_DAMAGED` / `PULLBACK_RESET` / `PULLBACK_ACCELERATING` |
| 7 | G07 15m Trigger | 15m MA/volume/pivots | A、B、A+B 或 NONE | `TRIGGER_NONE` |
| 8 | G08 去重 | 现有 signal/trade | 单一逻辑信号 | `DUPLICATE_SIGNAL` / `ACTIVE_TRADE_EXISTS` |
| 9 | G09 Entry/SL | tickSize、ATR、结构点 | 合法 Entry/SL/R | `PRICE_RULE_INVALID` / `STOP_DISTANCE_INVALID` |
| 10 | G10 TP/RR | 结构目标、ATR | TP1/TP2 | `OBSTACLE_LT_1R` / `TP_PLAN_INVALID` |
| 11 | G11 Score | 各 Gate 证据 | 0–100 排序分 | 不阻断 |
| 12 | G12 持久化结果 | 版本与全部输入引用 | WATCHING 或 ARMED 事件 | `PERSISTENCE_FAILED`，本轮 Fail Closed |

## 2. 数据 Gate

| 条件 | 决策 | reason code |
|---|---|---|
| 任一必需周期窗口不足 | 不评估 | `DATA_NOT_READY` |
| 最新正式 Candle 尚未收盘 | 忽略该 Candle | `OPEN_CANDLE_IGNORED` |
| 唯一键重复且内容相同 | 去重并记录 | `DUPLICATE_CANDLE_IDENTICAL` |
| 唯一键重复但内容冲突 | 禁止该 symbol 本轮判断 | `DUPLICATE_CANDLE_CONFLICT` |
| 时间网格缺口 | 禁止该 symbol 本轮判断 | `CANDLE_GAP` |
| open/high/low/close 关系非法 | 禁止该 symbol 本轮判断 | `OHLC_INVALID` |
| quote volume < 0 或非有限值 | 禁止该 symbol 本轮判断 | `VOLUME_INVALID` |
| ATR 缺失、非有限或 <= 0 | 禁止该 symbol 本轮判断 | `ATR_INVALID` |
| tickSize 缺失或 <= 0 | 禁止创建交易计划 | `TICK_SIZE_INVALID` |

## 3. 4H 方向表

| SMA 排列 | SMA180 对比 t-5 | 初始方向 |
|---|---|---|
| 30 > 60 > 90 > 180 | 上升 | LONG |
| 30 < 60 < 90 < 180 | 下降 | SHORT |
| 任一相等 | 任意 | NEUTRAL |
| 混合 | 任意 | NEUTRAL |
| 多头排列 | 平或下降 | NEUTRAL |
| 空头排列 | 平或上升 | NEUTRAL |

得到 LONG/SHORT 后再判断 COMPRESSED：

| 任一条件 | 状态 | reason code |
|---|---|---|
| band width < threshold | COMPRESSED | `MA_BAND_NARROW` |
| 5 根 SMA180 变化绝对值 <= 0.10 ATR | COMPRESSED | `MA180_FLAT` |
| 12 根 order 标签变化 >= 3 | COMPRESSED | `MA_ORDER_UNSTABLE` |
| 12 根带外方向翻转 >= 2 | COMPRESSED | `PRICE_BAND_WHIPSAW` |
| 全部否 | TRENDING | `REGIME_TRENDING` |

多个压缩原因可同时保存，不做投票。

## 4. 1D 过滤表

| 4H 方向 | 1D 完整排列与斜率 | 决策 | Score |
|---|---|---|---:|
| LONG | 空头且 SMA180 下降 | BLOCK | 不评分 |
| SHORT | 多头且 SMA180 上升 | BLOCK | 不评分 |
| LONG | 多头且 SMA180 上升 | ALIGNED | 15 |
| SHORT | 空头且 SMA180 下降 | ALIGNED | 15 |
| 任意 | 混合、不完整或同向但 SMA180 不满足 | MIXED | 7 |
| 任意 | 数据不足 | BLOCK | 不评分 |

## 5. 1H 回踩表

优先级从上到下，命中即停止：

### LONG

| 优先级 | 条件 | 状态 | 是否可进 Trigger |
|---:|---|---|---|
| 1 | low <= SMA180 | RESET | 否 |
| 2 | close < SMA90 | DAMAGED | 否 |
| 3 | range 与 SMA60–SMA90 区域重叠 | STANDARD | 是 |
| 4 | range 与 SMA30–SMA60 区域重叠 | SHALLOW | 是 |
| 5 | close 在趋势侧且距区域 <= 0.50 ATR | WATCHING | 否 |
| 6 | 其他 | NONE | 否 |

### SHORT

| 优先级 | 条件 | 状态 | 是否可进 Trigger |
|---:|---|---|---|
| 1 | high >= SMA180 | RESET | 否 |
| 2 | close > SMA90 | DAMAGED | 否 |
| 3 | range 与 SMA60–SMA90 区域重叠 | STANDARD | 是 |
| 4 | range 与 SMA30–SMA60 区域重叠 | SHALLOW | 是 |
| 5 | close 在趋势侧且距区域 <= 0.50 ATR | WATCHING | 否 |
| 6 | 其他 | NONE | 否 |

二次阻断：

| 条件 | 决策 | reason code |
|---|---|---|
| LONG 最近两根 1H 均下跌、均放量且 TR 扩大 | BLOCK | `PULLBACK_ACCELERATING` |
| SHORT 最近两根 1H 均上涨、均放量且 TR 扩大 | BLOCK | `PULLBACK_ACCELERATING` |
| episode 前无一根 close 位于 SMA30 趋势侧 | BLOCK | `PULLBACK_EPISODE_NOT_NEW` |
| SMA30 与 SMA60 的方向不符 | BLOCK | `PULLBACK_MA_DIRECTION_INVALID` |

## 6. 15m 量价表

### 通用条件

| 条件 | LONG 通过 | SHORT 通过 | 失败 reason |
|---|---|---|---|
| Trigger volume | > 前 20 根中位数 | 同 LONG | `TRIGGER_VOLUME_LOW` |
| Trigger vs previous | > 上一根 volume | 同 LONG | `TRIGGER_VOLUME_NOT_EXPANDING` |
| Close location | >= 0.65 | <= 0.35 | `VOLUME_WITH_ADVERSE_CLOSE` |
| Candle range | high > low | high > low | `ZERO_RANGE_TRIGGER` |

### Trigger 专用条件

| Trigger | 前 3 根均量 < 更早 20 根中位数 | 决策 |
|---|---|---|
| A | 必须 | 否则 `PULLBACK_VOLUME_NOT_CONTRACTING` |
| B | 不强制，只记录 | 不阻断 |

## 7. Trigger A 决策表

| 序号 | LONG 条件 | SHORT 镜像 | 失败 reason |
|---:|---|---|---|
| A1 | 有效 SHALLOW/STANDARD episode | 相同 | `NO_VALID_EPISODE` |
| A2 | 最近 4 根有 close<SMA30 或触及回踩区 | close>SMA30 或触区 | `NO_MA_PULLBACK_TOUCH` |
| A3 | 当前 close>SMA30 | 当前 close<SMA30 | `MA_RECLAIM_FAILED` |
| A4 | 当前 close>前一根 high | 当前 close<前一根 low | `PREVIOUS_BAR_BREAK_FAILED` |
| A5 | 通用量价及缩量通过 | 镜像 close location | 对应量价 reason |
| A6 | 有已确认结构 Pivot | 有已确认结构 Pivot | `STOP_STRUCTURE_MISSING` |

全部通过输出 `TRIGGER_A_CONFIRMED`。

## 8. Trigger B 决策表

| 序号 | LONG 条件 | SHORT 镜像 | 失败 reason |
|---:|---|---|---|
| B1 | 有效 SHALLOW/STANDARD episode | 相同 | `NO_VALID_EPISODE` |
| B2 | 有 sweep 前已确认且未破坏的支撑 zone | 阻力 zone | `SWING_ZONE_MISSING` |
| B3 | low < zone.lower | high > zone.upper | `SWEEP_NOT_BEYOND_ZONE` |
| B4 | close > zone.upper | close < zone.lower | `SWEEP_RECLAIM_FAILED` |
| B5 | close > SMA30 | close < SMA30 | `MA_RECLAIM_FAILED` |
| B6 | 通用量价通过 | 镜像 close location | 对应量价 reason |

全部通过输出 `TRIGGER_B_CONFIRMED`，结构失效点取 sweep 极值。

## 9. A/B 合并与去重表

| A | B | 输出 |
|---|---|---|
| 否 | 否 | 无 Trigger |
| 是 | 否 | 主触发 `MA_RECLAIM` |
| 否 | 是 | 主触发 `SWEEP_RECLAIM` |
| 是 | 是 | 单一信号；主触发 `SWEEP_RECLAIM`，附加 `MA_RECLAIM` |

去重顺序：

1. 相同确定性 signal ID 已存在：不新建，返回已有记录。
2. 同 symbol 已有活跃 Paper Trade：保存合格 Setup，但不创建新 ARMED，reason=`ACTIVE_TRADE_EXISTS`。
3. 同 episode 出现新的确认 K 线：允许生成新逻辑信号，但若已有 ARMED 未终结，则保留最早 ARMED，不替换，reason=`ARMED_SIGNAL_EXISTS`。

## 10. Entry / SL / TP 表

### Entry 与 Stop

| 检查 | 允许 | 拒绝 |
|---|---|---|
| 合法 tick | 已按方向保守对齐 | `PRICE_RULE_INVALID` |
| Stop risk | 0.5 ATR <= risk <= 2.0 ATR | `STOP_DISTANCE_INVALID` |
| Entry 与 Stop 方向 | LONG stop<entry；SHORT stop>entry | `STOP_SIDE_INVALID` |

### TP

| 条件 | 决策 |
|---|---|
| 最近障碍可实现距离 < 1R | 拒绝：`OBSTACLE_LT_1R` |
| 有 >=1R 的最近结构目标 | 用作 TP1 |
| 无 >=1R 结构目标 | 用 max(1R, 1ATR) 扩展 |
| 有不同的 >=2R 结构目标 | 用作 TP2 |
| 无 >=2R 结构目标 | 用 max(2R, 2ATR) 扩展 |
| 最终 TP1<1R 或 TP2<2R | 拒绝：`TP_PLAN_INVALID` |

## 11. ARMED 生命周期表

事件按交易所时间升序处理。同一时间戳优先使用 Aggregate Trades；无法排序时使用最不利结果。

| 当前状态 | 事件 | 下一状态 | 动作 |
|---|---|---|---|
| ARMED | 先触及结构失效点 | INVALIDATED | 不创建 Trade |
| ARMED | gap>0.15 ATR | MISSED | 不追价 |
| ARMED | 正常触及 Entry | TRIGGERED | 创建 Trade/Entry fill |
| ARMED | 到达有效期右端 | EXPIRED | 不创建 Trade |
| ARMED | 重复事件 | ARMED | 幂等，无新增记录 |
| INVALIDATED/EXPIRED/MISSED | 任意迟到 Entry | 原状态 | 忽略并记录迟到事件 |

同一 1m 同时包含 Entry 与结构失效且无逐笔顺序时：按 `TRIGGERED → STOP` 处理，这是比无损 INVALIDATED 更不利的结果。

## 12. TRIGGERED 生命周期表

| 当前持仓 | 事件 | 动作/下一状态 |
|---|---|---|
| 100% | SL | 全平，CLOSED/SL |
| 100% | TP1 | 平 50%，安排下一根 1m 生效的成本保本 Stop |
| 50% | 保本 Stop | 全平，CLOSED/BREAKEVEN_AFTER_TP1 |
| 50% | TP2 | 全平，CLOSED/TP2 |
| 100% 或 50% | 最大持仓到期 | 全平，CLOSED/TIME_EXIT |
| 任意 | 重复成交事件 | 不重复结算 |

同一分钟多价位且无逐笔数据时：

| 情况 | 不利处理 |
|---|---|
| Entry 与 SL 同时可能 | Entry 后 SL |
| TP1 与 SL 同时可能 | SL 先发生 |
| TP1 与 TP2 同时可能 | TP1 后 TP2；保本 Stop 下一分钟才生效 |
| TP1 后下一分钟同时可能 TP2/保本 | 保本 Stop 先发生 |
| TP/SL 与 TIME_EXIT 同时 | 先选净结果更差的路径 |

## 13. 扫描输出等级

| 结果 | 是否保存扫描 | 是否保存 Setup | 是否创建 Signal | 是否创建 Trade |
|---|---:|---:|---:|---:|
| 数据错误 | 是 | 否 | 否 | 否 |
| NEUTRAL/COMPRESSED/1D BLOCK | 是 | 否 | 否 | 否 |
| WATCHING | 是 | 是 | 否 | 否 |
| 回踩有效但无 Trigger | 是 | 是 | 否 | 否 |
| Trigger 合格但计划失败 | 是 | 是 | 否 | 否 |
| ARMED | 是 | 是 | 是 | 否 |
| TRIGGERED | 是 | 是 | 是 | 是 |

## 14. Reason code 最低集合

Reason code 使用稳定的大写 snake case，展示文本可变但 code 不得变。最低集合：

```text
DATA_NOT_READY
OPEN_CANDLE_IGNORED
DUPLICATE_CANDLE_IDENTICAL
DUPLICATE_CANDLE_CONFLICT
CANDLE_GAP
OHLC_INVALID
VOLUME_INVALID
ATR_INVALID
TICK_SIZE_INVALID
SYMBOL_INELIGIBLE
DIRECTION_NEUTRAL
MA_BAND_NARROW
MA180_FLAT
MA_ORDER_UNSTABLE
PRICE_BAND_WHIPSAW
DAILY_OPPOSITION
PULLBACK_NONE
PULLBACK_DAMAGED
PULLBACK_RESET
PULLBACK_ACCELERATING
PULLBACK_EPISODE_NOT_NEW
PULLBACK_MA_DIRECTION_INVALID
TRIGGER_VOLUME_LOW
TRIGGER_VOLUME_NOT_EXPANDING
VOLUME_WITH_ADVERSE_CLOSE
ZERO_RANGE_TRIGGER
PULLBACK_VOLUME_NOT_CONTRACTING
NO_VALID_EPISODE
NO_MA_PULLBACK_TOUCH
MA_RECLAIM_FAILED
PREVIOUS_BAR_BREAK_FAILED
STOP_STRUCTURE_MISSING
SWING_ZONE_MISSING
SWEEP_NOT_BEYOND_ZONE
SWEEP_RECLAIM_FAILED
DUPLICATE_SIGNAL
ACTIVE_TRADE_EXISTS
ARMED_SIGNAL_EXISTS
PRICE_RULE_INVALID
STOP_DISTANCE_INVALID
STOP_SIDE_INVALID
OBSTACLE_LT_1R
TP_PLAN_INVALID
FUNDING_DATA_MISSING
PERSISTENCE_FAILED
```

新增 reason code 可以向后兼容；改变既有 code 语义必须提升数据 schema 版本。
