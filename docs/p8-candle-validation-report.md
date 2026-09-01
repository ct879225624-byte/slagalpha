# P8.6a Candle 全量验证报告

状态：完成（27 个真实断层归档已隔离，不填充）  
验证日期：2026-08-29–2026-08-30

## 结果

- 输入：73,340 个已通过官方 SHA-256 的月度 ZIP。
- 成功规范化：73,313 个 ZIP，69,189,525 行。
- Parquet：73,313 个文件，5,434,152,441 bytes；逐文件 normalization receipt 数量一致。
- 隔离：27 个 ZIP，全部为 `CandleGapError`；没有 checksum、ZIP 布局、OHLC、Decimal、
  重复冲突或越界时间错误。
- 成功数据集哈希：
  `154f1980d63e5342cb3408ea30dab3e6f84da43993a4fa5e073d84e56599c430`。
- 批次结果哈希：
  `c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`。

## 27 个隔离归档

断层集中在 9 个 symbol-month，每个涉及 15m/1h/4h；1d 文件没有日级断层：

| Symbol | Month | 判定 |
|---|---|---|
| AERGOUSDT | 2025-04 | 当前 `exchangeInfo` 无元数据，继续 `UNVERIFIED` |
| AIAUSDT | 2026-01 | 断层后首根 15m 等于官方 `onboardDate` |
| CTKUSDT | 2025-04 | 断层后首根 15m 等于官方 `onboardDate` |
| CVCUSDT | 2025-05 | 断层后首根 15m 等于官方 `onboardDate` |
| CVXUSDT | 2025-07 | 断层后首根 15m 等于官方 `onboardDate` |
| LITUSDT | 2025-12 | 断层后首根 15m 等于官方 `onboardDate` |
| MAVIAUSDT | 2025-03 | 断层后首根 15m 等于官方 `onboardDate` |
| PUMPUSDT | 2025-07 | 断层后首根 15m 等于官方 `onboardDate` |
| SLPUSDT | 2025-07 | 断层后首根 15m 等于官方 `onboardDate` |

1h/4h 的断层后 open time 为 `onboardDate` 所在周期的网格起点，因此可能早于精确
`onboardDate` 15–225 分钟；这不把周期视为上市前可交易。Universe 仍以精确
`onboardDate + 90 days` 判定年龄。

## 验收解释

- 不降低原有连续网格校验，不把两段生命周期拼接成一条连续行情。
- 不插值、不补零、不复制上一根，也不改写官方 ZIP。
- 27 个归档保留原始文件和失败回执，后续对应 symbol-day 使用
  `CANDLE_GAP` / `CONTRACT_UNVERIFIED` Fail Closed。
- 8 个有当前官方重新上线元数据的 symbol，其断层月早于重新上线后 90 天合格边界，
  因此不会损失已核验新生命周期内的合格 Universe 日。
- 本报告只完成 Candle 数据验证；历史 contract-rule 生效区间仍是 P8.6b 的独立闸门。
