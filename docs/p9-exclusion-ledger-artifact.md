# P9 显式空排除表工件

日期：2026-09-02。此项补齐既有 Universe 输入的可追溯性，不新增排除规则。

P8 的 1,096 份 Universe 快照及汇总均引用 `empty-ledger/0.1.0`，原接受脚本在内存中创建
零条目 `ExclusionLedger`，此前没有独立文件。现由
`src/slagalpha/data/universe.py::write_exclusion_ledger` 保存精确模型字节：

- ledger version：`empty-ledger/0.1.0`
- entry count：0
- artifact SHA-256：`6d975a9efba4902e35060f00845c0a7abb524790c9972705790eee5e7ead07f5`
- path：`data/manifests/exclusion_ledger/<artifact-sha256>.json`

写入以完整文件 SHA-256 命名；重复同内容幂等，既有路径内容冲突或符号链接时拒绝覆盖。
不同 ledger version 会产生不同工件。命令：

```powershell
.venv\Scripts\python.exe scripts\p9_exclusion_ledger.py
```

内容与语义门禁均已确认其版本与 Universe 一致。此工件没有回填或修改历史 Universe，
没有把任何 symbol 加入/移出标的池，也不解除历史合约规则、1m、Funding 或多周期数据阻断。
