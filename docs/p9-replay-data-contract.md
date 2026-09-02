# P9 按请求限定的回放数据契约

日期：2026-09-02。工程输入准备；不获取网络数据、不批准规则、不运行策略。

## 1. 有限 DEV 请求

`research/replay_inputs.py` 接受既有 P6 TakeProfit、冻结 split/计划/参数、Universe 和规则
注册表。先重读模型并核验参数绑定，计划有任何既有阻断时直接拒绝。

- P6 必须 accepted；Entry/Stop 按候选 Stop 缓冲和 TTL 重算后必须完全一致。
- P7 holding 参数取自同一候选，不从调用方额外设置。
- 确认时刻必须在历史 Universe 有效区间内，symbol 必须属于该池。
- 整个请求窗口必须在 DEV 内；不截断跨验证集/锁定集窗口，不按实际交易结果缩短。
- 一条 VERIFIED 历史规则必须覆盖整个窗口，tick 与计划相同，生命周期有效。
  首版不支持跨规则变更拼接，因为 P7 当前使用固定 tick；即使确认时有效，后续失效也拒绝。
- 绑定原始模型的内容哈希，保存后仍需用 `require_replay_data_request_binding` 重验原证据。

区间为 `[confirmation_close, last_possible_TIME_EXIT_candle_close)`：
Entry 窗口右开，最后可能入场分钟为 `expires_at - 1m`，取其 15m bucket，
加 `max_holding_bars × 15m` 得最晚 TIME_EXIT 的 open，再加 1m 作为右开终点。
默认 TTL=4、holding=32 时请求 526 根 1m（不是实际持仓 526 分钟）。
数据末端保留完整 Candle，因为 P7 的 deadline 风险路径比较也会使用其 high/low。

请求文件只表达数据范围，不宣称完成所有策略、指标或研究执行验证：
`download_authorized=false`、`research_authorized=false`。它不证明单个 Universe 快照已经
属于完整日度序列，也不重算 Pivot/MA/TP 的上游证据；正式执行入口仍须核验完整输入链。
当前真实敏感度计划仍被历史规则阻断，因此没有写出任何真实 accepted replay 请求。

请求写入器只写内容寻址 JSON；没有下载器或订单入口。所有正向测试都在 pytest 临时目录，
使用明确的合成计划与 VERIFIED 测试规则，不改变真实注册表。

## 2. 后续工件顺序

有限请求 → 1m 完整区间及来源校验 → Funding 真实结算日程/数值覆盖校验 → 再验上游证据。
不得以空请求列表、缺失数据或 mock 解除门禁。Aggregate Trades 保持可选的后续输入。
