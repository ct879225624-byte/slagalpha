# P4 Setup Context Report

状态：已验收  
版本：`setup-context/0.1.0`

## 已完成

- P4.1：冻结 Setup 输入、边界、状态优先级、Episode 与短路契约。
- P4.2：实现 4H Direction、MA Order、Band Position、Compression 与 1D Context。
- P4.3：实现 1H Pullback Snapshot、严格放量加速过滤和稳定 Episode ID。
- P4.4：实现 4H → 1D → 1H 整合函数和严格短路；被跳过的周期结果保持 `None`。
- LONG/SHORT 使用同一对称规则；均线相等、阈值相等、平坦斜率、排列变化和价格带翻转均有边界测试。
- 1H 状态优先级、wick 边界、加速严格不等式、Episode 延续与非新 Episode 均有测试。
- 输入不足与非法数据分别使用 `SetupNotReadyError` 和 `SetupInputError` Fail Closed。

## 验证

```text
P4 tests: 24 passed
full pytest: 75 passed
ruff: All checks passed
mypy: Success: no issues found in 25 source files
```

测试覆盖：

- 4H LONG/SHORT、NEUTRAL、方向与压缩独立、4 类压缩原因及等号边界。
- 1D ALIGNED/MIXED/BLOCK_LONG/BLOCK_SHORT 镜像。
- 1H 六种状态、STANDARD 优先级、wick 不误判、加速严格不等式。
- Episode 新建、延续 ID 稳定、非新 Episode 与候选资格。
- NEUTRAL → COMPRESSED → DAILY BLOCK 的短路顺序，完整 LONG/SHORT 路径。
- 使用 P3 实际指标计算函数构造 240 根/周期的确定性多周期重放；重复结果一致，未来行不进入历史快照。

## 限制与验收边界

- 多周期重放使用明确标注的合成 Candle 测试夹具，不冒充真实行情。
- 当前本地真实样本只有 P2 的 BTCUSDT 15m 2024-01；为避免扩大 P4 下载范围，真实 1H/4H/1D 联合历史验证留到 P7/P8 数据集阶段。
- P4 不读取 15m，不生成 Trigger、Entry、Score、交易信号或订单。

P4 已由用户验收；后续历史信号语义变更必须提升 Setup context 版本。
