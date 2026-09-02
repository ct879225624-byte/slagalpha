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

## 3. 单请求的原始 REST 响应工件

`research/replay_market_data.py` 只读取已经保存的响应，没有网络调用。
每页描述固定公开 base URL、endpoint、symbol、查询起止毫秒、limit、观测时间、
响应文件相对路径和原始 SHA-256。拒绝 API 错误对象、重复 JSON key、逃逸路径、
符号链接及超过 4 MiB 的单页响应；所有查询页须按顺序无重叠、无空档覆盖请求区间。

1m：每页按整分钟切分，必须逐行覆盖全部请求分钟；复用现有 Decimal Candle 规范化器，
按月份分组后拼接；保留 `source=REST` 与每页原始文件哈希。完整 OHLC、volume、
交易笔数、来源 close time 和收盘状态检查不变。不会排序、去重或补造错误响应。

Funding：使用返回结算时刻构造已有 P7 `FundingDataset`，不硬编码周期；
查询区间按包含两端的毫秒语义覆盖，应用层仍使用原 P7 持仓时刻规则。
达到 limit 的页拒绝，需细分查询区间证明未截断；缺 rate/mark、重复或乱序结算、
未知字段和 Special 类型拒绝。全空历史也拒绝，需要独立日程证据才能认定无结算。
部分查询页可以为空，但所有页合并后必须有结算记录；本版本暂不接受无结算窗口。

工件绑定 request hash、查询描述、记录数与规范化内容哈希。每次加载都重新校验原始字节
和规范化结果；不能只读取汇总 JSON 后认为数据未变化。全部通过仍固定
`research_authorized=false`，不代替原始请求证据重验或正式 DEV 请求集合完整性验收。
人工提供的查询描述/观测时刻并非交易所签名，来源真实性仍需后续受控采集链证明。

接口核对日期：2026-09-02。[Binance 当前官方市场数据文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
确认 `/fapi/v1/klines` 12 字段响应及最大 limit=1500；Funding limit 最大 1000、
升序、起止均包含，以及可选 `rateType`。兼容历史未提供 `rateType` 的普通记录，
显式 `Special` 不按普通 Crypto Funding 入账。

目前测试响应全部为合成 fixtures；没有抓取、落地或批准任何真实 replay 输入。
正式运行的 1m/Funding 聚合输入门禁尚未接入这些单请求工件，仍保持阻断。

## 4. 同一请求的联合加载

`research/replay_loading.py::load_bound_replay_inputs` 先重新执行请求的原始来源绑定检查，
然后核对两类工件角色与 request hash，最后重读原始文件并重新规范化。
原始规则变回 UNVERIFIED、计划改变、角色互换或跨请求拼接都在读取市场数据前拒绝。
返回现有 P7 可用的 DataFrame 与 FundingDataset，不调用 replay/scheduler，也不更改
全局研究授权。单请求加载通过不能证明整个 DEV 的信号集合或参数执行已经完成。

合成端到端测试确认正常 TP2 与最后可能入场后的 TIME_EXIT 均可闭合，并接入原 P7
Funding 计算；这是测试代码显式调用 P7，不是加载器自动执行，更不是策略验收结果。
