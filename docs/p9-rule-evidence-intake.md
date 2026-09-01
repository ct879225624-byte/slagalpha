# P9 历史规则取证清单与本地验收

日期：2026-08-31。属于 P9.2b 输入准备，不是回测、参数冻结或新数据来源接入。

## 1. DEV 取证清单

`scripts/p9_rule_gap_audit.py` 先复用 P9.1 审计，确认完整日度元数据序列与冻结 split
匹配，再仅对 DEV 内受阻成员汇总连续日期。没有读取行情收益，也没有执行锁定测试。

- 548 个 DEV 日期、16,440 个成员日，0 个规则门控可用成员日。
- 248 个合约、2,564 段连续缺口；成员离池或阻断原因变化时分段，不跨空档合并。
- 取证优先级：受阻成员日数降序，然后 exact symbol 升序。不按策略表现排序。
- BTCUSDT、DOGEUSDT、ETHUSDT、SOLUSDT、XRPUSDT 各有 548 个受阻成员日。
- 报告绑定 split、input audit 和完整规则注册表内容哈希，重跑结果一致。
- report hash：`78b11fb3e35e22ae90108f8be2e512342483022aae938dd02a983b2c1664d808`。

这里的日期段是**取证需求**，不是规则生效区间。`EFFECTIVE_FROM_GATE_ONLY` 仅统计
每天 Universe `effective_from`（00:15 UTC）的门控；不能据此声称整天、日内规则变化
或持仓期已覆盖。后续真实 replay 仍需按实际事件时间检查规则。

## 2. 接收格式与命令

首版只验收单个候选区间和原始 exchangeInfo JSON，不自动解析公告网页、接入提供方、
推导连续性或合并注册表。沿用项目已有 exchangeInfo 解析器，没有新增依赖。

提交 JSON 的顶层字段：

| 字段 | 含义 |
|---|---|
| `candidate` | 现有完整 `ContractRegistryEntry`，必须为 `UNVERIFIED` |
| `snapshot_file` | 相对证据目录的原始快照文件；使用 `/` 分隔 |
| `snapshot_observed_at` | 该历史响应最初被实际观测/采集的 UTC 时间，不能拿今天下载时间冒充 |
| `collected_at` | 本次收集到证据材料的 UTC 时间，不早于原始观测时间 |
| `continuity_file` | 覆盖所声明区间的连续性说明及证据文件；不能只重复原始单点快照 |
| `continuity_sha256` | 连续性文件原始字节的 SHA-256 |
| `continuity_note` | 该材料如何支持整个区间、仍有哪些疑点；不得为空白 |

`candidate.source_snapshot_hash` 必须等于原始快照的 SHA-256。候选区间需有明确右开
终点，且原始观测时间处于区间内。缺少连续性文件或终点会得到 BLOCKED 报告；
`VERIFIED` 候选、无时区时间、重复 JSON key 等无效提交直接拒绝。

在项目根目录运行：

```powershell
# 查看完整输入 JSON Schema，不生成占位历史证据
.venv\Scripts\python.exe scripts\p9_rule_evidence_audit.py --schema

# 以下输入路径是示例；先由真实证据提供方/审阅者准备材料
# --evidence-root 不传时使用 submission.json 所在目录
.venv\Scripts\python.exe scripts\p9_rule_evidence_audit.py "D:\Codex\work\rule-evidence\submission.json"

# 重建 DEV 缺口清单
.venv\Scripts\python.exe scripts\p9_rule_gap_audit.py

# 使用已有真实当前快照验证“不得外推到 DEV”
.venv\Scripts\python.exe scripts\p9_rule_evidence_acceptance.py
```

CLI 不访问网络、不读取凭证。原始文件保持原位，仅在
`data/manifests/rule_evidence_intake/<hash>.json` 保存不可变验收报告。退出码：
0 为 `READY_FOR_REVIEW`，1 为 `BLOCKED`，2 为提交格式/读写失败。
所有文件路径必须位于显式证据目录内；拒绝绝对路径、`..`、Windows ADS 和逃逸软链接。

## 3. 验收与权限边界

- 校验原始字节哈希、symbol、base/quote/margin、合约类型、状态、onboardDate、
  tick、step、min/max quantity、min notional；不能丢弃原快照已有的可选 filter 值。
- 缺少 PRICE_FILTER/LOT_SIZE、非有限数值、重复 JSON key、文件损坏或缺失均阻断。
- 单点快照不能独自证明整个区间；有连续性文件也只说明材料已提交，并不证明其内容正确。
- `READY_FOR_REVIEW` **不是 VERIFIED，也不授权研究**。来源真实性、历史采集时间、
  区间连续性、生命周期以及与旧注册表是否冲突，仍必须逐项人工复核。
- 所有报告固定 `verification_status=UNVERIFIED`、`registry_modified=false`、
  `research_authorized=false`。没有自动批准、覆盖或导入注册表的入口。
- 人工复核通过后如何生成非重叠新注册表属于后续有真实材料时的独立任务，不能手改
  当前草案的 verification 字段后直接开始回测。

## 4. 真实反例验收与测试边界

反例脚本读取已保存的真实 BTC 当前快照与冻结 DEV 日期，**故意在内存构造无效历史
声明**，验证返回 `SNAPSHOT_OUTSIDE_CLAIMED_INTERVAL` 和
`CONTINUITY_EVIDENCE_REQUIRED`。这个声明不是历史数据，未写入任何注册表。

- 两次运行报告哈希一致：`eaac96511c1f737f319161bd89e49885d84e324b245ddfc9fbf1e9bb72384d22`。
- 脚本复核原注册表字节未改动。
- 新增 5 项缺口清单测试和 34 项证据验收测试；正向材料全部为显式 mock，只验证代码。
- 全仓 264 项测试通过；Ruff 通过，Mypy 81 个文件通过。
- 原 P9.1 审计和 P9.2a 计划重跑哈希未变，真实研究仍为 BLOCKED。

下一解锁条件仍是可核验的历史规则材料。当前工具不解决外部证据缺失，也不替代
正式 RunManifest 所需的 Git 提交、干净工作区和后续 1m/Funding 输入验证。
