# 开发恢复记录 — 2026-08-28

保存时间：2026-08-28 01:06（Asia/Hong_Kong）

## 当前状态

- P0–P7 已验收。
- P8.1 历史数据与动态标的池契约已完成，等待用户验收。
- 整体已验收进度：8/14，约 57%。
- P8 内部进度：1/6，约 17%。
- 未启动 P8.2，未下载或扩展 36 个月数据。
- 未连接账户、API Key、私有接口或真实交易。

## 今天完成

- 冻结正式研究区间：
  `[2023-08-01T00:00:00Z, 2026-08-01T00:00:00Z)`。
- 冻结多周期 warm-up 起点：`2023-02-01T00:00:00Z`。
- 冻结历史合约并集、生命周期证据、下架合约保留与 `UNVERIFIED` 排除规则。
- 冻结每日 00:05 UTC 选择、96 根 15m quote volume、00:15 生效、Top 30 及
  BTCUSDT/ETHUSDT 替换规则。
- 冻结归档 checksum、内容更新、来源冲突和 symbol-day Fail Closed 规则。
- 冻结两阶段数据获取边界：主周期数据优先，1m/Aggregate Trades 只按 replay 请求按需获取。

详细契约：`docs/p8-historical-universe-contract.md`  
阶段报告：`docs/p8-historical-universe-report.md`  
整体计划：`IMPLEMENTATION_PLAN.md`

## 已验证基线

- `pytest`：147 passed。
- `ruff check .`：通过。
- `mypy`：41 个源码文件通过。
- `python -m slagalpha --help`：通过。

## 明天恢复入口

1. 先由用户确认 P8.1。
2. 确认后只启动 P8.2：实现最小 `ContractRegistry`、`ExclusionLedger`、
   `UniverseSnapshot` 领域模型和契约测试。
3. P8.2 不下载历史数据，不实现每日选择算法；每日选择和 GS047–GS049 属于 P8.3。
4. P8.2 完成后立即汇报本任务结果与整体进度，等待下一次继续指令。

## 后续边界

- P8.4 才做官方归档 inventory/planning 和小型下架合约试点。
- P8.5 的 36 个月全量扩展涉及网络与存储消耗，必须先取得用户确认。
- P8.6 才集成 P7 replay、重复性验证和覆盖损失报告。
