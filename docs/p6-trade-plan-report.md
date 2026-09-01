# P6 Trade Plan and Score Report

状态：已验收  
版本：`trade-plan/0.1.0`

## 已完成

- P6.1：冻结 Entry、Stop、结构目标、TP 与 Score 契约。
- P6.2：实现统一 `Decimal(str(value))` 输入、任意正 tick 网格舍入、Entry、Stop buffer 和风险 Gate。
- P6.3：实现 15m/1H 结构目标筛选、前方障碍、TP1/TP2 与 ATR extension。
- P6.4：实现固定五项 Score、强制 Gate 一致性校验和 0–100 整数拆分。
- 保存 raw/rounded Entry 与 Stop、舍入方向、ATR/tick、risk、normalized risk、TTL 和右开 expires_at。
- LONG/SHORT 镜像、非网格价格、非十进制 tick、0.5/2.0 ATR 等号和非法输入均有测试。
- 结构确认时点、96/60 根窗口、0.99R/1R 边界、不同 zone 和 TP2 顺序均有测试。
- Score 阈值、满分 100、无拒绝门槛、LONG/SHORT 镜像与重复 JSON 均有测试。

## 当前验证

```text
Plan tests: 19 passed
full pytest: 119 passed
ruff: All checks passed
mypy: Success: no issues found in 29 source files
```

测试覆盖：

- GS-033/034：Entry/Stop 保守舍入及 0.5/2.0 ATR 风险边界。
- GS-035/036：0.99R 障碍拒绝、1R 等号、结构目标与 ATR extension。
- 非十进制 tick、未来 zone、TP2 顺序、同 zone 不复用及非法输入。
- 五项 Score 上限、阈值等号、满分 100、无评分门槛与强制 Gate Fail Closed。

## 限制与验收边界

- 当前测试是确定性计划夹具，不代表真实市场收益或成交质量。
- P6 生成理论 Entry/Stop/TP 和排序 Score；1m 成交、费用、滑点、Funding 和生命周期属于 P7。
- 未读取账户余额，未计算真实下单数量，未创建 Paper Trade。

P6 已由用户验收；后续历史价格计划或 Score 语义变更必须提升 Trade Plan 版本。
