# P8.4 下架合约小型试点报告

状态：已完成  
执行日期：2026-08-28  
试点：ANTUSDT，2024-03，15m/1h/4h/1d

## 结果

- Binance Public Data 月度 Kline 根目录快照包含 986 个原始 symbol 前缀；该数字未经
  PERPETUAL、USDT、生命周期和 ExclusionLedger 过滤，不能当作合格合约数量。
- 根目录 listing hash：
  `cc9b0202082a7170e50eed0d9e11918faca87b8b77d7db5beb666b07210f8032`。
- ANTUSDT 2024-03 四个周期的 inventory plan 均显示 ZIP 和 `.CHECKSUM` 可用，无缺月。
- 四个 ZIP 均通过官方 SHA-256，并通过完整自然月时间网格校验：

| 周期 | 行数 | Source SHA-256 | Normalized content hash |
|---|---:|---|---|
| 15m | 2,976 | `aacf5c6187914b457218ed6aea04b3590c17e28f4c88f40be28f0a7608fb23af` | `852706f67ae00ddd508258c5da5d19b463d0e16c08245923ed137a911c28cf9e` |
| 1h | 744 | `141d728cf1ad36d1cfd444c92eced600a04e62386f1608373ce8bf09c5868309` | `7382d4424f12351190fd13f18f0cf3b71246da5ad443eb714ac610bf2d770f4f` |
| 4h | 186 | `04d5c12fdb5217918bcc6f5e1826398831a7dc9c223aeec64633d04bb385693e` | `92fb19d5ae9af57d95d673ed72adca13bb2cc6e83e9e42a62f08531408d5f668` |
| 1d | 31 | `57a6495cfa4b8c384962cdc6a57760b11ddacbaadb208c09c00fb14c458755ef` | `126e2333e31a5cd2b1feaf9a9cb170c3d1a11a94d8c849930f5fb0c8a5b7a4ec` |

合计 3,937 根 Candle；原始 ZIP 150,300 bytes，Parquet 334,397 bytes。

## 生命周期证据

Binance 官方公告确认 ANTUSDT、DGBUSDT 和 CTKUSDT 永续合约将在
`2024-04-01T09:00:00Z` 自动结算，结算完成后下架。

- 公告代码：`ce74360a3fd444b2b35da7186f8dd0ad`。
- 公告发布时间：`2024-03-25T04:40:12.437Z`。
- 抓取响应 SHA-256：
  `84608ac82a162cfa7175b9ac3dec48645a9a41a32384ad5bc2915b07ff76e32f`。
- 官方来源：
  `https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode=ce74360a3fd444b2b35da7186f8dd0ad`。

P8.4 未取得同等级的官方 onboard 时间证据，因此 ANTUSDT pilot registry 状态保持
`UNVERIFIED`、`locked_research_eligible=false`。首个归档月或首根 Candle 不会被提升为
官方上线时间。

完整证据摘要：
`data/manifests/contract_evidence/ANTUSDT/pilot-2024-03.json`。

## 实现与验证

- 新增官方 S3 XML inventory 分页、内容哈希、缺月计划和幂等 manifest：
  `src/slagalpha/data/inventory.py`。
- 新增 CLI：`slagalpha archive-plan`，只列目录和生成计划，不下载 ZIP。
- 新增 6 项 inventory 测试和 1 项 CLI 边界测试。
- 官方 inventory 暴露了 Unicode symbol；symbol 校验已统一支持 Unicode 字母/数字和
  下划线，同时继续拒绝路径分隔、空白和控制字符。
- 全量 175 项测试通过，Ruff 通过，Mypy 48 个源码文件通过，CLI help 通过。

## 风险与 P8.5 闸门

- 官方归档在某些下架日期之后仍可能存在对象，因此归档首末日期不能替代生命周期证据。
- 本次访问共享出口 IP 时，`exchangeInfo` 曾返回 Binance `-1003` 临时封禁；P8.5 必须
  缓存 metadata 快照、限制请求频率并解析服务端解禁时间，不能密集重试。
- 986 个原始 archive prefixes 仍需与历史 metadata、官方公告和 ExclusionLedger 交叉
  核验；P8.5 不能直接全部下载。
- 36 个月全量扩展涉及显著网络与存储消耗，仍需用户明确确认后执行。
