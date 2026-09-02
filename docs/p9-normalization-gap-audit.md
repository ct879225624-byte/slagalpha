# P9 多周期规范化失败审计

日期：2026-09-02。只读检查本地原始 ZIP，不补 Candle、不重算策略、不运行任何研究集。

## 输入与结果

- normalization result：`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`
- daily snapshot sequence：`2ae73f816286e7932f88d2e7c8859a3c5ad8146df4b0ae59dc242faaae1412f8`
- 1,096 份既有 Universe 快照；27/27 个失败文件均有真实内部时间缺口。
- 失败分布：AERGO/2025-04、AIA/2026-01、CTK/2025-04、CVC/2025-05、
  CVX/2025-07、LIT/2025-12、MAVIA/2025-03、PUMP/2025-07、SLP/2025-07，
  每个标的月份均涉及 15m、1h、4h。
- 入选有效时段直接重叠：0；185 根有限回看重叠：0。
- 递归历史依赖未解决：18 个文件、411 个文件—入选日组合（不是 411 个独立日期）。
- report hash：`9fdfe51b0a209451b2bae612f427ba33702b8225b71b03211372f0c986c1a7fc`
- 状态：`BLOCKED`，`semantic_gate_relaxation_authorized=false`。

每条证据保存完整 ZIP SHA-256、行数、首末时间、全部非连续时间跨度及依赖日期。
无法读取的文件产生阻断报告；乱序、重复、跨月或非网格时间拒绝作为正常缺口解释。
交集使用 Universe 的 `[effective_from, effective_to)`，包含跨日 00:15 边界。

## 不能用 185 根作为通用历史上限

冻结规则与 `strategy/indicators.py` 明确采用 Wilder ATR14 递归平滑。185 根覆盖
SMA180 的 5 根斜率参考，却不能证明 ATR 或继承状态与更早历史无关。
因此所有失败月份之后的入选时段均保守记录为递归依赖；没有明示并验证种子/重置边界前，
不能因为缺口距入选日较远而放行。初版仅检查有限窗口的临时报告已被纠正，未作为验收证据。

此报告不说明交易所缺口的业务原因，也不证明成功 Parquet 的逐文件依赖完整性。
全时期数据审计不等于消费锁定测试集的策略结果；DEV 专属依赖范围仍需正式执行器核验。

## 复现

```powershell
.venv\Scripts\python.exe scripts\p9_normalization_gap_audit.py
.venv\Scripts\python.exe -m pytest -q tests\test_normalization_gaps.py
```

脚本返回码 1 表示证据中存在预期阻断，不表示脚本运行失败。同输入重复报告哈希一致。
13 项单测覆盖真实缺口形态的合成 ZIP、有效期边界、远期递归依赖、缺文件、非法时间、
不可变写入、哈希篡改及路径限制；测试 ZIP 明确为 mock，不作为研究数据。

整体进度保持 P0–P8 完成、P9 1/4、9/14（约 64%）。
