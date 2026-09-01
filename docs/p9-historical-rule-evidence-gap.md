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

## 其他正式研究前置条件

本地 `git rev-parse --verify HEAD` 当前没有有效提交，仓库文件仍未跟踪。因此，即使
历史规则补齐，正式晋级 RunManifest 还需真实代码提交与干净工作区。本次未创建提交
或改动 Git 状态，工程输入审计与计划工件不冒充正式策略报告。
