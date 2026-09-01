# P5 15m Trigger Report

状态：已验收  
版本：`trigger/0.1.0`

## 已完成

- P5.1：冻结 Trigger A/B、量价窗口、Pivot/zone 时点和逻辑去重契约。
- P5.2：实现 Trigger A（MA Reclaim）及独立量价证据。
- P5.3：实现 Trigger B 的 96 根 zone 筛选、破坏判断和 Sweep Reclaim。
- P5.4：实现 A/B 真值表合并、Sweep 主触发优先级和确定性 logical signal ID。
- Trigger A 保存有效 episode、前 4 根回踩、SMA reclaim、上一根突破、volume 和结构 Pivot 证据。
- LONG/SHORT 共用镜像语义；严格突破、contraction、close-location 和零振幅边界均已覆盖。
- 15m 时间网格、warm-up、未来 Pivot 与重复运行 Fail Closed/确定性测试已覆盖。
- zone 必须在 Sweep 开盘前确认；最新确认优先、完整区域收回、严格 sweep 和破坏边界均已覆盖。
- B 历史不足不会抹掉有效 A；A+B 只输出一个 logical signal ID，重复运行 JSON 完全一致。

## 当前验证

```text
Trigger tests: 25 passed
full pytest: 100 passed
ruff: All checks passed
mypy: Success: no issues found in 27 source files
```

测试覆盖：

- GS-021–026：Trigger A 完整通过、缩量、close-location、零振幅、结构 Pivot。
- GS-029–031：Trigger B 完整区域 reclaim、zone 破坏、确认时点和 96 根窗口。
- GS-032：A+B 单一输出，`SWEEP_RECLAIM` 为 primary，`MA_RECLAIM` 为附加原因。
- LONG/SHORT 镜像、全部严格等号边界、未来 Pivot/zone 排除、重复模型和 JSON 一致。

## 限制与验收边界

- P5 测试使用明确标注的确定性 Candle/Pivot/zone 夹具，不冒充收益或真实市场有效性验证。
- 当前真实本地数据只有 15m 单周期，缺少与其同步的 1H/4H/1D Setup；完整真实多周期信号重放留到 P7/P8 数据集阶段。
- P5 只输出 `eligible_for_plan`，不生成 Entry、SL、TP、Score、ARMED 生命周期或 Paper Trade。

P5 已由用户验收；后续历史 Trigger 语义变更必须提升 Trigger 版本。
