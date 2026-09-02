# P9 DEV 扫描清单与请求集合

日期：2026-09-02。按顺序建设、独立验证；不执行策略，不升级历史规则状态。

## 1. 应扫描清单（已实现）

`research/scan_plan.py` 从冻结 split、敏感度计划、单一参数版本及完整日度 Universe
生成扫描义务，不读取多周期行情、不生成信号。全序列日期和 version 序列必须精确匹配
split；另绑定每份快照完整模型的哈希，避免仅凭 version 字段看不出成员变化。
这仍不重算 Universe 排名或其原始来源，不能代替上游工件验证。

每个 Universe 日从当日 00:15 UTC 生效开始，每 15 分钟、每个成员一个记录位；
次日 00:00 的确认仍属于前一份池，次日 00:15 起切换新池。
最后一天在 DEV 结束日 00:00 前停止，不纳入 VALIDATION 确认。
首个 DEV 午夜没有前日池证据，因此清单明确从首日 00:15 开始，不补造首个午夜记录。
这是按已冻结 Universe 有效时间生成的覆盖范围，不声称覆盖未提供前日池的时段。

正常日 96 个时间位、最后一天 95 个；每个时间位乘实际成员数，允许明确记录空池。
空池或清单本身都不表示完成扫描或研究。原计划 blockers 完整保留，
`scan_executed=false`、`research_authorized=false` 固定不可提升。

真实元数据复跑两次一致：548 天、1,578,210 个应扫描记录位；清单
`7a38b139b799572693a0e095f4f0ec3fa560626625da8fe2f601c73afe943706`。
仍带 `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`，脚本预期退出 1。

```powershell
.venv\Scripts\python.exe scripts/p9_dev_scan_plan.py
.venv\Scripts\python.exe -m pytest -q tests/test_scan_plan.py
```

本项新增 18 项合成测试；全仓 473 passed、1 skipped，Ruff、mypy 114 文件、pip check 通过。

## 2. 后续顺序

1. 每日扫描证据逐项匹配应扫描清单；accepted plan 与请求一一对应，拒绝漏项和重复。
2. 完整请求集合与各请求 1m/Funding 工件一一对应，并重验上游与原始响应。
3. 实际扫描执行及来源计算证明另行接入；哈希自洽、声明 NO_SIGNAL、单请求测试通过
   均不等于真实策略执行或全 DEV 数据就绪。总门禁保持失败关闭。

实际执行前仍需历史规则证据及多周期依赖/递归种子边界。整个工作属于 P9 输入准备，
整体保持 9/14（约 64%）、P9 1/4。
