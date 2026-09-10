# P9 付费历史规则来源资格审计

审计日期：2026-09-10；结论：4 个候选均为 `VENDOR_CONFIRMATION_REQUIRED`，P9 pilot
尚未授权。

## 资格门槛

`rule-source-qualification/0.1.0` 对每个 provider 强制核验同一组 12 项条件：Binance
USDS-M、完整 DEV 日期、tick size、step size、min/max quantity、minimum notional、精确
生效时间、历史变更完整性、原始来源时间戳、原始字节导出和可审计许可。只有全部为
`CONFIRMED` 才能得到 `QUALIFIED`；明确不可提供的条件得到 `REJECTED`；best-effort、
未公开或仍需合同确认的条件一律得到 `VENDOR_CONFIRMATION_REQUIRED`。

报告是 frozen Pydantic model，以 canonical JSON 计算 SHA-256 并写入新文件。资格报告固定
`purchase_authorized=false`、`credentials_used=false`、`paid_api_called=false`、
`registry_modified=false`、`research_authorized=false` 和 `locked_test_consumed=false`；
任何资格状态都不能直接修改 registry 或授权研究。

## 审计结果

| Provider | 公开文档结论 | 状态 | 报告 SHA-256 |
| --- | --- | --- | --- |
| Tardis.dev | 明确覆盖 Binance USDS-M 和 DEV 日期；price/amount increment 及其他非 multiplier 变更仅 best-effort，max quantity、逐规则原始时间戳和原始规则快照未公开 | `VENDOR_CONFIRMATION_REQUIRED` | `edd15e1f58a70e989de25edeead722687426b6363ac9a0629200097cfa5984c5` |
| Kaiko | 公开 reference schema 提供 instrument identity、类别和交易起止时间；未公开 Binance USDS-M 历史 filters schema、精确变更时刻或完整性保证 | `VENDOR_CONFIRMATION_REQUIRED` | `ba2d2619122c4b5dd5c895177095889ef90ba9923e8168b945f6bb5a9aa801b6` |
| Amberdata | 当前 futures reference schema 有价格/数量限制与精度字段，但 Binance 示例未区分 USDS-M，minimum cost 为 null，且未公开历史规则序列或完整性保证 | `VENDOR_CONFIRMATION_REQUIRED` | `d286a057144bbd17ae9205f0125b4c82c0184aa42a7623f1c635a2957a09c0c6` |
| Coin Metrics | 当前 market metadata 最接近所需 schema，包含 tick、amount increment、min/max amount 和 minimum order size；但公开 API 没有历史 metadata 查询、生效时刻或完整性保证 | `VENDOR_CONFIRMATION_REQUIRED` | `ad388a7df2d28f272b4a8e89b209a151bfc76b4e1ced30248be385b4d021c548` |

### Tardis.dev

- [Instruments Metadata API](https://docs.tardis.dev/api/instruments-metadata-api) 明确说明只有
  `contractMultiplier` 变更保证准确完整；`priceIncrement`、`amountIncrement` 等其余
  变更为 best-effort，可能不完整。
- [Binance USDS-M Futures coverage](https://docs.tardis.dev/historical-data-details/binance-futures)
  显示市场数据自 2019-11-17 起可用，并有 exchange-native replay；这不等于逐次保存了
  `exchangeInfo` filters。页面的 `!contractInfo` 从 2023-07-24 起记录 listing、settlement
  和 bracket 更新，也未声明包含全部 PRICE_FILTER、LOT_SIZE 与 notional 变更。
- [Billing and Subscriptions](https://docs.tardis.dev/faq/billing-and-subscriptions) 说明 metadata
  API 只供 Pro/Business，历史可访问范围受订阅类型及周期影响。
- [Terms of Service](https://docs.tardis.dev/legal/terms-of-service) 允许内部业务使用和在客户
  系统保存数据；真实订阅时仍须把适用条款随证据归档。

因此 Tardis 保持计划指定的 `VENDOR_CONFIRMATION_REQUIRED`，不能作为当前主证据。

### Kaiko

- [Instrument reference data](https://docs.kaiko.com/rest-api/data-feeds/reference-data/basic-tier/exchange-trading-pair-codes-instruments)
  的公开字段是 instrument identity、class、exchange pair code 及交易起止时间，没有交易
  filters 或规则变更序列。
- [Data dictionary](https://docs.kaiko.com/explore-our-data/data-dictionary) 描述广泛的 CeFi
  derivatives 行情覆盖；[data versioning](https://docs.kaiko.com/rest-api/general/getting-started/data-versioning)
  约束的是 market datasets 的修订透明度，不是历史规则完整性。
- [Pricing and licensing](https://www.kaiko.com/about-kaiko/pricing-and-contracts) 明确方案与用途
  按合同定制；公开页面不足以确认本项目需要的原始规则留存及审计权限。

### Amberdata

- [Futures Exchange Reference](https://docs.amberdata.io/http/market/futures-exchanges-reference)
  的当前 schema 有 `limitsVolumeMin/Max`、`limitsCostMin`、`precisionPrice/Volume` 等字段；
  公开 Binance 示例的 `limitsCostMin` 是 null，且没有历史时点参数或变更记录。
- [Futures Instruments](https://docs.amberdata.io/http/market/futures-exchanges-information)
  给出 OHLCV、Funding、Trade 等行情的可用期，不证明 reference rules 在 DEV 内连续。
- [Ordering FAQ](https://www.amberdata.io/online-market-data-ordering-faq) 允许标准许可下商业
  使用但禁止再分发；原始历史规则快照的交付及审计条款仍需供应商确认。

### Coin Metrics

- [API v4 reference](https://docs.coinmetrics.io/api/v4/) 的 `/reference-data/markets`
  当前 schema 明确包含 `tick_size`、`order_price_increment`、`order_amount_increment`、
  `order_amount_min/max` 和 `order_size_min`，其中 `order_size_min` 定义为 amount × price。
- [Market metadata](https://gitbook-docs.coinmetrics.io/market-data/market-data-overview/market-metadata)
  说明这些是当前 listed-market reference data；公开 endpoint 没有 metadata 的
  `start_time`/`end_time` 查询，也没有逐次规则变更的生效时间或无遗漏保证。
- [FAQ](https://docs.coinmetrics.io/resources/faqs) 确认 derivatives 采用 exchange-reported
  symbol，并把 contract specifications 指向同一个当前 reference endpoint；这仍不是历史序列。
- [Master Terms](https://coinmetrics.io/wp-content/uploads/2023/06/Master-Terms-June-30-2023-.pdf)
  允许客户内部业务使用；实际购买时仍需保存适用 order form。原始 exchange-native
  历史规则快照的交付能力需要供应商书面确认。

## 供应商确认与 pilot 输入要求

只有供应商书面确认并提供可核验样例后才重做资格报告。确认必须逐项覆盖：

1. Binance USDⓈ-M perpetual 的明确 venue/market identity；
2. 2023-08-01 至 2025-01-30 的完整覆盖；
3. 每次 tick、step、min/max quantity、minimum notional 变更及精确 UTC 生效时刻；
4. 未遗漏历史变更的合同保证，而不是 best-effort 表述；
5. 可下载、可哈希的 exchange-native 原始字节及原始/采集时间戳；
6. 允许内部保存、复核和重复性审计的适用许可；
7. BTCUSDT 与 ETHUSDT 在 2023-11-02 minimum-notional 变化附近的闭区间样例。

样例只能作为本地文件进入后续 evidence bundle；适配器不会读取环境变量 token，也不会
直接调用付费 API。任何一项仍缺失，状态继续阻断，不能开始 registry promotion、248 symbol
批量处理、1m/Funding 请求或 DEV 研究。

复跑命令（返回码 1 是当前四个候选均未合格的预期结果）：

```powershell
.venv\Scripts\python.exe scripts\p9_rule_source_qualification.py
```
