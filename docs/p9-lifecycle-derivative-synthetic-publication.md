# P9 lifecycle derivative synthetic staged publication

第 30 项在第 29 项的内存 ZIP executor 之后补齐 synthetic output 发布验收。它只允许调用方
指定的测试工作区，并显式拒绝本仓库 `data/normalized`；不提供真实批处理脚本。

## 发布语义

- 重验冻结 contract、action 与 acceptance 的 lineage、行数和 normalized content hash。
- 在独立 staging 目录写 Parquet，再复验 metadata、source hash、行数和文件 SHA-256。
- 最终文件使用 action SHA-256 的 16 位前缀，避免 Windows 临时文件路径超限。
- 完整 staged bytes 通过不可变发布原语一次暴露；同内容重复调用复用。
- 既有路径内容不同则 Fail Closed，不覆盖，并清理 staging。
- 空分区只发布 `.exclusion.json` acceptance，不创建 Parquet。

合成测试覆盖正常发布、幂等恢复、冲突拒绝、临时目录清理、空分区和仓库 normalized 路径
拒绝。frozen normalization 没有被创建或修改。

## 门禁

本项没有读取真实 ZIP、生成真实 derivative、下载行情、连接账户、交易、部署或 push。
normalization execution、ATR reset、history seed、历史规则放行、research、strategy 和
locked test 授权全部保持 `false`。
