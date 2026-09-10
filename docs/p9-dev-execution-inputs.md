# P9 DEV 运行输入内容门禁

日期：2026-09-02。属于 P9.2b 工程准备，不是参数研究或执行授权。

## 契约

`src/slagalpha/research/execution_inputs.py` 对正式 DEV 运行所声明的文件逐字节计算 SHA-256，
并生成内容寻址的 readiness 报告。必需类别包括：

- 策略规则、环境锁、研究 split、输入审计、敏感度计划、参数版本；
- 历史合约注册表、排除表、Universe 汇总；
- 多周期 Candle、1m Candle、Funding、归档来源和依赖工件。

Aggregate Trades 是可选类别：缺失时 P7 固定使用 OHLC 不利顺序，因此不作为启动阻断，
但实际使用时仍必须纳入哈希清单。

`NORMALIZATION_GAP_AUDIT` 是可选诊断证据，不增加必需类别，也不替代多周期 Candle。
提供时同样验证原始文件 SHA-256，语义层再绑定 normalization、Universe 和 split。

门禁只接受规范的项目相对 POSIX 路径，拒绝绝对路径、`..`、非规范分隔符、符号链接、
目录和根目录逃逸。读取完整文件前后检查大小与修改时间；字节哈希不符、读取失败或读取中
变化均失败关闭。报告重新核对已验证类别、缺失类别、阻断集合和自身内容哈希。

参数工件必须先通过与敏感度计划的内容绑定。即使全部文件哈希通过，报告仍固定
`research_authorized=false`、`strategy_executed=false`、`locked_test_consumed=false`；
它不能自行调用 P7。

## 当前真实结果

运行：

```powershell
.venv\Scripts\python.exe scripts\p9_dev_execution_inputs.py
```

脚本使用代码中冻结的期望哈希，不从文件现算“期望值”。当前 12 类必需工件和 1 份附加
缺口审计逐字节匹配，
更新后的报告哈希为：

`cd60bc0c2076e132b6a57d2b7a09e9634b141c17bf87f954bde536cad0481b07`

状态为 `BLOCKED`，缺少两类：

- `CANDLE_ONE_MINUTE`
- `FUNDING`

历史规则 blocker 已由 DEV-only approximate fallback 解除；规则仍是 `UNVERIFIED`，并在
审计中记录 16,440 条 warning。当前尚未生成运行结果。

## 尚未解决的语义门禁

该报告证明的是“所选文件字节与预期哈希一致”，不证明文件业务内容足够覆盖研究区间。
全时期多周期 normalization 诊断仍记录 27 个规范化失败；它不再预先阻断全部 DEV，但
实际扫描读取受影响区间时仍失败关闭。历史规则仍是 `UNVERIFIED`，仅 DEV 可按明确 warning
使用。显式空排除表工件见 `docs/p9-exclusion-ledger-artifact.md`。下一层语义门禁已实现并
确认当前报告级 blocker 只剩 1m/Funding，见
`docs/p9-dev-execution-semantics.md`。依赖 wheel 工件已补齐并实际重验，见
`docs/p9-dependency-artifacts.md`。1m、Funding 的单请求 schema 和离线校验已实现，
但仍无真实 accepted 请求集合及其完整聚合输入，见 `docs/p9-replay-data-contract.md`；
未通过前不得把单份工件或内容哈希通过理解为全研究数据已就绪。
