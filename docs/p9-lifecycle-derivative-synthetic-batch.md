# P9 lifecycle derivative synthetic batch receipt

第 31 项把合成 archive executor 串成 canonical 多 action batch。输入 archive key 必须与
可信 remediation plan 完全一致；缺失、额外或篡改任一输入都会 Fail Closed。

每个 action 继续执行 contract/plan membership、主与 SETTLED hash、exact cutoff、行守恒、
staging 和不可变发布校验。逐项输出可以在失败后供同内容恢复，但 batch completion receipt
只在所有 action 成功后发布。

`lifecycle-derivative-synthetic-batch/0.1.0` receipt 内容寻址并绑定：

- executor contract hash 与 remediation plan hash；
- 每项 acceptance hash、相对输出路径和输出 SHA-256；
- accepted/excluded action 数；
- source、excluded、retained 和 derivative 总行数。

receipt 固定 `synthetic_output_materialized=true`、`real_output_materialized=false`，所有真实
normalization、ATR reset、history seed、历史规则放行、research、strategy 和 locked test
授权保持 `false`。本项不读取真实 ZIP，不写仓库 normalized，不下载、不交易、不部署。
