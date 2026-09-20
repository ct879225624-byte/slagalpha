# SlagAlpha P9 v0.1 Research Diagnosis / DEV Sensitivity Closeout

**状态：CLOSED（DEV sensitivity 已结束）**  
本报告只读取既有 immutable artifacts；没有运行新的 Scan、Replay、Sensitivity、Validation 或 Locked Test，也没有修改 frozen strategy semantics。

## 结论摘要

- Baseline 的 ZERO edge 为正，但在 BASELINE/STRESS 成本下分别为 **-1.306150 / -2.845698 R**；核心问题是 gross edge 不足以覆盖 fees + slippage，funding 只提供小幅抵消。
- Holding 16/48 和 TTL 2/6 主要改变执行/重放漏斗与持仓结果，未形成稳定的成本后正 edge；TTL 6 与 baseline replay 结果相同，不能被解释成新的 alpha 证据。
- Stop ATR 0.10/0.20 属于 P6 planning layer；0.20 明显减少 accepted/entered，且 BASELINE/STRESS 更差；0.10 新增交易但成本后仍为负。
- Pivot 3×3 是唯一呈现跨 ZERO/BASELINE/STRESS 都为正的 DEV candidate：**+4.625010 / +1.482079 / +0.305166 R**，但其改善主要来自 candidate set 变化（8 new、13 removed），不是 common trade replanning；因此这是 strong DEV evidence of a discovery-layer hypothesis，不是未来盈利结论。
- Compression 0.50/1.00 的 `REPLAY_SEMANTIC_EQUIVALENCE=true`；两者均引用 baseline replay，未在该分支重新执行 replay。
- SHORT 样本始终很小（baseline 4；pivot 2），总体明显弱于 LONG，但不能因为样本少而删除；LONG-only / trigger-family separation 仅作为 v0.2 hypothesis。

## 1. 完整 sensitivity matrix

金额单位均为 R；`DD` 是 realized exit-time order max drawdown。`confirmed/accepted/entered` 分别来自对应 immutable funnel/replay artifacts。Compression 行的经济结果是 baseline replay 引用，不是重新执行。

| Candidate | confirmed | accepted | entered | ZERO Net R | BASELINE Net R | STRESS Net R | BASELINE win rate / PF | STRESS PF | DD BASE/STRESS | cost erosion / note |
|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|
| Baseline | 2946 | 27 | 22 | +3.199858 | -1.306150 | -2.845698 | 45.45% / 0.9087 | 0.8151 | 5.3854 / 6.1361 | fees 3.2832 + slip 1.0944 - funding 0.1862 |
| Holding 16 | 2946 | 27 | 22 | +3.339093 | -0.647823 | -2.216915 | 45.45% / 0.9464 | 0.8318 | 5.0016 / 5.7522 | execution horizon changes only |
| Holding 48 | 2946 | 27 | 22 | +5.588819 | -0.737192 | -2.213538 | 50.00% / 0.9458 | 0.8491 | 4.6678 / 5.4183 | ZERO uplift does not survive costs |
| Stop ATR 0.10 | 2946 | 30 | 25 | +3.469572 | -1.910695 | -3.772310 | 44.00% / 0.8851 | 0.7885 | 5.4986 / 6.2891 | more accepted/entered, more cost burden |
| Stop ATR 0.20 | 2946 | 21 | 17 | +1.227860 | -2.522855 | -3.765442 | 47.06% / 0.7792 | 0.6941 | 5.6924 / 5.9286 | fewer trades, edge removed |
| TTL 2 | 2946 | 27 | 21 | +1.507990 | -2.933491 | -4.448842 | 42.86% / 0.7949 | 0.7109 | 5.3854 / 6.4051 | 1 baseline TP2 trade expires |
| TTL 6 | 2946 | 27 | 22 | +3.199858 | -1.306150 | -2.845698 | 45.45% / 0.9087 | 0.8151 | 5.3854 / 6.1361 | same economics as baseline |
| Pivot 3×3 | 2816 | 22 | 18 | +4.625010 | +1.482079 | +0.305166 | 44.44% / 1.1288 | 1.0248 | 4.0286 / 4.2332 | gross edge higher, fewer trades/costs |
| Compression 0.50 | 2948 | 27 | 22 | +3.199858* | -1.306150* | -2.845698* | 45.45% / 0.9087* | 0.8151* | 5.3854 / 6.1361* | `REPLAY_SEMANTIC_EQUIVALENCE=true`; baseline replay |
| Compression 1.00 | 2936 | 27 | 22 | +3.199858* | -1.306150* | -2.845698* | 45.45% / 0.9087* | 0.8151* | 5.3854 / 6.1361* | `REPLAY_SEMANTIC_EQUIVALENCE=true`; baseline replay |

\* Compression economic fields point to `artifacts/dev_replay_baseline_{ZERO,BASELINE,STRESS}_20260913/summary.json`; `replay_executed_here=false`.

为使 friction 口径对每一行可追溯，下面列出各 candidate 的 BASELINE/STRESS `(gross, fees, slippage, signed funding) R`：

| Candidate | BASELINE `(gross, fees, slip, funding)` | STRESS `(gross, fees, slip, funding)` |
|---|---|---|
| Holding 16 | (3.529877, 3.283105, 1.094368, +0.199773) | (3.602417, 3.283148, 2.735957, +0.199773) |
| Holding 48 | (3.385712, 3.282662, 1.094221, +0.253979) | (3.550814, 3.282725, 2.735605, +0.253979) |
| Stop ATR 0.10 | (3.154316, 3.939003, 1.313001, +0.186992) | (3.262254, 3.939030, 3.282526, +0.186992) |
| Stop ATR 0.20 | (0.914824, 2.678950, 0.892983, +0.134254) | (1.011755, 2.678973, 2.232478, +0.134254) |
| TTL 2 | (1.193397, 3.234827, 1.078276, +0.186214) | (1.295507, 3.234853, 2.695711, +0.186214) |
| TTL 6 | (2.885266, 3.283222, 1.094408, +0.186214) | (2.987376, 3.283248, 2.736040, +0.186214) |
| Pivot 3×3 | (5.012796, 2.624978, 0.874993, -0.030746) | (5.148347, 2.624964, 2.187471, -0.030746) |

Baseline 与两条 Compression candidate 的 friction 直接继承 baseline replay；ZERO 场景的 fees/slippage/funding 均为 0。

### Sensitivity interpretation by layer

- **Execution / replay layer（Holding, TTL）**：Holding 16/48 改变退出路径和成本暴露，但 BASELINE/STRESS 均仍为负；TTL 2 删除一个本来能进入并获利的 baseline trade，TTL 6 与 baseline 相同。结论是执行 timing 会改变结果，但没有显示可直接 hard-code 的执行最优值。
- **P6 planning layer（Stop ATR）**：Stop 0.10 放宽 accepted set，Stop 0.20 收窄 accepted set；两者都不能把成本后结果推到正值。Stop 参数改变的是 trade-plan funnel 与风险暴露，不是已证实 alpha。
- **Alpha discovery layer（Pivot, Compression）**：Pivot 3×3 改变确认延迟和候选集合，并在三种成本场景保留正值；Compression 两个候选仅提供 funnel diagnostics，经济结论必须引用 baseline replay，不能当作独立 replay evidence。

## 2. Pivot 3×3 深度诊断

Baseline 为 ZERO **+3.199858**、BASELINE **-1.306150**、STRESS **-2.845698**；Pivot 3×3 为 ZERO **+4.6250104624**、BASELINE **+1.4820788489**、STRESS **+0.3051661531**，entered 18，PF BASELINE **1.1288**、STRESS **1.0248**。

Phase 3 Stage B decomposition 为 **14 common / 8 new / 13 removed**。总改善不是只看总 Net R，而是：

| Scenario | total Δ Net R | removed-trade effect | newly-added-trade effect | common-trade replanning effect |
|---|---:|---:|---:|---:|
| BASELINE | +2.7882284550 | +0.5198986824 | +2.2949741922 | -0.0266444195 |
| STRESS | +3.1508639353 | +1.2665697949 | +1.9059788381 | -0.0216846976 |
| ZERO | +1.4251519971 | -1.8536958743 | +3.3189239405 | -0.0400760692 |

定义为：`removed effect = - removed_baseline.net_r`；`new effect = newly_accepted.net_r`；`common effect = candidate_common.net_r - baseline_common.net_r`。因此 BASELINE/STRESS 的主要收益是移除负贡献 trades 加上新增 trades；ZERO 中移除了一些正贡献 trades，新增 trades 的收益抵消了该损失；common replanning 在三个场景均轻微负贡献。

## 3. Trigger-family diagnosis

Stage A confirmed 从 baseline 的 **MA_RECLAIM 2711 / SWEEP_RECLAIM 235** 变为 Pivot 3×3 的 **2664 / 152**；Sweep confirmed 减少 83（-35.3%），但这不是单独的质量证明。

| Family / candidate | confirmed | accepted | entered | ZERO Net R / PF | BASELINE Net R / PF | STRESS Net R / PF |
|---|---:|---:|---:|---:|---:|---:|
| MA_RECLAIM baseline | 2711 | 22 | 18 | +4.577636 / 1.4901 | +1.214267 / 1.1733 | +0.103208 / 1.0722 |
| SWEEP_RECLAIM baseline | 235 | 5 | 4 | -1.377778 / 0.4907 | -2.520416 / 0.1364 | -2.948906 / 0.1130 |
| MA_RECLAIM pivot 3×3 | 2664 | 18 | 15 | +4.017484 /（见注） | +1.552972 /（见注） | +0.630466 /（见注） |
| SWEEP_RECLAIM pivot 3×3 | 152 | 4 | 3 | +0.607527 /（见注） | -0.070893 /（见注） | -0.325300 /（见注） |

注：Stage B trigger-family artifact 持久化了 family Net R/entered，但未持久化 family PF；baseline PF 是从 case-level net-R 正负和复算的。Pivot family 的 exact PF 不作为独立强证据。Sweep 的质量确实改善（BASELINE -2.52→-0.071、STRESS -2.95→-0.325、ZERO -1.38→+0.608），但样本只有 5→4 accepted、4→3 entered，仍是 sample-limited；不能把“confirmed 235→152”单独解释成质量因果。

## 4. Direction diagnosis

可靠 DEV replay 均保留 LONG/SHORT。Baseline 为 LONG 18 trades、SHORT 4 trades；Pivot 为 LONG 16、SHORT 2。Baseline LONG 在 BASELINE/STRESS/ZERO 为 **+1.846/+0.813/+4.411 R**，SHORT 为 **-3.152/-3.659/-1.211 R**；Pivot LONG 为 **+1.873/+0.896/+4.502 R**，SHORT 为 **-0.391/-0.591/+0.123 R**。跨 holding/stop/TTL candidates，LONG 大多保持正或接近正，SHORT 除 Holding 48 的 ZERO 外基本为负；但 SHORT 样本不足以删除。

**证据边界**：LONG-only、trigger-family separation 只列为 v0.2 hypotheses，不进入 frozen v0.1 semantics，也不作为本次“最佳参数”选择。

## 5. Cost diagnosis

Baseline：ZERO gross edge = **+3.199858 R**；BASELINE gross **+2.885266**，fees **3.283222**，slippage **1.094408**，funding **+0.186214**，按 signed funding 计入后为 **-1.306150 R**；STRESS slippage 上升至 **2.736041**，净值变为 **-2.845698 R**。也就是说，baseline 从 ZERO 正到 BASELINE 负，主要是 fees + slippage 合计超过 gross edge，funding 只小幅缓冲。

Pivot：ZERO gross **+4.625010**；BASELINE gross **+5.012796**，fees **2.624978**，slippage **0.874993**，funding **-0.030746**，净值 **+1.482079**；STRESS slippage **2.187471** 后仍为 **+0.305166**。因此 Pivot 的稳健性是三者共同作用：gross edge 更高、trade count 更少（22→18）使 fees/slippage 更低；funding 并非优势，反而略负。DEV 结果不能证明未来实盘成本相同。

## 6. Evidence classification

### Strong DEV evidence

- Pivot 3×3 在 ZERO/BASELINE/STRESS 三个既有成本场景均为正，且 decomposition 可复核；common-trade replanning 不是主要改善来源。
- Cost erosion 是 baseline 由正转负的直接解释；Pivot 的 gross/cost/trade-count 联合变化可解释其 BASELINE/STRESS 正值。
- Sweep family 的已观察交易质量相对改善，但只能在已有 sample 范围内陈述。

### Suggestive but sample-limited

- LONG 相对 SHORT 更稳定；SHORT 只有 2–4 entered trades。
- Sweep confirmed 235→152 与质量改善同步，但 accepted/entered 极少，存在选择与样本风险。
- Pivot 的 18 entered 与 PF 1.1288/1.0248 仍是小样本 DEV 结果。

### Not supported

- Holding 16/48、TTL 2/6 或 Stop ATR 0.10/0.20 可被选作“最佳 DEV 参数”。
- Compression 0.50/1.00 有独立经济 replay 证据；它们是 baseline replay 的 semantic-equivalence 引用。
- DEV 正值等同于未来盈利、实盘可交易性或可迁移性。

### Falsified / little evidence

- “只要 ZERO 为正，成本后就会为正”：baseline、holding、stop、TTL 多数反例成立。
- “Pivot 改善主要来自 common trades 的重新规划”：decomposition 显示 common effect 在三个场景都略负。
- “Sweep confirmed 减少本身证明 alpha 质量提升”：样本不足，不能单独成立。

## 7. Proposed v0.2 hypotheses（不实现）

1. **Discovery hypothesis：3×3 pivot confirmation 能提高候选 trade quality。** 来自 Pivot 三场景正值与 14/8/13 decomposition；问题是确认延迟/候选集合是否提高 gross edge。独立验证：冻结 execution/cost semantics，在新版本用预先声明的 out-of-sample split 比较 candidate funnel、gross edge、成本后 Net R，并保留 common/new/removed 分解。不能 hard-code 3×3 为“最优”，也不能把 DEV PF 当作 Locked Test 先验。
2. **Direction hypothesis：LONG 与 SHORT 可能需要分层诊断。** 来自 LONG 稳定、SHORT 弱但样本很少。独立验证：在不删除 SHORT 的前提下，预注册方向分层指标和最小样本门槛，在独立数据上比较稳定性。不能因 v0.1 直接 hard-code LONG-only。
3. **Trigger hypothesis：Sweep family 可能受更严格 pivot confirmation 影响。** 来自 Sweep confirmed 235→152、accepted 5→4、entered 4→3 及质量改善。独立验证：按 family 预注册样本、成本和置信区间，避免只看 confirmed count。不能直接删除 Sweep 或 hard-code family filter。

## 8. Governance / caveats

- DEV sensitivity 已结束；**Validation 尚未消费；Locked Test 尚未消费**。
- 不选择“最佳 DEV 参数”直接进入 Locked Test；本报告不授权任何 frozen strategy semantic 变更。
- 已记录 caveats：Windows path-length 导致 4 个既有 transport regression tests `FileNotFoundError`；Stage A candidate scanner original report 未独立持久化，Phase 3 Stage B 使用 immutable checkpoint reconstruction + request-set reconciliation；存在 historical tick-size 与 DEV approximate funding-mark warnings。
- 本报告是 DEV diagnosis，不是未来盈利、实盘收益或风险承诺。

## 9. Source anchors

- `artifacts/dev_replay_baseline_{BASELINE,STRESS,ZERO}_20260913/summary.json`
- `artifacts/sens_p1/phase1-summary.json`
- `artifacts/sens_p2_stage_b_final/phase2-stage-b-summary-v3.json`
- `artifacts/sens_p3_stage_a_summary/{b27864dc...,f5ae1a5f...}.json`
- `artifacts/sens_p3_stage_b/phase3-stage-b-summary.json`
- `artifacts/sens_p3_stage_b/d889a882a5f5/results_{BASELINE,STRESS,ZERO}/*.summary.json`
