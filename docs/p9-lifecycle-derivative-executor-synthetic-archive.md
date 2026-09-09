# P9 lifecycle derivative synthetic archive executor core

第 29 项实现冻结 executor contract 约束下的 ZIP 读取核心，但执行范围仍严格限定为调用方
提供的合成内存字节。入口没有文件路径或输出目录参数，因此不会读取仓库真实 ZIP，也不会
物化 derivative Parquet。

## 验证顺序

1. 重验 executor contract、remediation plan 和 action 模型。
2. 精确匹配调用方信任的 contract hash、plan hash 及 plan/action membership。
3. 匹配 normalization result 与 lifecycle boundary audit lineage。
4. 分别验证主和 SETTLED ZIP SHA-256、唯一 CSV member、rows SHA-256。
5. 核对 SETTLED evidence 行数，但只把主 symbol 行传给既有 synthetic executor。
6. 复用既有 exact cutoff、行数守恒、schema 和 normalized content hash 校验。

合成数据中 SETTLED 使用与主 symbol 不同的价格；验收结果只含主 symbol 价格，证明
SETTLED 没有进入 normalization。篡改任一 ZIP 或替换可信 contract 都会 Fail Closed。

## 门禁

本项不提供写盘或真实文件入口。acceptance 保持 `output_materialized=false`，normalization
execution、ATR reset、history seed、历史规则放行、research、strategy 和 locked test
授权全部为 `false`。没有下载行情、连接账户、交易、部署或 push。

```text
.venv\Scripts\python.exe -m pytest -q tests\test_lifecycle_derivative.py
.venv\Scripts\python.exe -m ruff check src\slagalpha\data\klines.py src\slagalpha\research\lifecycle_derivative.py tests\test_lifecycle_derivative.py
.venv\Scripts\python.exe -m mypy src\slagalpha\data\klines.py src\slagalpha\research\lifecycle_derivative.py tests\test_lifecycle_derivative.py
```
