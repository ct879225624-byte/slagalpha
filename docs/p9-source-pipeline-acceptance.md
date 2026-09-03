# P9 来源链路端到端验收

日期：2026-09-03。仅合成数据，不运行真实策略研究、P7 或交易。

## 执行方式

```powershell
.venv\Scripts\python.exe -m pytest -q -s acceptance/test_source_pipeline.py
.venv\Scripts\python.exe -m mypy src tests scripts acceptance
```

位于独立 `acceptance/` 目录，必须显式运行；原默认 `tests/` 回归范围不变。
该验收会反复读取源文件、重算全部扫描位，耗时高于单元测试。不要与其他使用同一
`.pytest-tmp` 目录的 pytest 进程并发运行。

## 范围与边界

- 冻结四天合成 split：两天 DEV、一天 VALIDATION、一天 LOCKED_TEST。后两者只含
  Universe 元数据，不执行策略；第一个 DEV 日为空池，第二个 DEV 日为一个合约、95 个位置。
- 使用合成四周期 ZIP 和实际规范化 Parquet，显式历史起点；P3、P4、P5、P6、扫描位、
  整日重验、不可变保存、来源恢复及完整请求集合全部调用真实实现，不 monkeypatch 验证器。
- 四周期前缀生成后，统一调用正式 `compute_source_bound_scan_slot` 入口计算 P3–P6，
  不再在验收代码内重复接线。新入口重新核对完整冻结上下文和精确扫描时刻规则。
- 合成历史规则虽然在夹具中标为 VERIFIED，但不改变真实注册表；所有研究/种子审核限制保留。
- 正向场景预期为 94 个 NO_SIGNAL、1 个 accepted plan，有限窗口 526 根 1m，以及一条
  合成 Funding 记录。市场 JSON 同样经过实际字节哈希和规范化校验，不调用 API。
- 正向报告后修改临时合成 ZIP，验证旧报告不能掩盖原始来源变化。
- 文件全部写在 pytest 临时目录，不在真实项目数据目录创建扫描、请求或研究报告。

## 当前验收

正式单扫描位入口接线后，两项显式验收全部通过，耗时 475.55 秒
（本机实测，不是生产性能保证；此前逐层测试接线验收为 351.00 秒）：

- 完整 DEV 范围 2 天，第 1 天 0 位，第 2 天 95 位；94 个 NO_SIGNAL、1 个 accepted plan。
- 完整来源落盘、恢复重算和请求集合通过；526 根合成 1m 与 1 条 Funding 字节验收通过。
- 正向报告后实际破坏临时 ZIP，旧报告复用被来源哈希校验拒绝。
- 本次逐位来源 JSON 共 3,111,141 bytes；该小样本尺寸不能外推为全 DEV 容量保证。
- 默认回归最近一次为 755 passed、1 skipped；Ruff、mypy（含 acceptance）145 文件、
  pip check 通过。默认回归与显式验收分开运行，没有修改默认测试选择范围。

这不是全真实 DEV 的来源完整性、历史种子审核或生产容量验收。
