# P9 lifecycle replacement semantic verification

本项把第 33 项 overlay 接入只读 DEV 输入语义审计。它只验证现有本地工件，不写行情、Parquet
或研究结果，也不放宽任何策略门禁。

## 验证链

语义审计要求调用方提供固定 replacement result hash
`7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b`，然后交叉验证：

1. replacement 与内容报告中的 source normalization result/dataset/requested count 一致；
2. completion receipt hash 为
   `3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e`；
3. receipt 的 normalization、remediation plan、materialized/excluded action 数和 retained rows
   与 replacement 一致；
4. 每个输出路径均为相对路径，解析后仍位于
   `data/normalized/lifecycle_scoped/v0.1.0`；
5. 32 个输出文件重新读取并复算 SHA-256。

任一 receipt、路径或输出字节异常都会产生
`RUN_INPUT_SEMANTIC_INVALID_LIFECYCLE_REPLACEMENT`，原 incomplete normalization blocker 同时
保留，不会降级为警告。

## 当前报告

内容寻址报告为
`c00ef3a90750581fc6ad2c490df8c2e5ae4ff143ba3e23d74081ee3c04f3f47e`，实测重验 32 个文件、
1,107,438 bytes。旧 gap audit 中 18 条已被 lifecycle derivative 覆盖的递归历史诊断不再重复
传播，改为 replacement 的当前 blocker：

- `ATR_RESET_OR_HISTORY_SEED_NOT_AUTHORIZED`
- `FUNDING_INPUTS_MISSING`
- `HISTORICAL_RULE_GATE_REMAINS_BLOCKED`
- `ONE_MINUTE_INPUTS_MISSING`
- `UNRESOLVED_LIFECYCLE_SYMBOL:AERGOUSDT`

replacement 仍有 4 个不可用分区，因此 `CANDLE_MULTI_TIMEFRAME` 保持 deferred；内容报告本身的
1m、Funding 和历史规则 blocker 也继续存在。报告状态为 `BLOCKED`，脚本返回码为 1；
`research_authorized=false`、`strategy_executed=false`、`locked_test_consumed=false`。

未传 replacement 的调用继续生成 `dev-execution-semantics/0.1.0`。v0.1 模型、旧内容 hash 和
writer 序列化保持向后兼容。
