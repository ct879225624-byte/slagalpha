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

## 2026-09-09 公开存档实证审计

经用户授权，只访问公开页面和公开存档，不使用账户、密钥或付费接口。Internet Archive
CDX 对 Binance 官方 USDⓈ-M `exchangeInfo` URL 在 2023–2025 年只返回 1 份去重后的
HTTP 200 JSON 捕获：`20231102093209`。已保存解压后的原始响应，SHA-256 为
`6a3b256bcc2a05d3542897bc45df57417dc33b51506231099186ba9a73f21572`；原始文件继续位于
Git 忽略的数据目录，不把第三方存档身份冒充 Binance 官方签名。

新增 `historical-rule-source-audit/0.1.0`，将存档捕获时间、CDX digest、原始官方 URL、
回放 URL、下载时间、原始字节哈希和 DEV rule-gap hash 绑定到不可变报告。真实复跑结果：

- 快照含 272 个 symbol；248 个 DEV 取证目标中可解析观察到 186 个，缺少 62 个；
- 1 个无关的待交易 `BTCSTUSDT` 条目 `contractType` 为空，审计显式记录为不可解析，
  但不会再让它遮蔽 BTC/ETH 等可解析目标的精确点时值；
- 最高优先级 BTC、DOGE、ETH、SOL、XRP 五个目标均提取了完整 PRICE_FILTER、LOT_SIZE
  和 minimum-notional 点时值；
- 响应 `serverTime=2023-11-02T08:47:08.849Z`，存档捕获时间为
  `2023-11-02T09:32:09Z`，两者相差 2,700,151 ms；
- BTC/ETH 在该响应中已分别为 100/20 USDT，而官方公告仅声明更新会在 10:00 UTC
  前完成。这可以证明公告计划时间不能当作精确撮合切换时刻；
- 单个点时观察没有区间连续性，第三方存档真实性仍需人工复核；因此新增的可用
  DEV member-day 仍为 0，注册表未修改，研究授权保持关闭。

内容寻址报告 hash：
`01b28cea5c13569cdc70ee3645eae4b2e0f4d22f051313c57b9d379217f5ef4d`。阻断原因固定为：

- `ARCHIVE_SOURCE_AUTHENTICITY_REVIEW_REQUIRED`
- `DEV_TARGETS_MISSING_FROM_SNAPSHOT`
- `POINT_IN_TIME_SNAPSHOT_HAS_NO_INTERVAL_CONTINUITY`
- `SNAPSHOT_CONTAINS_UNPARSEABLE_CONTRACTS`

复跑命令：

```powershell
.venv\Scripts\python.exe scripts\p9_historical_rule_source_audit.py
```

该脚本固定预期原始 SHA-256，文件变化会返回 `INVALID_SOURCE`；正常审计仍按设计返回码 1。
冻结 CPython 3.12.13 下专项历史源与既有 intake 回归 `41 passed`，全量
`892 passed, 1 skipped`；Ruff 全仓与 mypy `src scripts`（100 source files）通过。
来源：[Binance Exchange Information](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、
[Binance 2023 minimum-notional adjustment](https://www.binance.com/en/support/announcement/detail/e4384cba297a4bd2a154be644d5d76f9)、
[Internet Archive replay](https://web.archive.org/web/20231102093209id_/https://fapi.binance.com/fapi/v1/exchangeInfo)。

## 2026-09-09 Common Crawl 补充核验

对覆盖 DEV 的 Common Crawl 公开索引（`CC-MAIN-2023-40`、`2023-50`、
`2024-10` 至 `2024-51`、`2025-05`）逐一查询同一 Binance 官方 URL。全部返回
`No Captures found`；首次遇到的 `2024-26`、`2024-33`、`2025-08`、`2025-13` 网关超时已
使用 60 秒上限定向重试，均同样返回无捕获。`2025-43` 虽有记录，但状态是 HTTP 451，且在
DEV 结束后，因此不构成历史规则材料。

这只是对第二个第三方公开爬虫索引的可用性核验，不是证明互联网不存在其他材料。它没有
产生可下载的 HTTP 200 原始快照，未改变第 38 项的 Internet Archive 审计、注册表、
eligible member-day 或任何研究授权。
