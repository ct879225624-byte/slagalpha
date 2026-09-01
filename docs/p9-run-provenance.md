# P9 DEV 运行溯源与环境前检

日期：2026-08-31。属于当前 P9.2 工程准备，不是敏感度研究完成或进入 P9.3。

## 运行记录契约

`src/slagalpha/reporting/run_manifest.py` 实现冻结数据契约第 14 节的 DEV 子集：

- `DevRunInputs` 保存代码提交、dirty 状态、策略/参数/Schema 版本、规则与排除表版本、
  Universe 与 Candle 哈希集合、归档 manifest 哈希、成本版本、bootstrap seed、命令参数、
  环境锁哈希，以及 P9 split/敏感度计划哈希。
- `DevRunManifest` 增加 UTC 毫秒精度起止时间、规范 JSON 结果哈希和全记录 `run_id`。
- `DEV_RESEARCH` 必须有完整 Git object ID 且 `dirty_worktree=false`；无提交或 dirty
  的开发记录只能显式标记 `NON_REPRODUCIBLE_DEV_RUN`，不得用于晋级。
- 仅接受 DEV；VALIDATION/LOCKED_TEST 均拒绝，没有布尔开关放行。
- 原始价格等类型应先按各自契约转成 JSON 数据；拒绝非对象结果、NaN/Infinity、非法
  时间和不完整哈希集合。结果中已有 Decimal 字符串不做浮点转换。

这是**声明与持久化契约**，并非授权器。字段格式有效不等于来源已验证；参数版本引用
目前为不透明标识，正式 ParameterVersion 的内容核对、参数与计划绑定仍待实现。
纯构造函数也不会确认调用者填写的提交真的存在，实际 Git 观测由前检负责。

## 保存、恢复与复现

`write_dev_run(manifest, result, artifacts_dir)` 输出：

```text
artifacts/runs/<run_id>/
  result.json
  manifest.json
```

- 结果先发布，manifest 最后发布；manifest 是完成标记。
- 通过同目录临时文件及无覆盖发布保存完整字节，并发同内容写入幂等。
- 文件内容冲突不覆盖、不修复；只有 result 的中断运行可用同内容补全 manifest。
- 已有完成标记但 result 丢失视为损坏，不静默重建。
- `read_dev_run` 重新核对记录哈希、目录 ID 和结果字节；拒绝非规范 JSON 和重复 key。
- 完全相同输入/时间/结果生成相同 run_id；实际运行时间变化会改变运行记录 ID，
  但不改变相同策略结果的 `result_content_hash`。不能用审计时间差异判断策略不确定性。
- 当前文件系统已通过并发发布测试；不支持硬链接的文件系统会报错，不退化为覆盖写入。

测试使用现有 P7 真代码重放明确标注的合成 Candle，然后保存结果；没有创建真实行情
收益报告。本项目没有在 `artifacts/runs` 中生成冒充真实研究的演示结果。

## 当前环境锁

新增 `requirements.lock`，记录当前已测试虚拟环境的 29 个精确发行包版本，包含开发
工具。SlagAlpha 自身通过 Git 代码版本记录，不写 editable 绝对路径或远程下载地址。

- Python：CPython 3.12.13；平台：win32 / AMD64。
- 锁文件 SHA-256：`8c3d1ef3887544516ac06fa3efe7f9bcfc2b81b1f56b1da267cafa6a24574ee7`。
- `reporting/environment.py` 核对 Python、实现、平台、架构，以及全部已安装发行包。
  名称规范化后逐项精确匹配；漏包、多包、版本变化、重复/含歧义条目均拒绝。
- 锁内不允许范围、URL、editable、安装器选项或环境条件，避免只核验部分声明。
- 本轮只读取已安装包的名称和版本，没有安装、升级或联网下载依赖；`pip check` 通过。

这是一份**本地环境精确版本锁**，不是跨平台依赖求解结果，也没有 wheel/source
工件哈希。不能声称二进制供应链已验证；Ubuntu/Debian 部署前需独立解析、锁定并验收
目标环境。不得为通过检查而自动刷新锁文件。

## 只读前检

```powershell
.venv\Scripts\python.exe scripts\p9_dev_preflight.py
```

默认检查项目根目录 `requirements.lock`；如需另一份真实目标环境锁，可显式传入
`--environment-lock`。文件存在时先核对当前环境，失败返回退出码 2，不生成通过报告。

前检还会：

- 验证冻结 split/audit/plan 自身哈希、相互引用和所选文件名，检查策略规则文档未改变。
- 用只读 Git 命令读取项目 root、HEAD 与 dirty 状态；禁用可选索引锁，不 add/commit。
  无法读取时状态为未知并阻断，不能默认为干净工作区。
- 保留 DEV 历史规则输入阻断，不调用策略重放，不访问账户或凭证。
- 将报告写入 `data/manifests/dev_preflight/<hash>.json`，不创建完成运行记录。

报告的 `CHECKS_PASSED` 只表示这些前置检查通过，始终 `research_authorized=false`。
完整参数版本/输入内容核验、实际事件时刻的历史规则、1m/Funding、依赖工件哈希等仍需
后续执行器验证；不能拿该状态启动锁定测试。

退出码：0 = 有限前检通过，1 = 正常发现阻断，2 = 环境不匹配或前检输入/读写错误。

## 提交前真实验收结果（历史记录）

提交前报告版本 `dev-research-preflight/0.2.0`；两次运行结果相同：

- hash：`759e25c55cba9e73d25a8a69e894057a9bfba27e76ce7c0de26d60e648f7b044`。
- 环境版本匹配，策略规则文档未改变。
- `GIT_COMMIT_MISSING`：项目尚无有效代码提交。
- `WORKTREE_DIRTY`：项目文件仍未跟踪/提交，前检没有替用户处理 Git 状态。
- `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`：历史规则证据仍缺失。

环境锁补齐前的 v0.1.0 报告 `bf1c2f15da63364815b99ad9a38debeb12c4121add4a9039db4e95ef8491c94d`
作为旧检查证据保留，没有覆盖。它额外包含 ENVIRONMENT_LOCK_MISSING；不作为当前状态。

新增测试：运行记录 35 项、前检 8 项、环境锁 21 项，共 64 项。Git 正例提交只发生在
临时测试仓库。上述验收时本项目尚未创建提交；后续经用户授权的首次基线与提交后
检查见 `docs/git-baseline.md` 和其本地回执。真实回测、锁定测试和真实下单均未执行。
