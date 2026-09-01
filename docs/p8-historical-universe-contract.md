# P8 历史数据与动态标的池契约 v0.1

状态：P8.1–P8.5 已完成；P8.6 进行中  
冻结日期：2026-08-28  
适用范围：P8 历史研究数据、合约生命周期和每日动态 Universe；不适用于实时下单

## 1. 目的与边界

本契约把 P0 已定义的 `ContractRegistry`、`ExclusionLedger` 和
`UniverseSnapshot` 落到可执行的历史数据边界。P8.1 只冻结规则，不下载 36 个月
数据、不接账户、不使用 API Key，也不实现 P8.2 领域模型。

本阶段固定以下原则：

- 历史池只能使用选择时刻已经可见的数据，不能用今天的 Top 30、今天的
  `exchangeInfo.status` 或当前 24 小时 ticker 回填过去。
- 后续下架但在当时合格的合约必须参与其有效期内的候选池。
- 数据、生命周期或排除证据不确定时，只阻断对应 symbol-day，批次继续处理其他标的。
- 原始文件和历史快照不可静默覆盖；任何来源内容变化都生成新版本。

## 2. 官方来源冻结

以下接口和数据说明已于 2026-08-28 核验：

| 用途 | 官方来源 | 本项目使用边界 |
|---|---|---|
| 当前合约规则快照 | [USDⓈ-M Futures Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data) 的 `GET /fapi/v1/exchangeInfo` | 保存当前 `contractType`、`onboardDate`、`status`、资产和 filters；当前状态不能单独证明历史状态 |
| 历史 K 线与 Aggregate Trades | [Binance Public Data](https://github.com/binance/binance-public-data/blob/master/README.md) | 使用 USD-M (`um`) 日/月 ZIP 和官方 `.CHECKSUM`；K 线对应 `/fapi/v1/klines`，Aggregate Trades 对应 `/fapi/v1/aggTrades` |
| Public Data 下载约定 | [Public Data Python README](https://github.com/binance/binance-public-data/blob/master/python/README.md) | 用于目录、市场类型、symbol、interval 和 checksum 规则核验，不把示例脚本当数据证据 |
| Funding 历史 | [Get Funding Rate History](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History) 的 `GET /fapi/v1/fundingRate` | 保存响应和内容哈希；缺失结算记录时沿用 P7 Fail Closed 规则 |
| K 线字段语义 | [Kline/Candlestick Data](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data) | 校验 open/close time、quote volume 和周期字段 |

约束：

- `exchangeInfo.pricePrecision` 不能替代 `PRICE_FILTER.tickSize`，
  `quantityPrecision` 不能替代 `LOT_SIZE.stepSize`。
- 当前 `/fapi/v1/ticker/24hr` 是滚动 24 小时端点，不是历史 Universe 的排名数据源。
- REST Aggregate Trades 只提供近期窗口；历史歧义成交顺序只能读取官方 Public Data
  归档，不能假装可由 REST 完整回补。
- Public Data 归档可能被官方更新。发现同一 URL 的 checksum 或内容变化时保留旧文件和
  manifest，并创建新的 dataset/universe 版本。

## 3. 研究时间轴

### 3.1 正式观察区间

固定为 36 个完整 UTC 自然月：

```text
research_start          = 2023-08-01T00:00:00Z   # inclusive
research_end_exclusive  = 2026-08-01T00:00:00Z   # exclusive
```

即正式观察数据覆盖 2023-08-01 至 2026-07-31。当前日期为 2026-08-28，P8 不把
尚未结束的 2026-08 月混入月度研究集。将来滚动截止日期必须新建 dataset 版本，不能修改
本版本。

### 3.2 Warm-up 与 Universe 前置数据

- 15m、1h、4h、1d 的统一归档请求从 `2023-02-01T00:00:00Z` 开始，确保正式观察
  起点前具备 1D SMA180 的完整 warm-up 余量。
- 首日 Universe 排名至少需要 `[2023-07-31T00:00:00Z,
  2023-08-01T00:00:00Z)` 的 96 根已收盘 15m Candle。
- 90 天合约年龄由已核验的生命周期证据计算，不能仅凭 warm-up 区间或首根 Candle
  自动判定。
- 所有研究划分仍按全局时间轴进行；新合约达到 90 天前没有资格，但不改变全局边界。

## 4. 两阶段数据获取范围

### 4.1 第一遍：可复现研究主数据

对历史候选合约并集，在各自已核验生命周期范围内获取：

- 15m、1h、4h、1d K 线：官方 Public Data 月度 ZIP + `.CHECKSUM`。
- Funding：官方 `/fapi/v1/fundingRate`，分页响应原样保存并写内容哈希。
- Funding 结算所需 Mark Price Kline：官方历史数据，保存独立 manifest。

月度归档是正式主源。日度归档或 REST 只能用于有明确工单的缺口调查/补齐，且必须记录
source、请求区间、原始响应哈希和与月度源的冲突结果；不得静默混用。

### 4.2 第二遍：成交路径细化

- 不预先下载全部历史合约的完整 1m 与 Aggregate Trades。
- 先用冻结后的 15m 信号和 P7 replay request 确定
  `confirmation_time -> expiry/holding_end` 的闭区间请求清单。
- 仅为这些区间获取 1m K 线。
- 仅当同一 1m Candle 内多个价位顺序仍不明确时，获取对应官方 Public Data
  Aggregate Trades；不可用时沿用 P7 的不利成交顺序规则，并记录原因。

该两阶段方案减少无用途下载，但不会改变策略判断：第二遍数据只解析成交路径，不得反向
改变 Setup、Trigger、Trade Plan 或 Universe。

## 5. 历史合约并集与生命周期证据

历史候选并集由以下来源求并集，不以当前 `exchangeInfo` 列表为全集：

1. 当前 `exchangeInfo` 快照。
2. 官方 Public Data 的 USD-M symbol/month 文件清单。
3. 可审计的 Binance 官方上线、下架或迁移证据。

每条 registry 记录至少保存：

- `symbol`、`contract_type`、`quote_asset`、`margin_asset`。
- `onboard_date`、`effective_from`、`effective_to`（可空）。
- `tick_size`、`step_size` 及其生效区间。
- 原始来源、抓取时间、原始内容哈希、推导方法、复核状态和置信等级。

生命周期判定规则：

- 优先采用官方明确给出的上线/下架或有效区间证据。
- `derived_first_candle_at` 只是候选证据，不能自动等同官方 `onboard_date`。
- 当前 `status=TRADING` 只描述当前快照，不得用于历史日期。
- selection time 必须位于该合约已核验的有效区间内。
- 无法核验当时为 `PERPETUAL + USDT quote + USDT margin` 的合约标记
  `UNVERIFIED`，不得进入锁定研究池，并计入 coverage-loss 报告。
- 下架后不再入池，但其下架前的合格快照和行情永久保留。

## 6. ExclusionLedger

稳定币互换、指数、杠杆代币、TradFi 类合约和已知异常标的只能通过版本化
`ExclusionLedger` 排除：

- 每条记录必须有 exact symbol、类别、`effective_from`、`effective_to`、证据、审核者和
  `ledger_version`。
- 运行时禁止使用模糊名称匹配或未经审核的临时黑名单。
- P8.2 不凭推测预填真实排除项；实际条目在取得证据并复核后加入。
- 新增、删除或修改历史排除项必须生成新 ledger/universe 版本并重跑相关结果。

## 7. 每日 Universe 算法

对每个 UTC 日期 `D`：

1. `D 00:05` 生成快照，只读取 `[D-1 00:00, D 00:00)` 的数据。
2. 候选 symbol 必须在 `D 00:05` selection time 满足已核验生命周期和合约类型/资产
   规则、至少 90 个完整 24 小时周期的年龄、不在当时生效的 ExclusionLedger 中；恰好
   90 天通过，少 1 秒失败。排名行情仍只截止 `D 00:00`。
3. 每个候选必须恰有 96 根唯一、连续、已收盘 15m Candle；否则只阻断该
   symbol-day。
4. `rolling_quote_volume_24h = sum(96 根 quote_volume)`。
5. 按 `rolling_quote_volume_24h` 降序排列，完全相同时按 `symbol` 升序。
6. 取总数 30。合格的 BTCUSDT/ETHUSDT 若不在前 30，依次替换排名最低的非固定
   symbol；总数仍为 30。
7. BTCUSDT/ETHUSDT 自身不合格或数据异常时不强行加入，并记录阻断原因。
8. 快照从 `D 00:15` 首个扫描点生效，直到下一快照生效。

禁止事项：

- 不使用 `D 00:00` 之后的数据。
- 不使用当前 `/ticker/24hr` 结果重建历史排名。
- 不用当前合约列表删除历史下架标的。
- 不因某个异常 symbol 中止其他合格 symbol 的当日选择。

## 8. 故障与冲突策略

以下任一情况均对对应 symbol-day Fail Closed：

- checksum 不匹配、ZIP 损坏或 manifest 缺失。
- Candle 缺失、重复、乱序、未收盘、非法 OHLC 或 quote volume 非法。
- 同一唯一键存在内容冲突，且来源优先级/人工复核尚未解决。
- 月度、日度或 REST 对同一 Candle 内容不一致。
- 生命周期、合约类型、资产或排除证据处于 `UNVERIFIED`。

批次必须继续计算其他 symbol，并输出：阻断 symbol、UTC 日期、reason code、来源证据和
影响的候选/入选数量。禁止插值、复制上一根、用零填充或把缺失当低成交额。

## 9. 版本与不可变性

每个 UniverseSnapshot 的版本输入至少包括：

```text
universe_version = sha256(
    selection_algorithm_version
    + contract_registry_version
    + exclusion_ledger_version
    + candle_dataset_manifest_hash
    + ordered_member_evidence
)
```

`ordered_member_evidence` 包含排名、96 根输入范围、rolling quote volume、固定成员替换和
阻断原因的 canonical 表示。相同输入必须得到相同 hash；任一 registry、ledger、归档内容
或算法变化均创建新版本，旧 snapshot 不可修改。

## 10. P8.1 验收矩阵

| 验收项 | 冻结结果 |
|---|---|
| 至少 36 个月的共同研究边界 | 2023-08-01（含）至 2026-08-01（不含） |
| 1D SMA180 warm-up | 统一从 2023-02-01 获取主周期数据 |
| 历史排名输入 | 选择前恰好 96 根已收盘 15m quote volume |
| 当前数据误用防护 | 禁止当前 Top 30、当前状态和当前 24h ticker 回填历史 |
| 幸存者偏差控制 | 当前列表 + 官方归档清单 + 官方生命周期证据求并集 |
| 上线时间证据 | 首根 Candle 只作候选证据；未核验合约不得进锁定池 |
| 异常隔离 | symbol-day Fail Closed，批次继续 |
| 归档变更 | 保留旧内容并生成新 dataset/universe 版本 |
| 下载边界 | P8.1 不下载；P8.4 先小型试点，P8.5 经用户确认后才扩展 36 个月 |
| 实盘边界 | 不接账户、私有接口、API Key 或真实下单 |

P8.2 只能把本契约实现成最小领域模型和测试；若发现契约歧义，必须先修订本文件并提升
契约版本，不能在代码中默默选择。
