# P2 Historical Data Minimum Loop Report

状态：P2 已验收  
执行日期：2026-08-27  
样本：Binance USD-M `BTCUSDT / 15m / 2024-01`

## 交付结果

- 官方 ZIP 与 `.CHECKSUM` 下载成功。
- expected SHA-256 与 actual SHA-256 完全一致。
- ZIP 只包含预期 CSV，解析 2,976 行。
- UTC 时间范围为 `2024-01-01T00:00:00Z` 至 `2024-01-31T23:45:00Z`。
- 完整月份应有 31 × 24 × 4 = 2,976 根 15m K 线，实际一致。
- 相同重复行 0，冲突重复 0，时间缺口 0。
- OHLC、quote volume、trade count 和 close-time 网格校验通过。
- Decimal 权威字段以 `decimal128(38, 18)` 写入 Parquet。
- 第二次运行只重新读取官方 checksum，没有重复下载 ZIP；结果哈希一致。

## 可追溯哈希

```text
source_sha256:
76953983fcd4cc35ac181c4a1c69d28cbb4ef8b983021aac84a111ea4e82ef69

normalized_content_hash:
36b636be06cc6707a54e58ddbbd5d079ce85427641e99121cc362576442f8a1a

parquet_sha256:
661903926da68e17995ba51033630d341aa00e553a79d6a188eef072947c9d1e
```

## 自动验证

- pytest：31 passed。
- Ruff：通过。
- mypy strict：19 个源文件无问题。
- pip check：依赖无冲突。

## 存储边界

- `data/raw/`：官方 ZIP，Git 忽略。
- `data/normalized/`：Parquet，Git 忽略。
- `data/manifests/`：小型下载与规范化证据，可由 Git 跟踪。

## P2 未覆盖范围

- 尚未扩展到多个周期或多个合约。
- 尚未实现 REST 补洞或 Archive/REST 交叉核验。
- 尚未处理官方历史文件替换的自动迁移；发现本地与官方 checksum 冲突时当前会保留原文件并 Fail Closed。
- 尚未实现历史合约注册表、Top 30 或下架合约恢复。
- 尚未实现 SMA、ATR、Pivot 或任何交易策略。

以上内容必须在后续对应阶段单独实现，不能把本次单月成功视为完整历史数据系统已经完成。
