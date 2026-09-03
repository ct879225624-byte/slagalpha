# P9 扫描器行情输入边界

日期：2026-09-03。输入验证与后续扫描器接线，逐项开发；不批准历史规则、不运行真实策略。

## 1. 原始 ZIP → 规范化清单 → Parquet（已实现）

`research/candle_inputs.py::load_verified_candle_partition` 只接收 15m/1h/4h/1d 的
明确归档身份、下载凭证和规范化清单。检查官方固定 URL/文件名、同一 symbol/interval/月、
源哈希、行数和时间，并仅访问项目内的标准路径；拒绝逃逸和符号链接。

每次加载重新哈希原始 ZIP 和 Parquet；从 ZIP 重算规范化结果，核对清单全部统计，
再检查 Parquet 物理 schema、元数据、所有 Candle 权威值、收盘/来源标记及入库时间。
不会只信 Parquet 的哈希元数据，也不会因为伪造者同时更新文件哈希就接受修改的值。
复用既有 Decimal 规范化器，精确比较数值，不把 Arrow 的 18 位小数存储与原文小数位数
差异误判为不同值；不经过 float64 转换。

加载器返回从已验证 ZIP 重算、并与存储文件逐字段核对过的 Candle。声明凭证并非交易所
数字签名，仍需正式运行层绑定已冻结采集证据；单文件通过不证明完整 Universe/DEV 覆盖、
历史种子合格或策略可执行。

26 项合成定向测试通过。本地真实 BTCUSDT 2024-01 的四个周期复验通过：
15m 2,976 行、1h 744 行、4h 186 行、1d 31 行，合计 3,937 行。
两次验收报告一致：`f35e1e293a05278f0d605518ce91923f7e9451a96a266eb4625f2c824a62898e`。
原文件未改写，报告保存在本地忽略的数据目录；没有下载行情、运行指标或策略。

```powershell
.venv\Scripts\python.exe scripts/p9_candle_partition_pilot.py
.venv\Scripts\python.exe -m pytest -q tests/test_candle_inputs.py
```

## 2. 连续历史前缀（已实现）

`research/candle_history.py` 将明确的起点、周期、完整月份来源绑定到一个 DEV Universe
扫描位，按该周期最后完整收盘边界返回连续前缀。4H 的 08:15 只能看到 08:00 前收盘的
Candle；1D 的日内扫描只能看到当日午夜前收盘的 Candle。午夜归属遵循现有扫描清单。
缺月、重复/乱序月份、首尾缺 K 线及跨月接缝缺口全部拒绝；不填充、不排序修复。

源月文件先整体校验，再截取可见前缀；源文件在确认时刻之后的异常也可能保守阻断，
不会把未来行暴露给指标或策略。较早的 warm-up 历史可以跨 DEV 起点，但确认位必须在 DEV。
每个前缀保存完整来源和内容哈希；复用时重验原始字节。种子起点只是显式声明，
`history_seed_verified=false`、`research_authorized=false`，不能把它当成最早历史或已审核种子。

开发中修复直接重复规范化的入库时间不一致：旧 Parquet 被复用时从文件恢复原入库时间，
新文件在写入前规范到毫秒精度；不再创建带新入库时间的旧文件清单。既有文件未改写。
回归测试确认重复调用路径、文件字节和清单一致。历史旧清单按 Arrow 存储精度核对。

新增 25 项前缀测试并增强规范化幂等回归。全仓 582 passed、1 skipped；Ruff、
mypy 123 文件、pip check 通过；真实四周期 pilot 哈希保持不变。

## 3. P3 指标与结构证据（已实现）

`research/scan_features.py` 先绑定同一计划/候选/扫描清单，计划有 blockers 时在读取文件前
拒绝计算；随后重验前缀原始文件，复用 P3 计算指标、确认 Pivot 和 Zone。
Pivot 使用候选自己的 2/2 或 3/3 参数，不能借用另一候选的结果。
指标摘要使用 float64 精确十六进制值，warm-up NaN 显式编码为 null，禁止 Infinity。

证据保存原历史输入、参数、版本、指标摘要和完整 Pivot/Zone。复用时重新计算全部输出，
能识别自行重哈希的指标或遗漏结构；仅解析模型不能证明执行。
`history_seed_verified=false`、`strategy_executed=false`、`research_authorized=false` 保留。
这里的 strategy 指 P4 及以后的策略，不把 P3 特征计算当成完成策略扫描。

22 项合成测试验证参数绑定、未来行情不改变既有指标/结构、完整前缀不被 185 根截断、
原始字节篡改和自洽伪造输出。全仓 604 passed、1 skipped，Ruff、mypy 125 文件、pip check 通过。
未对真实行情运行 P3，没有写出真实策略证据。

## 4. 实施顺序

1. 基于已重算的多周期特征接入逐时点 P4 上下文，区分历史不足与合法无信号。
2. 历史规则证据与实际种子边界验证齐备后，才进入真实 DEV 扫描与请求采集。

整体仍为 9/14（约 64%）、P9 1/4。新准备工作不计为真实研究完成。
