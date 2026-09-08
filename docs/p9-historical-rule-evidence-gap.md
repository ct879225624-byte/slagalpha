# 历史合约规则证据缺口核验

核验日期：2026-08-31；结论：P9.2b 仍为 `BLOCKED`。

## 已确认的事实与限制

1. Binance 的 USDⓈ-M Exchange Information 文档将 `/fapi/v1/exchangeInfo` 定义为
   当前交易规则和合约信息；文档没有提供按历史时间查询该接口的参数。本轮据此不能
   把当前 filters 外推到 2023–2026。来源：
   [Binance USDⓈ-M Market Data / Exchange Information](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)。
2. 官方 Public Data README 列出合约 Klines、Trades、AggTrades 的数据结构，这些
   行情字段不是历史规则生效区间；已有归档下载不能自动补齐该缺口。来源：
   [Binance Public Data](https://github.com/binance/binance-public-data)。
3. 搜索检索到官方 2026-04 Tick Size 调整公告，说明公告可能成为逐项规则证据线索；
   但直接打开在当前访问环境跳转至公告列表，未取得完整原文，未抽取或升级任何规则。
   即使某条 tick 变更核验成功，也不能据此补出同一时期的 step 或证明全期无遗漏。
4. Tardis 的提供方文档有 `changes`、`priceIncrement`、`amountIncrement`，但标明
   价格/数量精度变更仅尽力追踪、不保证完整；接口需要 Pro/Business 订阅和 Bearer
   密钥。这里只阅读公开文档，没有购买、登录、读取密钥或调用付费 API。来源：
   [Tardis Instruments Metadata API](https://docs.tardis.dev/api/instruments-metadata-api)。

这是有限的公开来源核验，不是断言所有来源都不存在历史数据。当前没有拿到足以
将任何历史 contract-rule 区间升级为 VERIFIED 的完整材料。

## 解锁所需材料

- exact symbol、交易所/合约类型、USDT quote/margin 身份；
- 历史 `tick_size`、`step_size`，以及提供时的 min/max quantity、min notional；
- 精确 UTC 生效起点/终点和原始官方快照、公告或可追溯采集记录；
- 来源 URL/文件、采集时间、内容 SHA-256、区间连续性证据；
- 复核状态与置信等级。无法证明的区间继续 UNVERIFIED，缺口不插值。

应先核验一个小范围规则区间，再复用现有门控与 P7 集成测试验证，不能直接批量
把全部草案标成 VERIFIED。第三方付费资源、密钥操作或改变既定证据标准须先确认。

## 2026-09-08 续核

官方公告进一步证明当前规则不能回填 DEV，而不是补齐了连续历史：

- BTCUSDT 的 tick size 在 2022-02-15 从 0.01 调整为 0.1，早于 DEV；该公告只能证明
  一个变更点，不能证明 2023-08-01 至 2025-01-30 没有其他未收集变更。来源：
  [Binance BTC USDⓈ-M tick size adjustment](https://www.binance.com/en/support/announcement/detail/81e6795b0bae49828cbd52479094a987)。
- BTCUSDT、ETHUSDT 的 minimum notional 在 DEV 内于 2023-11-02 分别从 5/5 USDT
  调整为 100/20 USDT；当前快照中的 BTC 值已是 50 USDT，因此单条当前规则明显不能
  覆盖 DEV。来源：
  [Binance 2023 minimum-notional adjustment](https://www.binance.com/en/support/announcement/detail/e4384cba297a4bd2a154be644d5d76f9)、
  [Binance 2026 minimum-notional adjustment](https://www.binance.com/en/support/announcement/detail/10999fd17dc045de801c0c78ab29e6fc)。
- Binance 早期 minimum-notional 规则公告明确提示阈值可能不经预告调整，因此仅搜索
  公告列表也不能证明完整连续性。来源：
  [Binance minimum order notional rule](https://www.binance.com/en/support/announcement/detail/76719bbaeeb847bbac4daa2906fcdcc0)。

本轮只核对公开页面，没有把网页摘要保存成历史快照、没有生成 intake submission，
也没有提升 verification 状态。现有 649 条规则仍全部 `UNVERIFIED`；DEV 的 248 个
symbol、16,440 个 member-days 仍为 0 eligible。

## 其他正式研究前置条件

2026-08-31 首次核验时，本地还没有有效 Git 提交；该工程阻断已在后续基线任务解除。
2026-09-08 的干净 preflight 已绑定提交 `2aa61a9`，当前剩余问题是历史规则和真实数据，
而不是 Git 基线。工程输入审计与计划工件仍不得冒充正式策略报告。
