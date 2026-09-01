# P9 DEV 运行输入语义门禁

日期：2026-09-02。该门禁建立在文件内容哈希报告之上，仍不授权策略执行。

## 已实现检查

`src/slagalpha/research/execution_semantics.py` 首先重新计算全部已选文件哈希；内容报告与
当前文件不再一致时立即拒绝。随后解析并交叉核对：

- 策略规则 SHA-256 与敏感度计划一致；本地环境精确匹配环境锁。
- 保存的敏感度计划、参数版本与调用方选择完全一致，参数仍属于该计划候选。
- research split、输入审计、计划的 split/audit/规则注册表引用互相一致。
- 合约规则注册表版本与计划一致。
- Universe batch 必须 complete，覆盖全局研究起止，run/daily snapshot 哈希与 split 一致。
- 多周期 normalization batch 必须 complete，dataset hash 与 Universe 输入一致。
- archive batch 必须 complete，capacity plan hash 与 normalization 输入一致。
- 若提供排除表，其版本必须与 Universe batch 一致。

目前尚无正式 1m、Funding 和依赖工件 manifest schema；即使以后只提供任意文件并通过
字节哈希，这三类也会因缺少语义验证器继续失败关闭。Aggregate Trades 可选，但一旦声明
也必须先有语义验证器才能用于运行。

## 当前真实结果

运行：

```powershell
.venv\Scripts\python.exe scripts\p9_dev_execution_semantics.py
```

报告哈希：

`00104d25f2d297d2160a0ee471c1ac29fab4713744bdd8582e0b80a82e5a23ad`

10 类通过解析与交叉引用：Archive、合约规则注册表、环境锁、显式空排除表、参数版本、
输入审计、split、敏感度计划、策略规则和 Universe。

4 类仍 deferred：

- 多周期 Candle：已提供的 normalization batch 为 `complete=false`，27 个文件规范化失败。
- 1m Candle：没有正式输入 manifest。
- Funding：没有正式输入 manifest。
- 依赖工件：只有环境版本锁，没有 wheel/source artifact 哈希清单。

报告还保留历史规则 0 个合格成员日的既有阻断。状态为 `BLOCKED`，固定
`research_authorized=false`、`strategy_executed=false`、`locked_test_consumed=false`。
这不是策略失败，也不改变 P9 1/4 的完成口径。
