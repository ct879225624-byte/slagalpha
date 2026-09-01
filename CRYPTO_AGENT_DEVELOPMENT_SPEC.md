# Crypto Trading Copilot 开发规格

状态：第一版定稿（用于后续开发对话）  
日期：2026-08-26  
项目目录：`slagalpha`  
当前阶段：规划完成，尚未开始代码开发

## 1. 产品目标

开发一个仅服务于个人使用的 Binance Crypto Trading Copilot：

> 自动扫描 Binance USDⓈ-M 永续合约，按照用户的 MA 与量价交易逻辑寻找高质量机会，生成可执行且可复现的 Entry、Stop Loss、Take Profit 计划，并使用 Paper Trading 自动跟踪结果。

它不是“会聊币的 AI”，也不是自动交易机器人。第一阶段的价值是：

1. 替用户持续扫描市场。
2. 增加高质量候选机会，而不是降低标准追求频率。
3. 将用户的 MA 与量价判断程序化。
4. 专门研究入场、止损和止盈是否具有正期望。
5. 积累可复盘的前向模拟交易数据。

## 2. 明确边界

### 2.1 第一阶段包含

- Binance USDⓈ-M USDT 永续合约。
- Binance 公共 REST 与 WebSocket 行情。
- 动态高流动性标的池。
- LONG 与 SHORT 信号。
- MA 趋势、排列和回踩判断。
- 量价确认。
- 结构化 Entry、SL、TP1、TP2。
- 历史回测。
- 实时市场扫描。
- Paper Trading。
- Web Dashboard。
- 后续通知、统计复盘和 Copilot。

### 2.2 第一阶段不包含

- Binance 账户连接。
- API Key。
- 真实下单。
- 自动仓位管理。
- 现货 Agent。
- 新闻、社交情绪和链上数据。
- 2 分钟 K 线。
- 高频做市或盘口套利。
- RSI、MACD、ADX等额外方向指标。
- 让 LLM 直接决定交易方向。
- 自动修改策略参数。
- 收益或胜率保证。

现货模块必须作为后续独立系统设计，不能简单复用合约逻辑。

## 3. 核心设计原则

1. 策略判断必须确定、可编码、可回测、可重放。
2. MA 和量价是第一版唯二核心判断体系。
3. ATR只用于风险尺度和止损缓冲，不决定方向。
4. BTC只作为市场环境过滤，不制造山寨币信号。
5. 不使用指标投票；每个信息只承担一种职责。
6. 回测、实时扫描和信号重放必须调用同一策略核心。
7. 信号必须使用已经收盘的 K 线。
8. 所有参数、数据和策略结果必须版本化。
9. 数据异常时停止相关判断，不能使用错误数据继续生成信号。
10. 如果策略没有通过验证，允许修改、暂停或淘汰，不能为了升级版本降低标准。

## 4. 第一版策略定义

### 4.1 策略家族

第一版只开发一个策略家族：

> MA Trend Pullback

它有两种独立统计的入场触发方式：

- Trigger A：MA Reclaim。
- Trigger B：Liquidity Sweep + MA Reclaim。

两种触发不能针对同一行情重复创建两笔 Paper Trade。若同时满足，记录全部原因，但只生成一笔信号，并标记主触发类型。

### 4.2 固定均线

第一版固定使用简单移动平均线：

- SMA30
- SMA60
- SMA90
- SMA180

MA周期来自用户现有交易习惯，不允许在 V0.1 中优化或替换。

四条 MA 应视为一套均线带，不能作为四个独立指标重复加分：

- MA30：快速趋势与15m触发。
- MA30–MA60：浅回调区域。
- MA60–MA90：标准或较深回调区域。
- MA90：趋势防守位置。
- MA180：大级别多空分界。

### 4.3 多周期职责

- 1D：大环境与强反向过滤。
- 4H：唯一的正式方向来源。
- 1H：回踩位置、趋势是否保持以及失效判断。
- 15m：量价确认和入场触发。
- 1m：仅用于 Paper Trade 成交路径，不参与方向判断。

不同周期不能被当作完全独立证据重复加分。

### 4.4 4H方向

LONG方向：

```text
SMA30 > SMA60 > SMA90 > SMA180
且 SMA180 当前值 > 5根4H K线之前的 SMA180
```

SHORT方向：

```text
SMA30 < SMA60 < SMA90 < SMA180
且 SMA180 当前值 < 5根4H K线之前的 SMA180
```

不满足任一方向时为 `NEUTRAL`，不生成趋势回调信号。

### 4.5 1D环境

1D不负责入场，只用于阻止明显逆大趋势交易：

- 1D完整空头排列且SMA180向下时，阻止LONG。
- 1D完整多头排列且SMA180向上时，阻止SHORT。
- 1D均线混合时不直接阻止，但降低质量评分。

### 4.6 均线缠绕过滤

MA策略最容易在震荡期反复止损。必须识别并过滤：

- SMA30、60、90距离过近。
- SMA180接近水平。
- MA顺序频繁交换。
- 价格反复穿越整个均线带。

使用以下尺度判断均线带是否压缩：

```text
band_width = abs(SMA30 - SMA180) / ATR(14)
```

V0.1以 `{0.50, 0.75, 1.00}` 作为小范围稳健性测试候选，不允许扩展为大规模参数搜索。最终阈值只能根据时间顺序验证确定。

状态为 `COMPRESSED` 时不生成正式信号。

### 4.7 1H回踩

LONG候选必须满足：

1. 4H方向为LONG。
2. 1H的SMA30高于SMA60。
3. 价格回踩SMA30–SMA90区域。
4. 1H收盘未有效跌破SMA90。
5. 回调不是放量加速下跌。

SHORT使用镜像规则。

回踩分级：

- `SHALLOW`：进入SMA30–SMA60区域。
- `STANDARD`：进入SMA60–SMA90区域。
- `DAMAGED`：1H有效收盘破坏SMA90。
- `RESET`：价格进入或破坏SMA180区域。

V0.1只允许 `SHALLOW` 和 `STANDARD` 进入触发阶段。

### 4.8 量价规则

成交量统一使用 USDT Quote Volume，不使用币本位成交量。

量价不是额外装饰，而是策略核心组成部分。它负责判断回调是否健康、触发是否得到真实成交支持。

普通回调的初始量价规则：

1. 回调最近3根K线的平均Quote Volume低于此前20根的中位数。
2. 触发K线Quote Volume高于此前20根的中位数。
3. 触发K线Quote Volume高于上一根K线。
4. LONG触发K线收盘位于自身振幅上方35%区域。
5. SHORT触发K线收盘位于自身振幅下方35%区域。
6. 放量但收盘位置明显不利时，不视为有效确认。

Pullback与Sweep可以使用不同的量价解释，但必须分别统计。Volume规则仅允许做小范围消融测试，不允许为追求回测收益反复改变定义。

### 4.9 Trigger A：MA Reclaim

LONG：

1. 已存在有效1H回踩。
2. 15m价格曾位于SMA30下方或触及回踩区域。
3. 15m收盘重新站上SMA30。
4. 收盘突破上一根15m K线高点或形成等价的确认结构。
5. 满足量价确认。

SHORT使用镜像规则。

### 4.10 Trigger B：Liquidity Sweep + MA Reclaim

Swing使用确认型Pivot：默认左右各2根15m K线。Pivot只有在右侧两根K线收盘后才能生效，禁止未来函数。

LONG：

1. 已存在有效1H回踩。
2. 15m扫破最近已确认的Swing Low。
3. 15m收盘重新站回该Swing Low。
4. 同时重新站上SMA30。
5. 满足Sweep量价确认。
6. 随后突破确认K线高点。

SHORT使用镜像规则。

距离小于 `0.2 × ATR(14)` 的相邻Swing应合并为结构区域，避免重复支撑阻力。

## 5. 信号状态

```text
WATCHING     接近均线回踩区域
ARMED        回踩与触发条件已形成，等待入场价
TRIGGERED    入场条件完成，创建Paper Trade
INVALIDATED  入场前结构已经失效
EXPIRED      超过有效时间仍未触发
MISSED       跳价超过最大允许追价距离
CLOSED       Paper Trade已经结束
```

状态转换必须可重放、幂等并保存时间戳。

## 6. Signal Score

Signal Score只负责排序，不能取代Setup强制条件，也不能被描述成胜率。

初始评分：

| 项目 | 分数 |
|---|---:|
| 4H MA排列、间距和斜率 | 25 |
| 1D是否支持当前方向 | 15 |
| 1H回踩质量 | 25 |
| 15m量价与触发质量 | 20 |
| 结构止损和风险收益质量 | 15 |

V0.1/V0.2记录所有满足强制条件的正式Setup，并用Score排序。只有积累足够前向样本后，才允许在V0.4校准分数和推送门槛。

## 7. 市场数据与标的池

### 7.1 数据源

唯一数据源：Binance USDⓈ-M Futures。

- REST基础地址：`https://fapi.binance.com`
- 合约信息：`/fapi/v1/exchangeInfo`
- 历史K线：`/fapi/v1/klines`
- 24小时Ticker：`/fapi/v1/ticker/24hr`
- 实时K线：Futures公共WebSocket Kline Stream
- 历史批量数据：Binance Public Data

V0.1不使用API Key，也不接触账户数据。

### 7.2 合约过滤

只保留：

```text
contractType = PERPETUAL
status = TRADING
quoteAsset = USDT
marginAsset = USDT
```

附加条件：

- 上线至少90天。
- 有完整的15m、1H、4H、1D历史数据。
- 排除稳定币互换、指数、杠杆代币、TradFi类合约和异常数据标的。
- tickSize、stepSize等规则必须动态读取，不能硬编码。

### 7.3 动态标的池

- 默认每日选取Quote Volume排名前30的合格合约。
- BTCUSDT、ETHUSDT固定保留。
- 每日更新一次。
- 每次标的池变更保存版本快照。
- 历史回测必须使用当时可见数据重建每日标的池，不能拿今天的Top 30回测过去。
- 历史排名只能使用选择时间之前的成交额。

### 7.4 K线规则

- 内部统一使用UTC。
- 正式信号只使用已收盘K线。
- 每根K线以 `exchange + symbol + interval + open_time` 唯一标识。
- REST负责历史初始化、断线补洞和恢复校验。
- WebSocket负责实时接收。
- 15m收盘触发扫描。
- 1H、4H、1D只使用各自最新已收盘K线。
- 缺失、重复、乱序或异常OHLC时，该标的本轮禁止生成信号。

## 8. Entry、Stop与Take Profit

### 8.1 入场

LONG：

```text
入场触发价 = 15m确认K线最高价 + 1 tick
```

SHORT：

```text
入场触发价 = 15m确认K线最低价 - 1 tick
```

规则：

- 信号生成后最早从下一根1m K线开始成交。
- 默认有效期为4根15m K线，即60分钟。
- 入场前先触及结构失效点则 `INVALIDATED`。
- 超过有效期未触发则 `EXPIRED`。
- 跳价超过 `0.15 × 15m ATR(14)` 则 `MISSED`，禁止追价。

有效期只允许测试 `{2, 4, 6}` 根15m K线，并使用时间顺序验证选择。

### 8.2 初始止损

LONG：

- Pullback Trigger：有效回调Swing Low下方。
- Sweep Trigger：Sweep最低点下方。

SHORT使用镜像规则。

缓冲：

```text
max(0.15 × 15m ATR(14), 2 × tickSize)
```

只接受初始止损距离位于 `0.5–2.0 × 15m ATR(14)` 的交易计划。

ATR缓冲只允许测试 `{0.10, 0.15, 0.20}`，不得进行大范围搜索。

### 8.3 Take Profit

候选目标来自：

- 15m前高/前低。
- 1H已确认Swing。
- 已确认支撑阻力区。
- ATR扩展目标。

最低要求：

- TP1至少达到1R。
- TP2至少达到2R。
- Entry前方存在小于1R的明确阻力或支撑时拒绝信号。
- 找不到合理TP2或整体计划低于1:2时不生成正式信号。

默认管理：

- TP1平仓50%。
- TP1完成后，剩余仓位止损移动到覆盖手续费与滑点的成本保本位。
- TP2平仓剩余50%。
- 第一版不使用追踪止损。

### 8.4 时间退出

- 默认最大持仓32根15m K线，即8小时。
- 到期仍未结束时按模拟市价退出，标记为 `TIME_EXIT`。
- 只允许测试 `{16, 32, 48}` 根，不允许根据锁定测试集反复调整。

## 9. Paper Trade成交模拟

- 策略使用15m、1H、4H、1D。
- 1m只负责成交判定。
- 触发后按触发价或发生跳价时的下一根1m开盘价成交。
- 所有模拟成交向交易者不利方向加入成本。
- 保存理论价格和模拟成交价格。
- TP1后的保本止损从下一根1m K线开始生效。
- 同一标的同时只允许一笔活跃Paper Trade。
- 同一分钟同时触及多个关键价位时，优先使用Binance Aggregate Trades重放真实先后顺序。
- 逐笔数据不可用时采用不利结果优先。
- 条件触发第一版统一按 `CONTRACT_PRICE` 模拟。

成本模型：

- 默认手续费：每次成交6 bps，明确标记为保守模拟假设。
- 历史基准滑点：每次成交2 bps。
- 历史压力滑点：每次成交5 bps。
- 前向Paper Trading：使用当时最优买卖价并增加1 bp延迟缓冲。
- 跨越Funding结算时间时计入实际Funding。
- 后续获知真实账户费率时通过配置替换并重算Net R。

必须保存：

- Gross PnL
- Fee
- Slippage
- Funding
- Net PnL
- Gross R
- Net R
- MAE
- MFE

## 10. 历史验证

### 10.1 数据

- 优先使用Binance官方Public Data日度/月度文件。
- 下载文件必须验证checksum。
- 使用最近至少36个月数据；不足36个月的标的从满足90天历史后开始。
- 已下架但当时符合条件的合约也应参与历史标的池，降低幸存者偏差。

### 10.2 时间划分

```text
前50%：开发集
中间25%：验证集
最后25%：锁定测试集
```

- 参数只能在开发集调整。
- 验证集用于选择少量稳健候选。
- 参数冻结后只能运行一次锁定测试集。
- 查看锁定测试结果后再修改规则，必须创建新策略版本。
- 同时执行滚动Walk-forward测试。

### 10.3 参数研究纪律

固定不优化：

- SMA30/60/90/180。
- 15m、1H、4H、1D的职责。
- 策略家族定义。
- MA与量价为核心。

只允许小范围敏感度测试：

- Pivot：`{2/2, 3/3}`。
- MA压缩阈值：`{0.50, 0.75, 1.00}`。
- ATR缓冲：`{0.10, 0.15, 0.20}`。
- 入场有效期：`{2, 4, 6}` 根15m。
- 最大持仓：`{16, 32, 48}` 根15m。

禁止对以上参数执行完整笛卡尔积暴力搜索。应以默认参数为中心逐项做敏感度测试，选择邻近参数均稳定的区域，而不是收益最高的单点。

### 10.4 成本情景

每次回测至少输出：

1. 无成本参考。
2. 基准成本。
3. 压力成本。

只有基准成本通过、压力成本没有明显失效，策略才进入前向模拟。

### 10.5 核心指标

- Net Expectancy / Trade
- Profit Factor
- 平均与中位数R
- 最大回撤（R）
- 连续亏损次数
- TP1、TP2、SL、TIME_EXIT比例
- MAE/MFE
- 平均持仓时间
- LONG与SHORT分组结果
- MA Reclaim与Sweep Trigger分组结果
- 浅回踩与标准回踩分组结果
- 标的与市场阶段分组结果
- Score区间表现
- Fee、Slippage、Funding侵蚀

同时出现的大量山寨币信号不能视为完全独立样本。统计置信区间应按时间分组或使用Block Bootstrap，并保存同一时间窗口的信号群组。

## 11. 前向验证

参数冻结后才开始正式实时Paper Trading。

最低观察条件：

```text
至少60个自然日
且至少200笔已关闭Paper Trades
且统计置信区间足以支持结论
```

样本要求：

- LONG与SHORT分别至少50笔。
- 两种Trigger分别至少50笔。
- 不得主要依赖单一币种或单一市场阶段。
- 失效、过期和未成交信号同样保留。

若200笔时Net Expectancy的95%置信区间仍跨越0：

- 继续收集至最多400笔或120天。
- 仍不明确则标记 `INCONCLUSIVE`。
- 不能为了获得结论反复修改参数。

建议策略门槛：

- 基准成本后Net Expectancy > +0.15R/笔。
- Profit Factor >= 1.25。
- 最大回撤 <= 15R。
- 95% Bootstrap期望值置信区间下界 >= 0R。
- 压力成本下不能变成明显负期望。
- 单一标的贡献不超过总净收益30%。
- 高Score信号整体应优于低Score信号。

以上是研究晋级门槛，不是未来收益保证。

## 12. 技术架构

采用模块化单体，不使用微服务。

### 12.1 后端

- Python
- FastAPI
- Pydantic
- SQLAlchemy + Alembic
- PostgreSQL
- httpx + WebSocket客户端
- pandas / NumPy
- pytest

同一代码库运行：

- `worker`：行情、扫描、策略和Paper Trade。
- `api`：查询、Dashboard接口和实时状态。

### 12.2 前端

- React
- TypeScript
- Vite
- REST读取历史状态
- SSE接收实时信号与系统事件

### 12.3 研究数据

- Binance官方ZIP数据。
- Parquet保存历史研究数据。
- pandas / NumPy执行首版研究。
- 数据量确实需要时再考虑DuckDB，不作为首版强制依赖。

### 12.4 部署

- Ubuntu/Debian VPS。
- Docker Compose。
- API、Worker、PostgreSQL和Frontend/Reverse Proxy。
- 默认不公开暴露管理接口。
- 结构化日志、健康检查、自动重启和数据库备份。

### 12.5 明确不引入

- Redis
- Celery
- Kafka
- Kubernetes
- LangChain
- Vector Database
- 多Agent框架

除非后续出现明确、经过验证的需求，否则不得增加这些组件。

## 13. 数据与可追溯性

PostgreSQL保存：

- 合约与交易规则。
- 15m、1H、4H、1D K线。
- 近期1m数据和活跃交易相关逐笔数据。
- 标的池版本。
- 扫描运行记录。
- Setup与触发明细。
- Signal Score组成。
- Entry、SL、TP计划。
- Paper Trade事件与成交。
- 策略与参数版本。
- 系统状态和异常。

每个正式信号必须保存：

```text
策略版本
参数版本
标的池版本
输入K线时间
MA值和排列状态
量价条件结果
触发类型
评分明细
Entry / SL / TP
成本模型
```

旧信号不得因新版本上线而被重新解释或覆盖。

## 14. AI与Trading Memory

第一版不让AI参与交易判断。

```text
确定性策略生成结构化结果
→ 模板或AI解释结果
→ AI查询和汇总已经保存的数据
```

Trading Memory首先是结构化交易数据库，不是聊天历史。

AI只允许：

- 将信号原因转为自然语言。
- 查询历史交易。
- 生成每日/每周复盘。
- 根据统计提出研究建议。

AI不允许：

- 直接决定LONG或SHORT。
- 修改策略参数。
- 跳过风险规则。
- 自动下单。

AI服务不可用时，行情、扫描和Paper Trading必须继续运行。

## 15. 开发执行顺序

1. 建立项目骨架、配置和数据库迁移。
2. 实现Binance REST历史K线获取。
3. 实现WebSocket、重连、去重和补洞。
4. 建立可重放的标准Candle模型。
5. 实现SMA与量价计算及测试。
6. 实现4H方向、1H回踩和15m触发纯函数。
7. 实现Entry、SL、TP Engine。
8. 实现历史事件驱动回测。
9. 运行开发集与验证集，冻结参数。
10. 实现实时Scanner。
11. 实现1m Paper Trade成交引擎。
12. 实现API和最小Dashboard。
13. 连续运行稳定性测试。
14. 开始正式前向Paper Trading。

前端必须在策略、回测和Paper Engine之后开发。

每个开发任务执行：

```text
明确当前版本验收条件
→ 阅读现有项目状态
→ 建立测试或最小复现
→ 实现一个闭环
→ 运行测试/回测/重放
→ 修复失败
→ 报告结果与风险
→ 验收后进入下一任务
```

## 16. 版本路线

### V0.1 — Strategy Lab

目标：证明MA与量价策略可编码、可回测、可验证。

交付：

- Binance历史数据与校验。
- 标准Candle模型。
- MA Trend Pullback。
- 两种15m Trigger。
- Entry、SL、TP。
- 成本模型。
- 事件驱动回测。
- 可复现报告。
- CLI，无网页要求。

进入V0.2前：

- 规则测试通过。
- 无未来函数。
- 重复运行结果一致。
- 回测与实时计划共用同一策略核心。
- 至少一个Trigger在锁定历史测试集达到初始晋级门槛。

### V0.2 — Live Crypto Radar

目标：稳定实时扫描并输出交易计划。

交付：

- Binance REST/WebSocket。
- 动态Top 30。
- 15m收盘扫描。
- PostgreSQL。
- FastAPI。
- 最小Dashboard。
- 健康状态与异常日志。

验收：

- 连续运行至少7天。
- K线完整率 >= 99.9%。
- 无重复信号。
- 断线重连后自动补齐。
- 实时结果与相同历史数据重放一致。

### V0.3 — Paper Trading Loop

目标：形成完整模拟交易闭环。

交付：

- 1m成交模拟。
- Entry等待、失效、过期和错过。
- TP1、TP2和成本保本。
- 费用、滑点、Funding。
- Paper Trade页面。
- 基础信号通知。

验收：

- 所有订单生命周期均有固定测试样本。
- 重启不会丢失或重复结算。
- 历史重放与实时状态机一致。
- 连续运行至少14天并完成至少50笔交易，确认工程闭环。

### V0.4 — Analytics & Strategy Memory

目标：使用数据评价和校准策略。

交付：

- Setup、Trigger、方向、币种和市场阶段分组统计。
- MAE/MFE与回撤分析。
- Score校准。
- 参数版本对比。
- 策略启用、暂停和淘汰。
- 每日/每周复盘。

系统可以提出建议，但不能自动修改正式策略。

### V0.5 — Personal Copilot

目标：增加通知、解释与自然语言查询。

交付：

- Telegram或其他通知渠道。
- 信号自然语言解释。
- 历史交易查询。
- 自动复盘。
- 基于统计的研究建议。

### V1.0 — Personal Crypto Trading Copilot

发布条件：

- 至少60天、200笔冻结策略前向Paper Trades。
- 至少一个Trigger通过净期望、Profit Factor、回撤和压力测试。
- 系统稳定运行30天。
- 数据补洞、恢复、备份和还原通过验证。
- 信号、Paper Trade、统计和解释完整贯通。
- 所有策略和参数均可追溯。

V1.0仍然不包含真实下单、账户连接、现货Agent和自动参数修改。

## 17. 后续方向

- Spot Agent作为V1.x独立模块重新设计。
- 新指标只能作为Challenger独立测试，不能直接加入Champion。
- 2m K线只能作为15m正式Setup之后的未来进场优化实验。
- 订单流、盘口和Open Interest只有在MA与量价基线稳定后才可研究。
- 即使累计超过500笔前向交易，真实执行仍需单独设计权限、仓位、熔断和账户安全，并由用户明确批准。

## 18. 主要风险与处理

### 幸存者偏差

按历史当时可见数据重建标的池，并包含当时存在、后来下架的合约。

### 同一分钟成交顺序不明

优先用Aggregate Trades重放；缺失时采用不利结果。

### Binance接口变化

将交易所访问封装为独立Adapter，执行Schema校验、契约测试、动态交易规则刷新和Fail Closed。

### 指标过多与重复信息

首版固定为MA与量价，不加入其他方向指标。四条MA视为一个均线带。

### 震荡反复止损

使用MA间距、MA180斜率和价格穿越频率识别 `COMPRESSED` 状态并停止交易。

### 多币种信号高度相关

按时间窗口分组统计；通知只推送同组排名最高的少数机会；不把同一行情中的多个山寨币信号当作完全独立样本。

### 策略没有通过验证

采用Champion/Challenger；失败的Trigger可以单独暂停。无法证明有效时标记 `INCONCLUSIVE`，不强行进入V1.0。

## 19. 后续开发对话约束

新的开发对话开始后应先完整阅读本文件，并遵守：

1. 先完成V0.1，不提前开发后续版本。
2. 不擅自增加指标、Agent框架、通知或真实交易。
3. 不修改SMA30/60/90/180，除非用户明确改变策略。
4. 每次改动必须对应当前版本目标或验证需求。
5. 能运行测试、回测或重放就必须验证，不能只声称“应该可用”。
6. 未通过验收时保持当前版本，不通过扩大功能掩盖策略问题。
7. 所有涉及真实账户、API Key或下单的需求必须重新征得用户明确授权。

## 20. 官方参考

- Binance Developer Documentation: <https://developers.binance.com/en/docs/catalog>
- Binance USDⓈ-M Futures Market Data: <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data>
- Binance USDⓈ-M Futures WebSocket Streams: <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>
- Binance Public Historical Data: <https://github.com/binance/binance-public-data>

