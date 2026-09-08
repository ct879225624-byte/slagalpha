# P9 lifecycle derivative executor interface contract

第 28 项冻结未来生产 executor 的安全接口，只做实现前审查，不实现 executor、读取原始
行情或物化 derivative Parquet。

## 可信输入

契约只接受两个已冻结、内容寻址的上游工件：

- remediation plan：`fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79`
- dry-run design：`124c2c1ed41ff3008c61b39fb8f02b70e30e8a650c3d49961368255483fb1898`

构建时重验两个模型、精确 hash、plan 引用关系、frozen normalization 和 lifecycle audit
lineage。契约保留 32 个 action 以及 `30,877 = 20,724 + 10,153` 的预期行数守恒。

## 冻结接口

- 输入仅允许已验证的本地主 symbol archive；`*SETTLED` 永远只作证据，不得合并。
- frozen namespace 固定为 `data/normalized/klines`，禁止修改。
- derivative namespace 固定为 `data/normalized/lifecycle_scoped/v0.1.0`。
- 分区文件名包含 action hash；空分区只生成 exclusion receipt。
- 发布必须经过 stage、完整校验和 atomic rename。
- 恢复只复用内容完全一致的输出；任何冲突均 Fail Closed，禁止覆盖。
- 每个 action 必须复核 plan membership、主/SETTLED archive 与 rows hash、exact cutoff、
  行数守恒、normalized schema/content hash 和 frozen namespace 未变化。

内容寻址契约为
`data/manifests/lifecycle_derivative_executor_contract/e1033ff7559b3116e6825471911718dc16828ec6d99acc95d7e3d66d5a914fa3.json`。
重复生成得到相同 hash。

## 门禁结论

状态固定为 `BLOCKED`。executor implementation、真实 normalization execution、ATR reset、
history seed、历史规则放行、research、strategy 和 locked test 授权均为 `false`；没有输出
被物化。第 28 项没有下载行情、连接账户、交易、部署或 push，也不构成真实执行授权。

冻结 CPython 3.12.13 专项验证为 `5 passed`，全部 lifecycle 回归 `20 passed`；Ruff
全仓通过；mypy 全仓 `159 source files` 通过。第 27 项刚完成的全量基线为
`851 passed, 1 skipped`。
