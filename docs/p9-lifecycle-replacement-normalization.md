# P9 lifecycle replacement normalization lineage

第 32 项只生成独立的生命周期边界衍生分区，不能直接冒充完整 normalization 数据集。本项用
一个内容寻址 overlay 清单描述读取优先级和完整血缘，不复制全量数据，也不修改冻结结果。

## 可信输入与输出

- source normalization：`c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2`
- remediation plan：`fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79`
- real execution receipt：`3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e`
- replacement result：`7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b`
- replacement dataset identity：`c418cec78a0392119ce5f43bb68dd62f0581119dba09a16bd16bfb02a6f82831`

读取策略固定为 `DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION`：命中清单中的生命周期边界分区
时使用 derivative；其余分区继续引用冻结 normalization。清单本身明确记录尚未物化第二份
全量 dataset view。

## 分区与行数守恒

| 项目 | 数值 |
|---|---:|
| requested partitions | 73,340 |
| frozen available partitions | 73,313 |
| resolved original failures | 24 |
| shadowed contaminated daily partitions | 8 |
| materialized overlay partitions | 31 |
| excluded overlay partitions | 1 |
| replacement available partitions | 73,336 |
| unavailable partitions | 4 |
| frozen normalized rows | 69,189,525 |
| shadowed daily source rows | 247 |
| derivative retained rows | 10,153 |
| replacement rows | 69,199,431 |

唯一 exclusion 是 `CTKUSDT/1d/2025-04`。其余不可用分区为
`AERGOUSDT/{15m,1h,4h}/2025-04`；AERGO 不具备 verified lifecycle identity，不能从价格
断层反推 cutoff。

## 边界与复跑

构建器重验三份可信输入的固定 hash、相互引用、action coverage、失败集合、overlay 状态和
行数。writer 只发布以 result hash 命名的不可变 JSON；同内容复跑复用，既有内容冲突时拒绝。

运行 `scripts/p9_lifecycle_replacement_normalization.py` 会生成或复验清单，并按设计返回 1：
`status=BLOCKED`。这不是执行失败，而是历史规则、1m、Funding、ATR/history seed 与 AERGO
仍未满足。`research_authorized=false`、`strategy_executed=false`、
`locked_test_consumed=false`。
