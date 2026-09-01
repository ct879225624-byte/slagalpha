# P3 Indicator Core Report

状态：P3 已验收  
执行日期：2026-08-27

## 交付内容

- SMA30、SMA60、SMA90、SMA180。
- True Range 与严格 Wilder ATR14。
- Quote Volume previous、前 3 均值、前 20 中位数、前三根之前 20 根中位数。
- 确认型 Pivot 2/2，并支持允许的 3/3 单变量版本。
- Pivot 与 zone 的确定性 SHA-256 ID。
- 使用后一个 Pivot 确认时 ATR 的同类型结构区域合并。
- 等于 `0.2 × ATR` 时不合并。

## 自动验证

- pytest：51 passed。
- Ruff：通过。
- mypy strict：23 个源文件无问题。
- 手算 ATR seed 与后续 Wilder 递推一致。
- SMA 首个有效位置和 warm-up NaN 数量一致。
- Quote Volume baseline 已证明不包含候选 Candle。
- Pivot 在右侧第 2 根收盘前不可见。
- 添加未来 Candle 不改变此前指标与已确认 Pivot。
- 相同输入重复计算的指标、Pivot ID、Zone ID 一致。

## 真实 P2 数据集成验证

输入：`BTCUSDT / 15m / 2024-01`，共 2,976 根 Candle。

```text
SMA180 valid rows: 2,797
ATR14 valid rows: 2,963
Confirmed pivots: 831
  HIGH: 413
  LOW: 418
Merged zones: 715
Prefix candles: 2,000
Prefix confirmed pivots: 556
No-lookahead prefix comparison: PASS
Repeated-run equality: PASS
```

Pivot 与 zone 数量只是工程验证结果，不代表交易机会或策略表现。

## P3 未覆盖范围

- 未实现 4H 方向和 MA 压缩状态。
- 未实现 1D 过滤与 1H 回踩。
- 未实现 Pivot zone 的 96/60 根回看、破坏判断或 Sweep 选择。
- 未实现 Trigger、Entry、SL、TP、Score 或回测。
- 未进行任何参数收益优化。

这些内容必须在后续对应阶段单独实现和验收。
