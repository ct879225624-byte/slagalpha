# Golden Scenarios V0.1

状态：P0 已验收冻结基线  
用途：P1–P7 的测试设计输入，不是回测结果  
依赖：`strategy-rules-v0.1.md`、`decision-tables-v0.1.md`、`data-contracts-v0.1.md`

## 1. Fixture 约定

- 除非场景另有说明，所有 Candle 已收盘、时间连续、UTC 对齐且历史窗口充足。
- 除非场景另有说明，tickSize=`0.1`，15m ATR14=`10`，1H ATR14=`20`，4H ATR14=`100`。
- LONG 的 4H SMA 为 `SMA30=120, SMA60=115, SMA90=110, SMA180=100`，SMA180(t-5)=95。
- LONG 的 1D 环境为同向完整多头，SHORT 使用镜像数据。
- 合约合格、universe 有效且没有活跃 Paper Trade。
- Decimal 比较使用精确值；表中未列字段沿用 fixture 默认值。
- 每个场景未来实现时至少断言：最终 Gate、状态、reason codes、关键价格或“未创建对象”。

## 2. 数据与时间场景

### GS-001：未收盘 K 线不得参与

Given：扫描时间 12:14:59，存在 open_time=12:00 的 15m K 线且 `is_closed=false`。  
When：运行 15m 扫描。  
Then：该 K 线被忽略，使用上一根已收盘 K 线；记录 `OPEN_CANDLE_IGNORED`，不能产生以 12:00 为 confirmation 的 Signal。

### GS-002：时间网格缺口 Fail Closed

Given：15m 序列有 10:00、10:15、10:45，缺少 10:30。  
When：评估覆盖该窗口的 symbol。  
Then：G01 阻断，reason=`CANDLE_GAP`，不创建 Setup/Signal。

### GS-003：相同重复 Candle 幂等去重

Given：相同唯一键出现两次，所有权威字段相同。  
When：规范化。  
Then：只保留一条 Candle，记录 `DUPLICATE_CANDLE_IDENTICAL`，数据范围仍可用。

### GS-004：冲突重复 Candle 阻断

Given：相同唯一键出现两次，close 分别为 100 和 101。  
When：规范化。  
Then：写 DataAnomaly=`DUPLICATE_CONFLICT`，G01 reason=`DUPLICATE_CANDLE_CONFLICT`，影响区间禁止信号。

### GS-005：非法 OHLC 阻断

Given：open=100、high=99、low=98、close=98.5。  
When：校验。  
Then：reason=`OHLC_INVALID`，不得静默调高 high。

## 3. 4H 与 1D 场景

### GS-006：标准 4H LONG

Given：使用默认 LONG SMA，SMA180(t)=100>SMA180(t-5)=95，所有压缩条件为 false。  
When：G03/G04。  
Then：direction=LONG、regime=TRENDING。

### GS-007：SMA 相等时 NEUTRAL

Given：SMA30=120、SMA60=115、SMA90=100、SMA180=100。  
When：G03。  
Then：`DIRECTION_NEUTRAL`，相等不算严格多头。

### GS-008：band width 阈值边界

Given A：`abs(SMA30-SMA180)/ATR=0.749999`。  
Then A：COMPRESSED，reason=`MA_BAND_NARROW`。  
Given B：比值恰好 0.75，其他压缩条件 false。  
Then B：不因 band width 压缩。

### GS-009：近水平边界仍压缩

Given：`abs(SMA180_t-SMA180_t-5)=10`，4H ATR=100。  
When：G04。  
Then：因 `10 <= 0.10×100`，reason=`MA180_FLAT`。

### GS-010：MA 顺序变化次数

Given A：最近标签 `BULL,MIXED,BULL,MIXED` 后保持 BULL，共 3 次相邻变化。  
Then A：COMPRESSED，reason=`MA_ORDER_UNSTABLE`。  
Given B：只有 2 次变化且其他条件 false。  
Then B：不因顺序变化压缩。

### GS-011：全带穿越忽略 INSIDE

Given：去掉 INSIDE 后的带外序列为 `ABOVE, BELOW, ABOVE`。  
When：G04。  
Then：2 次翻转，reason=`PRICE_BAND_WHIPSAW`。

### GS-012：1D 强反向阻断

Given：4H LONG；1D `SMA30<SMA60<SMA90<SMA180` 且 SMA180 下降。  
When：G05。  
Then：`DAILY_OPPOSITION`，不进入 1H。

### GS-013：1D MIXED 不阻断

Given：4H LONG；1D SMA 混合且窗口完整。  
When：G05/Score。  
Then：daily_context=MIXED，继续，1D Score=7。

## 4. 1H 回踩场景

LONG fixture：SMA30=110、SMA60=105、SMA90=100、SMA180=90。

### GS-014：SHALLOW

Given：1H low=106、high=112、close=109。  
When：回踩分类。  
Then：range 与 [105,110] 重叠且未触及 STANDARD，状态 SHALLOW。

### GS-015：同 K 线触及两区时 STANDARD 优先

Given：low=103、high=111、close=108。  
When：回踩分类。  
Then：同时触及 [105,110] 和 [100,105]，输出 STANDARD。

### GS-016：wick 跌破 SMA90 不等于 DAMAGED

Given：low=99、close=101、且 low>SMA180。  
When：回踩分类。  
Then：不是 DAMAGED；因触及标准区，输出 STANDARD。

### GS-017：close 跌破 SMA90

Given：low=98、close=99、且 low>SMA180。  
When：回踩分类。  
Then：DAMAGED，reason=`PULLBACK_DAMAGED`。

### GS-018：RESET 优先于 DAMAGED

Given：low=90、close=89。  
When：回踩分类。  
Then：RESET，reason=`PULLBACK_RESET`，不同时输出 DAMAGED 为主状态。

### GS-019：WATCHING 距离边界

Given：close=120，SHALLOW 上沿=110，1H ATR=20，距离=10。  
When：分类。  
Then：距离恰好 0.50 ATR，输出 WATCHING。

### GS-020：放量加速回调

Given：此前 20 根 1H volume 中位数=1000；最近两根均为下跌 K 线，volume=1200/1300，TR=15/20。  
When：LONG 回踩健康检查。  
Then：reason=`PULLBACK_ACCELERATING`，即使区域为 SHALLOW 也不进入 Trigger。

## 5. Volume 与 Trigger A 场景

### GS-021：Trigger A LONG 完整通过

Given：有效 SHALLOW episode；最近 4 根有 close<SMA30；候选 close=111>SMA30=110 且 > previous high=110.5；前三根 volume 均值=800<此前 20 中位数=1000；候选 volume=1500>触发前 20 中位数=1000 且 > previous=900；low=100、high=112、close_location=11/12；存在已确认 Pivot Low=98。  
When：G07。  
Then：`TRIGGER_A_CONFIRMED`，输出 ARMED 候选。

### GS-022：Trigger A 未缩量

Given：GS-021，但前三根 volume 均值=1000，baseline=1000。  
When：G07。  
Then：严格小于不成立，reason=`PULLBACK_VOLUME_NOT_CONTRACTING`，无 Signal。

### GS-023：Close location 边界

Given LONG：low=100、high=120、close=113，location=0.65，其他条件通过。  
Then：close location 通过。  
Given SHORT：low=100、high=120、close=107，location=0.35。  
Then：close location 通过。

### GS-024：放量但收盘不利

Given LONG：候选 volume 通过，但 low=100、high=120、close=112.9，location=0.645。  
When：量价确认。  
Then：reason=`VOLUME_WITH_ADVERSE_CLOSE`，不得四舍五入成 0.65。

### GS-025：零振幅 Trigger

Given：high=low=close=100，volume 很高。  
When：量价确认。  
Then：reason=`ZERO_RANGE_TRIGGER`，不能除零或视为强收盘。

### GS-026：缺少已确认 Stop Pivot

Given：A1–A5 通过，但 episode 内只有一个尚缺右侧 2 根的潜在 Pivot Low。  
When：Trigger A。  
Then：reason=`STOP_STRUCTURE_MISSING`，无正式 Signal。

## 6. Pivot 与 Trigger B 场景

### GS-027：Pivot 禁止未来函数

Given：15m lows=`[105,103,100,104]`，中心 100 右侧只有 1 根。  
When：第 4 根收盘。  
Then：Pivot Low 尚未确认。  
When：再收一根 low=106。  
Then：中心 Pivot 在此时确认，`confirmed_at` 为第 5 根 close。

### GS-028：Pivot zone 合并严格小于

Given：ATR=10，已有 Pivot=100。  
When A：新 Pivot=101.9，距离 1.9<2.0。  
Then A：合并为 zone [100,101.9]。  
When B：新 Pivot=102.0，距离恰好 0.2 ATR。  
Then B：不合并。

### GS-029：Trigger B LONG 完整通过

Given：有效 episode；已确认支撑 zone=[100,101]；候选 low=99、close=111>zone.upper 且 >SMA30=110；high=112；volume=1500>median20=1000 且 >previous=900；close_location=12/13。  
When：G07。  
Then：`TRIGGER_B_CONFIRMED`，invalidation=99，Entry raw=112.1。

### GS-030：只收回单个 Pivot、未收回整个 zone

Given：zone=[100,101]，候选 low=99、close=100.5。  
When：Trigger B。  
Then：close 未严格高于 zone.upper，reason=`SWEEP_RECLAIM_FAILED`。

### GS-031：已破坏 zone 不可 Sweep

Given：支撑 zone=[100,101]；Sweep 前已有一根 15m close=99.9。  
When：后续行情扫到 99 并收回 102。  
Then：旧 zone 已失效，reason=`SWING_ZONE_MISSING`，不能事后把破位解释成 Sweep。

### GS-032：A/B 同时成立只建一个 Signal

Given：同一 confirmation K 线同时通过 GS-021 和 GS-029。  
When：合并。  
Then：一个 signal_id；primary=`SWEEP_RECLAIM`；all_triggers=`[SWEEP_RECLAIM, MA_RECLAIM]`。

## 7. Entry、Stop 与 TP 场景

### GS-033：LONG 价格按 tick 保守对齐

Given：confirmation high=112.03、tick=0.1；Pivot Low=99.97；ATR=10；buffer=max(1.5,0.2)=1.5。  
When：生成计划。  
Then：Entry=112.2（high+tick 后向上 tick 对齐）；raw stop=98.47，Stop=98.4（向下对齐）。

### GS-034：Stop 距离边界允许

Given A：risk=5、ATR=10。  
Then A：0.5 ATR，允许。  
Given B：risk=20、ATR=10。  
Then B：2.0 ATR，允许。  
Given C：risk=20.1。  
Then C：reason=`STOP_DISTANCE_INVALID`。

### GS-035：小于 1R 的障碍拒绝

Given：LONG Entry=100、Stop=90，最近阻力的保守目标价=109.9。  
When：TP 规划。  
Then：0.99R，reason=`OBSTACLE_LT_1R`。  
Given：目标价=110。  
Then：恰好 1R，允许作为 TP1。

### GS-036：无结构目标时 ATR 扩展

Given：LONG Entry=100、Stop=92，R=8、ATR=10，前方无确认阻力。  
When：TP 规划。  
Then：TP1=110（max(8,10)）；TP2=120（max(16,20)），再按 tick 对齐。

## 8. ARMED 与成交顺序场景

### GS-037：正常穿越 Entry

Given：LONG Entry=100，下一根 1m open=99、high=101、low=98.5，未触及失效点。  
When：成交模拟。  
Then：理论成交=100，状态 TRIGGERED；成本另计。

### GS-038：Gap 阈值边界

Given：Entry=100、ATR=10，允许 gap=1.5。  
When A：下一根 1m open=101.5。  
Then A：允许成交于理论 open=101.5。  
When B：open=101.5001。  
Then B：MISSED。

### GS-039：有效期右端不成交

Given：confirmation close=10:00，TTL=4×15m。  
When：首次触及 Entry 发生在 11:00:00。  
Then：窗口 `[10:00,11:00)` 已结束，状态 EXPIRED。

### GS-040：同一分钟 Entry 与失效且无逐笔

Given：LONG Entry=100、Stop/失效路径可在同一根 1m 内发生，high=101、low=90，无 Aggregate Trades。  
When：不利顺序。  
Then：先 Entry 后 Stop，创建一笔亏损 Trade；不能标记成无损 INVALIDATED。

### GS-041：入场前明确先失效

Given：第一根 1m high<Entry，low 已触及结构失效点。  
When：处理。  
Then：INVALIDATED，不创建 Trade。

## 9. Paper Trade 生命周期场景

### GS-042：TP1 后保本下一分钟生效

Given：10:05 的 1m 同时触及 TP1 后又回落到拟议保本价，无逐笔数据。  
When：处理 10:05。  
Then：TP1 平 50%，保本尚未生效，不在同一分钟退出剩余仓位。  
When：10:06 再触及保本。  
Then：剩余 50% 退出。

### GS-043：TP1 与 SL 同分钟不利优先

Given：持仓 100%，同一 1m high>=TP1 且 low<=SL，无 Aggregate Trades。  
When：处理。  
Then：SL 先发生，全平，不记 TP1。

### GS-044：TP1 与 TP2 同分钟

Given：同一 1m 同时达到 TP1、TP2，且未触及 SL。  
When：处理。  
Then：先记录 TP1 50%，再记录 TP2 50%，最终 CLOSED/TP2。

### GS-045：TIME_EXIT

Given：未触及任何 TP/SL，已满 32 个完整 15m 区间。  
When：下一根 1m 开始。  
Then：按该 1m open 加不利成本退出，CLOSED/TIME_EXIT。

### GS-046：Funding 缺失

Given：持仓跨过 Funding 时间，但对应 Funding rate 缺失。  
When：统计。  
Then：交易仍保留 Gross 结果，reason=`FUNDING_DATA_MISSING`，不得进入正式 Net Expectancy。

## 10. Universe、版本与重复性场景

### GS-047：历史池无未来数据

Given：D 日 00:05 生成池；某合约在 D 日 12:00 才出现巨量。  
When：计算 D 日 snapshot。  
Then：只能使用 D-1 00:00 至 D 00:00 的 96 根已收盘 15m，12:00 数据不能影响当日池。

### GS-048：BTC/ETH 固定保留但总数仍为 30

Given：BTC 排名 35、ETH 排名 2，二者均合格，原 Top30 有 30 个。  
When：生成 snapshot。  
Then：BTC 替换最低排名的非固定成员，最终仍为 30；BTC `forced=true`，ETH=false。

### GS-049：未满 90 天排除

Given：selection_time 与可验证 onboard time 相差 89 天 23:59:59。  
Then：`SYMBOL_INELIGIBLE`。  
Given：相差恰好 90 个完整自然日。  
Then：历史年龄条件通过。

### GS-050：同输入重复运行

Given：完全相同的 Candle hashes、strategy/parameter/universe version、成本模型和 random seed。  
When：运行两次回测。  
Then：Signal/Event/Trade 序列 canonical hash 和报告核心数值完全一致。

### GS-051：旧 Signal 不被新参数重解释

Given：signal 使用 parameter v0.1.0；之后创建 v0.1.1。  
When：查询或重放旧 signal。  
Then：仍引用 v0.1.0，不能覆盖其 Score、Entry、SL、TP 或原因。

### GS-052：活跃交易阻止同标的新 Signal

Given：symbol 已有 ACTIVE PaperTrade，新的 Setup/Trigger 完全合格。  
When：G08。  
Then：保存 Scan/Setup 证据，不创建新 ARMED，reason=`ACTIVE_TRADE_EXISTS`。

## 11. P1–P7 测试映射

| 阶段 | 首批必须自动化的场景 |
|---|---|
| P1 契约与模型 | GS-001–005、GS-050–051 |
| P2 数据闭环 | GS-001–005、GS-047–049 |
| P3 指标/Pivot | GS-006–011、GS-027–028 |
| P4 Setup | GS-012–020 |
| P5 Trigger | GS-021–032、GS-052 |
| P6 价格计划 | GS-033–036 |
| P7 回测/Paper 模拟 | GS-037–046、GS-050 |

任何场景的期望结果发生变化，都必须先说明属于 bug 修复、规则澄清还是策略语义变更；策略语义变更必须提升版本。
