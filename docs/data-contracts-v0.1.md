# Data Contracts V0.1

状态：P0 已验收冻结  
Schema 版本：`research-schema/0.1.0`  
适用：V0.1 离线数据、策略重放与回测

## 1. 契约原则

- V0.1 以 Parquet 和只追加 manifest 为持久化边界，不提前引入 PostgreSQL。
- 原始数据、规范数据、派生指标、策略输出和回测结果分层保存，不能相互覆盖。
- 所有记录携带 schema/version 信息，所有正式运行携带可复现 manifest。
- 金额和价格在持久化边界使用十进制定点值或十进制字符串，不使用二进制浮点作为权威价格。
- 时间一律为 UTC；序列化使用 ISO 8601 `Z` 或 Unix milliseconds，字段不能是无时区 datetime。
- 枚举序列化为稳定的大写字符串；展示文本不进入业务判断。
- 缺失必需字段、未知枚举、非有限数值或 schema 不兼容时 Fail Closed。

## 2. 规范序列化

用于哈希和 golden fixture 的 canonical JSON：

- UTF-8。
- Object key 按字典序。
- 无无意义空白。
- 时间统一为毫秒精度的 `YYYY-MM-DDTHH:mm:ss.SSSZ`。
- Decimal 以普通十进制字符串保存，禁止科学计数法，尾随零按对应交易规则精度保留。
- `null` 与字段缺失语义不同；必需字段禁止缺失。
- 内容哈希为 canonical bytes 的 SHA-256 小写十六进制。

## 3. ContractRule

权威来源为对应时间可见的 Binance exchange metadata 快照。

| 字段 | 类型 | 约束 |
|---|---|---|
| schema_version | string | 固定 schema 标识 |
| exchange | enum | `BINANCE_USDM` |
| symbol | string | Binance 原始大写 symbol |
| base_asset | string | 非空 |
| quote_asset | string | V0.1 必须 `USDT` |
| margin_asset | string | V0.1 必须 `USDT` |
| contract_type | string | 必须 `PERPETUAL` |
| status | string | 创建实时池时必须 `TRADING` |
| onboard_date | UTC timestamp/null | 原始 metadata 值；缺失时另用推导字段 |
| derived_first_candle_at | UTC timestamp | 首根通过校验的官方 1D Candle |
| inferred_delisted_at | UTC timestamp/null | 最后有效数据后的推导时间 |
| tick_size | Decimal | > 0 |
| step_size | Decimal | > 0；V0.1 不计算仓位但仍保存 |
| min_qty/max_qty | Decimal/null | 来自 filter |
| min_notional | Decimal/null | 来自 filter |
| effective_from | UTC timestamp | 快照生效时间 |
| source_snapshot_hash | sha256 | 原响应/注册表快照哈希 |

若历史 symbol 缺少当时 exchange metadata，允许建立版本化 `contract_registry` 推导记录，但必须包含：推导方法、证据来源、人工复核状态和置信等级。锁定测试前，任何 `UNVERIFIED` 合约不得进入历史标的池。

## 4. Candle

唯一键：`(exchange, symbol, interval, open_time)`。

| 字段 | 类型 | 约束 |
|---|---|---|
| schema_version | string | `candle/0.1.0` |
| exchange | enum | `BINANCE_USDM` |
| symbol | string | 对应 ContractRule |
| interval | enum | `1m`、`15m`、`1h`、`4h`、`1d` |
| open_time | UTC timestamp | 对齐 interval 网格 |
| close_time_exclusive | UTC timestamp | `open_time + interval` |
| source_close_time | Unix ms | 保留交易所原字段 |
| open/high/low/close | Decimal | > 0；low <= open/close <= high |
| base_volume | Decimal | >= 0，仅记录 |
| quote_volume | Decimal | >= 0，策略量价权威字段 |
| trade_count | integer | >= 0 |
| taker_buy_base_volume | Decimal | >= 0 |
| taker_buy_quote_volume | Decimal | >= 0 |
| is_closed | boolean | 正式数据必须 true |
| source | enum | `PUBLIC_ARCHIVE` / `REST` / `WEBSOCKET` |
| source_file_hash | sha256/null | Archive 必填 |
| ingested_at | UTC timestamp | 仅审计，不参与策略 |

冲突处理：

1. 同唯一键、同内容：幂等去重。
2. 同唯一键、内容不同：不自动选择赢家，写入 `data_anomalies` 并冻结该 symbol/interval/time range。
3. Archive 与 REST 校验不一致：保留两份原始证据，人工或官方更新清单解决前不得用于正式回测。

Parquet 建议分区：

```text
data/normalized/klines/
  exchange=BINANCE_USDM/interval=15m/symbol=BTCUSDT/year=2026/month=08/*.parquet
```

## 5. DataAnomaly

| 字段 | 类型 | 说明 |
|---|---|---|
| anomaly_id | sha256 | 确定性 ID |
| detected_at | UTC timestamp | 检测时间 |
| symbol/interval | string | 影响范围 |
| start_time/end_time | UTC timestamp | 影响区间 |
| kind | enum | GAP、DUPLICATE_CONFLICT、OHLC_INVALID、VOLUME_INVALID、CHECKSUM_FAILED、SOURCE_MISMATCH |
| severity | enum | WARNING / BLOCKING |
| source_refs | array | 文件或请求哈希 |
| resolution | enum | OPEN / OFFICIAL_REPLACEMENT / MANUAL_VERIFIED / EXCLUDED |
| resolution_note | string/null | 不参与策略 |

`BLOCKING + OPEN` 时，影响区间禁止生成正式信号。

## 6. ArchiveFileManifest

每个下载文件一条：

| 字段 | 类型 | 约束 |
|---|---|---|
| url | string | Binance 官方 Public Data URL |
| checksum_url | string | 同目录 `.CHECKSUM` |
| expected_sha256 | string | 官方值 |
| actual_sha256 | string | 本地计算值 |
| checksum_verified | boolean | 正式解析必须 true |
| downloaded_at | UTC timestamp | 审计字段 |
| file_size | integer | >= 0 |
| parser_version | string | 可追溯 |
| parsed_row_count | integer | >= 0 |
| normalized_content_hash | sha256 | 规范化结果哈希 |

失败下载、404 或 checksum 错误不能用空文件或 REST 静默替代；应记录状态，由明确的 gap-fill 任务处理。

## 7. 历史合约注册表与排除表

### 7.1 合约生命周期

- 优先使用官方 onboard/delist metadata。
- 缺少历史 metadata 时，`derived_first_candle_at` 仅作为候选上市时间证据，不自动等同于官方上线时间。
- 合约在 selection time 已有至少 90 个完整自然日有效历史才合格。
- 下架合约在其历史有效期内必须保留，不能因当前 exchangeInfo 不存在而删除。
- 无法验证 `PERPETUAL + USDT quote + USDT margin` 的历史合约，在锁定测试前排除并报告覆盖损失。

### 7.2 ExclusionLedger

稳定币互换、指数、杠杆代币、TradFi 类合约和已知异常标的使用版本化 ledger，不依赖 symbol 名称的临时模糊判断。

| 字段 | 类型 | 说明 |
|---|---|---|
| symbol | string | 合约 |
| effective_from/to | UTC timestamp | 排除有效期 |
| category | enum | STABLE_SWAP / INDEX / LEVERAGED_TOKEN / TRADFI / DATA_QUALITY |
| reason | string | 人可读原因 |
| evidence_ref | string | 官方资料或异常记录引用 |
| reviewed_by | string | `USER` 或明确的审阅标识 |
| ledger_version | string | 参与 universe version |

新增或改变历史排除项会改变 universe，必须新建 universe 版本并重跑开发/验证结果。

## 8. UniverseSnapshot

### 8.1 选择时间

- 每日 `00:05 UTC` 生成候选池。
- 仅使用截止 `00:00 UTC` 已收盘的最近 96 根 15m Candle。
- `rolling_quote_volume_24h = sum(96 根 quote_volume)`。
- Snapshot 从 `00:15 UTC` 的首个 15m 扫描开始生效，到下一版本生效前结束。
- 历史回放和实时系统使用完全相同的 96 根计算，不依赖今天的 Top 30。

### 8.2 选择算法

1. 应用 ContractRule、90 天历史、数据完整性和 ExclusionLedger。
2. 按 `rolling_quote_volume_24h` 降序、symbol 升序作为稳定 tie-break 排名。
3. 取总数 30，不是 30+2。
4. BTCUSDT、ETHUSDT 若合格但不在前 30，则替换排名最低的非固定 symbol。
5. BTC/ETH 本身数据异常或不合格时不强行加入，记录阻断原因。

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| universe_version | sha256 | 规范内容哈希 |
| selected_at/effective_from/to | UTC timestamp | 时间边界 |
| ranking_window_start/end | UTC timestamp | end 为 00:00 |
| member_count | integer | 通常 30 |
| members | ordered array | symbol、rank、quote volume、forced flag、eligibility refs |
| contract_registry_version | string | 输入版本 |
| exclusion_ledger_version | string | 输入版本 |
| candle_dataset_hash | sha256 | 排名数据版本 |

## 9. IndicatorSnapshot

指标快照只用于审计和加速，权威结果必须能从 Candle 重算。

| 字段 | 类型 | 说明 |
|---|---|---|
| indicator_version | string | 算法版本 |
| candle_key | object | 对应唯一 Candle |
| sma30/60/90/180 | float64/null | 窗口不足为 null |
| atr14 | float64/null | Wilder ATR |
| quote_volume_median20 | float64/null | 定义窗口随上下文保存 |
| pivot_events | array | 本 Candle 收盘后新确认的 Pivot |
| input_start/end | UTC timestamp | 使用窗口 |
| input_hash | sha256 | 输入 Candle 内容哈希 |

禁止把未来确认的 Pivot 回填成其中心 K 线时“当时已知”的特征。

## 10. ScanEvaluation 与 Setup

每个 symbol 每个 15m 收盘至少保存一条 ScanEvaluation：

| 字段 | 类型 | 说明 |
|---|---|---|
| scan_id | sha256 | run + symbol + close time |
| run_id | sha256 | 对应 RunManifest |
| evaluated_at | UTC timestamp | 逻辑扫描时间 |
| latest_candle_keys | map | 15m/1h/4h/1d 输入键 |
| data_status | enum | OK / NOT_READY / INVALID |
| direction_4h | enum | LONG / SHORT / NEUTRAL |
| regime | enum | TRENDING / COMPRESSED / INVALID |
| daily_context | enum | ALIGNED / MIXED / BLOCK_LONG / BLOCK_SHORT |
| pullback_state | enum | NONE / WATCHING / SHALLOW / STANDARD / DAMAGED / RESET |
| trigger_candidates | array | A/B 证据 |
| gate_reached | string | G01–G12 |
| reason_codes | ordered array | 全部通过/失败原因 |

Setup 只在 WATCHING 或有效回踩出现时创建：

| 字段 | 类型 | 说明 |
|---|---|---|
| setup_id | sha256 | strategy + symbol + direction + episode start |
| episode_start | UTC timestamp | 首根 1H 回踩 open time |
| pullback_class | enum | WATCHING / SHALLOW / STANDARD |
| status | enum | ACTIVE / INVALIDATED / ENDED |
| started_at/ended_at | UTC timestamp/null | 状态时间 |
| evidence | object | MA、ATR、volume、reason codes |

## 11. Signal 与 TradePlan

### Signal

| 字段 | 类型 | 说明 |
|---|---|---|
| signal_id | sha256 | 策略文件定义的确定性 ID |
| setup_id | sha256 | 来源 Setup |
| symbol/direction | enum | LONG/SHORT |
| primary_trigger | enum | MA_RECLAIM / SWEEP_RECLAIM |
| all_triggers | ordered set | 一个或两个原因 |
| confirmation_candle_key | object | 15m |
| created_at | UTC timestamp | confirmation close |
| expires_at | UTC timestamp | 右开边界 |
| status | enum | ARMED/TRIGGERED/INVALIDATED/EXPIRED/MISSED/CLOSED |
| score_total | integer | 0–100 |
| score_components | object | 五大项及子项 |
| reason_codes | ordered array | 可审计 |
| version_refs | object | strategy/parameter/universe/schema |

### TradePlan

| 字段 | 类型 | 说明 |
|---|---|---|
| signal_id | sha256 | 一对一 |
| tick_size | Decimal | 生成时快照值 |
| entry_theoretical | Decimal | 合法 tick |
| invalidation_price | Decimal | 未加 Stop buffer 的结构点 |
| stop_price | Decimal | 加 buffer 后合法 tick |
| stop_buffer | Decimal | 可重算 |
| atr_at_confirmation | Decimal | 15m ATR |
| risk_per_unit | Decimal | abs(entry-stop) |
| tp1/tp2 | Decimal | 合法 tick |
| tp1_source/tp2_source | enum | STRUCTURE_15M / STRUCTURE_1H / ATR_EXTENSION |
| gross_rr_tp1/tp2 | Decimal | 生成时数值 |
| cost_model_version | string | 不覆盖旧计划 |

所有计算中间量（raw entry、raw stop、舍入方向、结构 zone ID）必须保存在 evidence 中。

## 12. SignalEvent、PaperTrade 与 Fill

事件日志只追加：

| 字段 | 类型 | 说明 |
|---|---|---|
| event_id | sha256 | entity + type + exchange time + source event ref |
| entity_id | sha256 | signal_id 或 trade_id |
| event_type | enum | ARMED、ENTRY_FILL、INVALIDATED、EXPIRED、MISSED、TP1_FILL、STOP_FILL、TP2_FILL、TIME_EXIT、CLOSED |
| exchange_time | UTC timestamp | 排序依据 |
| observed_at | UTC timestamp | 接收/回放时间，不参与历史排序 |
| source | enum | KLINE_1M / AGG_TRADES / TIMER / SYSTEM |
| source_ref | string | Candle key 或文件哈希+行号 |
| payload | object | canonical event 数据 |

PaperTrade：

- `trade_id` 从 signal_id 确定性派生。
- 保存理论与模拟 Entry/Exit、数量比例、Gross/Net PnL、Gross/Net R、Fee、Slippage、Funding、MAE、MFE、持仓时长和终止原因。
- V0.1 以单位仓位或 `1R` 归一化统计，不实现真实账户仓位。
- 同一 symbol 同时最多一条 ACTIVE PaperTrade。

Fill：

- 每次 Entry、TP1、TP2、Stop、Time Exit 分别保存。
- `side`、`quantity_fraction`、theoretical price、simulated price、fee、slippage 和 source 顺序证据必填。
- TP1 后保本 Stop 的激活是独立事件，生效时间不得早于下一根 1m open_time。

## 13. ParameterVersion

| 字段 | 说明 |
|---|---|
| parameter_version | 人类可读 semver + 内容哈希 |
| strategy_version | 所属策略语义版本 |
| values | 五个允许敏感度参数及全部冻结常量 |
| created_at | 审计时间 |
| dataset_role | DEV / VALIDATION / LOCKED_TEST / FORWARD |
| frozen | 是否冻结 |
| parent_version | 来源版本/null |
| change_reason | 不得为空 |

锁定测试只能引用 `frozen=true` 的 ParameterVersion。

## 14. RunManifest

任何正式测试或报告必须包含：

```text
run_id
run_kind
started_at / finished_at
code_commit
dirty_worktree
strategy_version
parameter_version
schema_versions
contract_registry_version
exclusion_ledger_version
universe_versions
candle_dataset_hashes
archive_manifest_hash
cost_model_version
random_seed (仅 bootstrap 使用)
command_arguments
environment_lock_hash
result_content_hash
```

正式可复现报告要求：Git commit 非空、`dirty_worktree=false`、输入哈希完整。开发中的快速试验可以 dirty，但必须明确标记 `NON_REPRODUCIBLE_DEV_RUN`，不得用于晋级。

## 15. 时间切分契约

- 按全局研究时间轴切分，不按每个 symbol 各自百分比切分。
- 先确定共同的研究起止，再按时间点划分 50% DEV、25% VALIDATION、25% LOCKED_TEST。
- 某 symbol 在其 90 天成熟前没有资格，但不改变全局切分边界。
- 所有 dataset row 带 `dataset_role`；策略运行 API 必须显式接收允许的 role。
- 参数研究命令默认拒绝读取 LOCKED_TEST。
- 首次锁定测试运行写入不可变审计记录；再次运行只能是完全相同输入的复现，不得换参数。

## 16. 目录与提交边界

建议：

```text
data/raw/                 # 官方压缩包，默认不提交 Git
data/manifests/           # checksum、下载与解析 manifest，可提交
data/normalized/          # Parquet，默认不提交
data/registry/            # 合约生命周期、排除 ledger，可提交小型版本文件
artifacts/runs/<run_id>/  # 报告、事件和指标摘要，按策略决定是否提交
tests/fixtures/           # 小型、脱敏、固定 golden 数据，可提交
```

任何路径都不能包含 API Key、Cookie、账户 ID 或私有账户数据。V0.1 配置 schema 不提供这些字段。

## 17. 已知数据风险闸门

以下问题不允许以假数据绕过：

1. 历史 exchange metadata 无法覆盖下架合约。
2. Public Archive 曾更新历史文件，checksum 变化需要版本追踪。
3. Funding 历史缺口会使跨结算交易缺少 Net R。
4. Aggregate Trades 缺失会触发不利顺序假设，报告必须统计该比例。
5. 历史 ContractRule 无法验证时，必须报告 universe 覆盖损失，锁定测试不得纳入未验证合约。

这些风险在 P2/P8 通过真实数据验证前保持 OPEN，不能在 P0 声称已解决。
