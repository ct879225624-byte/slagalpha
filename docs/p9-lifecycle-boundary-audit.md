# P9 重上线生命周期边界审计

日期：2026-09-08。只读核验本地原始月度 ZIP，不修改既有 normalization frozen result，
不下载行情，不生成 ATR reset 或 history seed，不执行策略研究。

## 输入与内容寻址结果

- normalization result：`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`
- identity registry：`739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36`
- audit report：`a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818`
- 路径：`data/manifests/lifecycle_boundary_audit/a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818.json`
- 状态：`BLOCKED`；9 个标的中 8 个确认重上线边界，1 个未解决。

报告为 9 个主 symbol 及对应 `*SETTLED` 的 15m、1h、4h、1d 共 72 份原始 ZIP
保存文件 SHA-256、规范化行内容 SHA-256、行数、首末时间和全部时间断层。

## 15m、1h、4h 结论

AIA、CTK、CVC、CVX、LIT、MAVIA、PUMP、SLP 的三个周期均满足：

1. 主 symbol 月文件只有一个断层，且 normalization failure 为 `CandleGapError`；
2. 断层右端等于 identity registry `effective_from` 所属的 UTC 周期桶；
3. 对应 `*SETTLED` 文件连续且全部行落在主文件断层的
   `(旧主段末端, 重上线桶]` 内；
4. 主/SETTLED 仅可在重上线所属粗周期桶重叠，发生重叠时行内容不同，未把两个
   生命周期误判为重复数据。

因此上述 8 个标的记为 `CONFIRMED_RELIST_BOUNDARY`。PUMP 的 SETTLED 残段只有一个
07:00 桶；审计只要求真实残段位于主断层内，不要求 SETTLED 人为填满交易暂停期。

AERGO 的 15m、1h、4h 具有相似断层与 SETTLED 形态，但 identity registry 没有该次
生命周期的已验证 `effective_from`，故固定为 `UNRESOLVED`，不能由行情形态反推身份边界。

## 独立 1d 检查

8 个已确认标的的主 `1d` 月文件均同时包含：

- 重上线日之前的主 symbol 日线；
- 重上线日及之后的主 symbol 日线；
- 与 SETTLED 文件相同的重上线日 open time，但该日行内容不同。

这说明主 `1d` 月文件跨越旧、新生命周期，状态均为 `MIXED_OLD_LIFECYCLE`。它不是
15m/1h/4h 的显式时间断层，因此必须单独阻断，不能因日线网格连续而视为生命周期连续。
AERGO 的 1d 仍因缺少已验证 identity 边界而为 `UNRESOLVED`。

## 授权边界

本报告只确认和记录生命周期证据，不是输入修复或研究放行：

- `normalization_result_mutation_authorized=false`
- `atr_reset_authorized=false`
- `history_seed_authorized=false`
- `historical_rule_gate_relaxation_authorized=false`
- `research_authorized=false`
- `strategy_executed=false`
- `locked_test_consumed=false`

既有 27 个 normalization failure 和历史规则门禁均不变。即使 8 个标的的断层已解释为
重上线边界，也不能据此延续 Wilder ATR、种入历史状态或复用旧生命周期日线。

## 复现

```powershell
.venv\Scripts\python.exe scripts\p9_lifecycle_boundary_audit.py
.venv\Scripts\python.exe -m pytest -q tests\test_lifecycle_boundaries.py
```

脚本返回码 1 表示报告内存在预期阻断（8 个 1d 混存和 AERGO 未解决），不是审计执行失败。
同一输入重复运行得到相同报告哈希。合成测试覆盖确认边界、1d 混存、缺 identity 和断层
右端不匹配反例；测试 ZIP 只属于 mock，不是研究行情。
