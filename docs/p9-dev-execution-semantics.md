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
- 依赖工件必须与已验证环境锁一致，全部 wheel 名称/版本/平台元数据和真实文件哈希
  必须重新核验，不能仅凭汇总 manifest 放行。
- 若提供缺口审计，其 normalization hash、daily snapshot hash、快照数、失败文件数、
  失败 identity 和有限回看参数必须与当前输入一致；递归依赖阻断原样传播。
  审计只补充诊断，不解除 complete 要求；未提供它的旧冻结报告仍可读取。

1m、Funding 已有单请求工件 schema 和离线校验器，见 `docs/p9-replay-data-contract.md`。
但单请求不代表全部 DEV 请求集合：总门禁遇到此类工件时解析角色后仍报告
`REQUEST_SET_COVERAGE_REQUIRED`，不将其计为全研究范围数据已完成。
任意文件或角色串错仍失败关闭。Aggregate Trades 可选，但一旦声明也必须先有语义验证器。

## 当前真实结果

运行：

```powershell
.venv\Scripts\python.exe scripts\p9_dev_execution_semantics.py
```

报告哈希：

`095a2f2e7d33e2e5247a5656a919c9fec15b38daa4e86e320331b0dd4f9f20fd`

11 类通过解析与交叉引用：Archive、合约规则注册表、依赖 wheel 工件、环境锁、显式空排除表、参数版本、
输入审计、split、敏感度计划、策略规则和 Universe。

3 类仍 deferred：

- 多周期 Candle：已提供的 normalization batch 为 `complete=false`，27 个文件规范化失败。
- 1m Candle：没有真实 accepted 请求集合及其完整聚合输入 manifest。
- Funding：没有真实 accepted 请求集合及其完整聚合输入 manifest。

报告还保留历史规则 0 个合格成员日的既有阻断。状态为 `BLOCKED`，固定
`research_authorized=false`、`strategy_executed=false`、`locked_test_consumed=false`。
这不是策略失败，也不改变 P9 1/4 的完成口径。

当前附加诊断中有 18 个失败文件的递归历史依赖未解决（全时期范围），见
`docs/p9-normalization-gap-audit.md`。这是保守数据阻断，不能把有限窗口无交集解释为 ATR
历史已经完整，也不能当作 DEV 交易结果。

2026-09-05 在冻结 CPython 3.12.13 环境和干净提交 `b0a3e35` 后复跑，报告哈希及上述
11 类通过、3 类 deferred、18 个递归阻断均保持不变。说明近期扫描来源接线没有改变
真实输入语义或授权状态。
