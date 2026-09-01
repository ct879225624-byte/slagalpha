# P8 历史数据与动态标的池实施报告

状态：已完成；P8.1–P8.6 已验收  
更新时间：2026-08-30

## P8.1 已验收内容

- 冻结 36 个完整 UTC 自然月的正式观察区间：
  `[2023-08-01T00:00:00Z, 2026-08-01T00:00:00Z)`。
- 冻结从 2023-02-01 开始的多周期 warm-up 数据边界。
- 明确历史候选合约并集、生命周期证据等级及下架合约保留规则。
- 明确每日 00:05 UTC 选择、96 根 15m quote volume、00:15 生效、Top 30 与
  BTC/ETH 替换规则。
- 明确月度归档主源、checksum、归档更新、冲突和 symbol-day Fail Closed 规则。
- 冻结两阶段下载：主周期/Universe 数据优先，1m/Aggregate Trades 只按冻结的 replay
  区间按需获取。
- 官方 API 和 Public Data 文档均于 2026-08-28 核验；未使用账户或私有接口。

完整规则见 `docs/p8-historical-universe-contract.md`。

## P8.2 完成内容

- 新增不可变 `ContractRegistryEntry` / `ContractRegistry`：
  - 固定 Binance USD-M、USDT quote/margin 和 PERPETUAL 边界。
  - 校验 UTC、tick/step、数量范围、证据字段和不重叠生命周期。
  - `derived_first_candle_at` 不会自动填入 `onboard_date`；`UNVERIFIED` 明确不能进入
    locked research。
- 新增不可变 `ExclusionLedgerEntry` / `ExclusionLedger`：
  - 只接受 exact symbol、明确分类、有效期、证据和审核者。
  - entry 与 ledger 版本必须一致；禁止重复和非 canonical 顺序。
- 新增 `UniverseMember`、`UniverseBlockedCandidate`、`UniverseSnapshot`：
  - 校验每日 00:05 选择、24 小时排名窗口、00:15 生效及下一日失效。
  - 校验最多 30 个成员、连续排名、symbol 唯一和入选/阻断互斥。
  - 保存 registry、ledger、candle dataset 和 universe 版本证据。
- 本阶段只提供领域契约，未实现 Top 30 选择、90 天资格计算或 Candle 聚合。

实现：`src/slagalpha/domain/universe.py`  
测试：`tests/test_universe_models.py`

### P8.2 验证

- 新增契约测试：13 项通过。
- 全量测试：160 项通过。
- Ruff：通过。
- Mypy：43 个源码文件通过。
- CLI help：通过。
- 未发起网络请求，未下载历史数据。

## P8.3 完成内容

- 新增 `build_daily_universe` 纯函数和 `daily-top30/0.1.0` 算法版本。
- 对每个 UTC 日严格使用 `[D-1 00:00, D 00:00)` 的 96 根 15m Candle；窗口外数据
  不参与成交额或内容哈希。
- 实现已核验生命周期、精确 90 天边界、active exclusion 和 15m/1h/4h/1d manifest
  证据闸门。
- 实现 quote volume 降序、symbol 升序 tie-break、总数 30 和 BTC/ETH 固定成员替换。
- 异常只阻断对应 symbol-day，并记录稳定 reason codes 和 evidence refs。
- Universe 与排名 Candle 输入均生成 canonical SHA-256；相同输入重复结果一致。

实现：`src/slagalpha/data/universe.py`  
测试：`tests/test_universe_selection.py`

### P8.3 验证

- 新增测试：7 项通过，覆盖 GS047、GS048、GS049。
- 全量测试：167 项通过。
- Ruff：通过。
- Mypy：45 个源码文件通过。
- CLI help：通过。
- 未发起网络请求，未下载历史数据。

## P8.4 完成内容

- 实现 Binance Public Data 官方 S3 inventory 分页、canonical hash、月度缺口计划和
  content-addressed manifests。
- 新增 `archive-plan` CLI；计划命令不下载 ZIP。
- 保存月度 Kline 根目录 986 个原始 symbol prefixes 的版本快照。
- 完成 ANTUSDT 2024-03 下架合约小试点：四周期 4 个 ZIP 均通过 checksum 和完整月
  网格校验，共 3,937 行。
- 保存 Binance 官方 `2024-04-01T09:00:00Z` 自动结算/下架公告证据及响应哈希。
- 未取得官方 onboard 时间，因此 pilot 保持 `UNVERIFIED`，没有进入 locked universe。
- 根据真实 inventory 扩展安全 symbol 校验以支持官方 Unicode symbol，不放宽路径安全。

详细结果见 `docs/p8-delisted-pilot-report.md`。

### P8.4 验证

- 新增 inventory/CLI/Unicode 边界测试：8 项。
- 全量测试：175 项通过。
- Ruff：通过。
- Mypy：48 个源码文件通过。
- CLI help：通过。
- 真实下载仅 4 个小型 ZIP，共 150,300 bytes；未扩展 36 个月数据。

## P8.5 完成内容

- 保存 2026-08-28 官方 `exchangeInfo` 快照：882 条合约元数据；当前
  `TRADING + PERPETUAL + USDT quote/margin` 为 524 个，其中 523 个有归档目录。
- 对 986 个官方根目录、4 个周期、`2023-02` 至 `2026-07` 扫描 3,944 个精确
  S3 前缀；全部成功，无名称后缀推测。
- 发现 73,340 个带官方 `.CHECKSUM` 的 ZIP，精确原始容量 2,661,384,964 bytes；按
  4 倍保守系数预计 10,645,539,856 bytes，低于 20 GiB 门槛。
- 完成 73,340 个 ZIP 下载和 SHA-256 校验：新下载 73,335、复用 5、失败 0；磁盘
  文件数、验证回执数和总字节与容量计划完全一致。
- 生成 coverage-loss 报告：观察期 16,948 个 symbol-month 中，14,933 有当前官方
  目标合约身份元数据，646 缺少元数据而 Fail Closed，1,369 由当前官方元数据判定为
  非目标合约；四周期对象级缺口为 0。
- coverage-loss 仍明确标记 `locked_research_ready=false`：当前快照不能证明所有历史
  contract-rule 生效区间，且 ZIP 尚未全部提升为通过 Candle 网格验证的数据集。

内容寻址证据：

- 容量计划：`cb3aa1a9c634e8bd84202347b50b0b3ea92722c7c6cf7d9a0a8ce6f60c21363d`
- 下载批次：`9daba9a0aa2e4a61e3bb568cecec4e2c84166758dc24849ab974f2c4545edc0d`
- 覆盖报告：`32cfb3d2549e6bfcc44315af157a94cb883bcfc5fed5ca0c6c064945a4a9fb43`

### P8.5 验证

- 全量测试：190 项通过。
- Ruff：通过。
- Mypy：56 个源码/测试文件通过。
- CLI help：通过。
- 未访问账户、API Key、私有接口或真实交易。

## 尚未执行

- P8.6 历史 registry 锁定边界、动态 Universe 全期运行、P7 replay 集成与重复性验收。

## P8.6a 完成内容

- 73,340 个归档全部经过本地 ZIP 布局、Decimal、OHLC、时间边界、重复和网格验证。
- 73,313 个归档成功生成 69,189,525 行规范 Candle 和 5,434,152,441 bytes Parquet。
- 27 个归档因真实网格断层 Fail Closed；断层集中在 9 个 symbol-month，没有填充或
  放宽验证器。
- 8 个重新上线 symbol 的 15m 断层右边界与当前官方 `onboardDate` 完全一致；
  AERGOUSDT 缺少当前元数据，继续 `UNVERIFIED`。
- 详细证据见 `docs/p8-candle-validation-report.md`。

## P8.6b Registry 草案

- 从固定 `exchangeInfo` 快照的 882 条合约和已验证 15m Candle 生成 649 条 registry
  草案记录。
- 649 条全部为 `UNVERIFIED`、`locked_entry_count=0`；当前 filters 没有被静默声明为
  历史规则。
- 229 条当前元数据不符合目标 identity/status；DOSUSDT、FTTUSDT、RAYUSDT、SCUSDT
  因研究边界内 Candle/生命周期证据不足跳过。
- registry version：
  `registry-draft-f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0`。
- draft report：
  `f61e9a4168437c2a37b0604372c2af3865aeab3fabc7f50b350eba6eebe0225c`。

当前不能自动把草案升级为锁定研究 registry：`exchangeInfo` 只证明抓取时的 tick/step，
不能证明 2023-08 至 2026-07 的规则始终未变。若直接把当前 filters 回填历史，会引入
不可审计的价格舍入与成交规则偏差。

用户于 2026-08-30 确认采用拆分方案后，新增不含任何 filters 的 identity/lifecycle
registry：649 条均为 `VERIFIED`，版本为
`identity-registry-739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36`。
原 contract-rule 草案继续全部 `UNVERIFIED`，只有 identity registry 可以参与 Universe；
Trade Plan/P7 replay 仍必须取得对应日期的独立规则证据。

## P8.6c 全周期动态 Universe

- 生成并持久化 `[2023-08-01, 2026-08-01)` 的 1,096 个日度 Universe 快照。
- 首轮与独立断点第二轮均覆盖全部 1,096 天；第二轮强制重新读取规范数据并重算。
- 两轮日度版本序列哈希一致：
  `2ae73f816286e7932f88d2e7c8859a3c5ad8146df4b0ae59dc242faaae1412f8`。
- 全期成员日合计 32,880，symbol-day 阻断记录合计 351,250；批次失败日期为 0。
- 批次 run version：
  `d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b`。

## P8.6d P7 Fail-Closed 联调

- 新增历史 Universe/contract-rule/P7 前置门控；只有当时生效且 `VERIFIED` 的规则区间
  才会进入原 P7 scheduler。
- 合成集成测试证明：已验证且 tick 匹配的 case 可执行；未验证规则和 tick 不匹配均在
  读取或校验 1m replay 数据前阻断。
- 真实 2026-07-31 Universe 共 30 个成员，面对 contract-rule 草案时 30 个全部因
  `CONTRACT_RULE_UNVERIFIED` 阻断，0 个被错误放行。
- 真实门控报告哈希：
  `fce07c707c7eed38c00bf37d097f3214111e1e5907337b5c3adbadd30d0dbee3`。
- 该结果明确表示“研究执行输入尚不完整”，不表示策略收益失败，也不使用当前 filters
  回填历史。

## 当前风险

- 当前 `exchangeInfo` 无法单独重建历史状态，历史 registry 必须保留证据和复核状态。
- Binance Public Data 允许官方更新既有归档，不能只用 URL 判断输入是否未变化。
- 104 个有目标区间归档的 symbol 当前缺少元数据，观察期涉及 646 个 symbol-month；
  这些对象保留，但不会因名称后缀而推测性纳入。
- 1m 与 Aggregate Trades 仍只允许按 P7 replay 请求按需下载，未预取全历史。

## 下一验收闸门

P8 已完成。下一阶段为 P9 研究切分与冻结；任何需要 Trade Plan/P7 的真实历史执行仍须
先补齐权威历史 contract-rule 区间，否则研究结论必须保持 `BLOCKED`，不能输出伪收益。
