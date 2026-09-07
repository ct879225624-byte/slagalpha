# 开发续接记录：2026-09-05

继续执行已确认的顺序开发方式：完成一项后汇报整体进度，再进入下一项；普通实现选择
使用默认方案。此前记录见 `docs/development-checkpoint-2026-09-03.md`。

## 起点与顺序

续接提交 `7d90066`，工作区干净。第 17 项正式整日计算入口已完成：默认回归
812 passed、1 skipped，Ruff、mypy 147 文件、pip check 通过。

1. 第 18 项：完整合成验收接到正式整日计算入口，验证来源保存恢复及请求集合。
2. 第 19 项：接通跨日计算、不可变保存和完整请求集合的正式顺序入口，验证中途失败。
3. 第 20 项：用完整合成来源验证跨日入口与市场输入链路。

仍以 P0–P8 完成、P9 1/4、整体 9/14（约 64%）计。历史规则、完整多周期来源及
递归种子审核是实际研究前置条件；工程接线不会批准种子或自动提升规则审核状态。

## 本次运行时变化

第 19 项全量回归发现当前 `.venv` 实际使用 CPython 3.12.14，冻结 `requirements.lock`
要求 3.12.13。虚拟环境的 `pyvenv.cfg` 仍记载创建时版本 3.12.13，但其引用的基础运行时
现在已是 3.12.14。29 个依赖包的名称/版本与冻结锁完全一致；策略和依赖锁原始哈希未变。
检查现有运行时缓存后，没有找到可执行的旧版 Python。

本轮合成功能验收在 3.12.14 下进行，不能据此声称冻结环境已复现。保留环境门禁失败，
未修改锁、虚拟环境配置或已冻结报告，也未安装/降级 Python。正式研究需要恢复独立的
3.12.13 运行时并重新核验，或另行审核环境迁移；环境变化不会被默认为获批迁移。

用户随后明确授权恢复环境。Python.org 的 3.12.13 发布页确认该安全版本只有源码，
没有 Windows 安装包；本机也没有编译工具或旧运行时可恢复。因此使用 Astral `uv 0.12.10`
下载隔离的 `python-build-standalone` CPython 3.12.13，未注册系统、未修改 PATH 或现有
`.venv`。29 个冻结 wheel 从 `data/dependency-artifacts/` 离线安装，环境锁哈希与
`requirements.lock` 完全一致，pip check 通过。临时验证环境位于用户 Temp 目录，
不是项目交付物，也不改变已冻结依赖工件。

## 第 18 项：整日入口合成验收（完成）

验收代码按扫描清单惰性生成四周期前缀，调用 `compute_source_bound_scan_day` 返回
整日结果和 P6 来源，随后执行保存、恢复及完整请求集合校验。空池同样走正式入口。
单扫描位正向样例仍调用 `compute_source_bound_scan_slot`，合成行情和预期结果保持原样。

两项显式验收通过（568.69 秒）：95 位为 94 个 NO_SIGNAL、1 个 accepted plan；
来源保存恢复、完整请求集合、526 根合成 1m 和 1 条 Funding 均通过。实际修改临时
ZIP 后旧报告复用失败；经原始校验但起点冲突的历史前缀也被拒绝。没有替换验证函数。
本次来源 JSON 共 3,111,141 bytes；尺寸含运行凭证，不是跨运行不变量或容量保证。

默认回归最近一次为 812 passed、1 skipped；本项仅调整验收接线与文档，Ruff、
mypy 147 文件和 diff 检查通过。整体保持 9/14（约 64%）、P9 1/4。
下一项：正式跨日顺序入口，使计算、每日保存和最终完整集合对账由生产代码组织。

## 第 19 项：跨日计算、保存与完整集合（完成）

第 18 项提交 `585dff7`。新增 `compute_source_bound_request_set_with_checkpoints`，完整
上下文通过后才消费有序日期/前缀流；跨日共享新来源链，逐日计算、重验、不可变保存，
最后汇总全部 accepted 请求。缺日、额外项、来源漂移或中途异常均不返回完整集合。

已保存的有效日可保留供恢复；BLOCKED 日只能作为诊断检查点。重试仍从头复验并仅复用
相同内容，没有自动跳过来源、种子审批或研究授权。只写每日来源，返回的请求集合不落盘。

新增 24 项定向测试通过。中断前相关入口共 87 项通过（23.46 秒）；Ruff、mypy 148 文件、
pip check 和 diff 检查通过。中断前全量回归为 835 passed、1 failed、1 skipped
（404.97 秒）。唯一失败为环境语义测试不接受当前 CPython 3.12.14，符合冻结锁必须为
3.12.13 的既有门禁。恢复后单独复跑该测试仍为 1 failed；只读 DEV preflight 返回
`ENVIRONMENT_LOCK_MISMATCH`，29 个包版本差异为空。

根据 checkpoint 规则，当时没有强行提交。恢复后先单独复现并解除环境失败，再在隔离
3.12.13 环境运行相关 87 项，全部通过（20.69 秒）；全量回归 836 passed、1 skipped
（330.90 秒）。Ruff、mypy 148 文件、pip check、diff 检查通过；只读 preflight 正确
恢复为历史规则和 dirty worktree 阻断，不再报告环境错误。

第 19 项提交 `519a7ac`，包含：

- `src/slagalpha/research/source_request_set.py`：跨日正式顺序入口。
- `tests/test_scan_execution.py`：24 项顺序、失败、来源链与真实空池检查点测试。
- `docs/p9-scanner-input-contract.md`：入口契约和冻结环境回归状态。
- `docs/p9-source-pipeline-acceptance.md`：当前解释器与验收解释边界。
- `docs/development-checkpoint-2026-09-05.md`：本恢复记录。

下一项是把完整合成来源验收改用跨日入口，确认正式入口返回的请求集合可直接进入
1m/Funding 市场数据验收，并保留所有来源反例。

## 第 20 项：跨日入口完整合成验收（完成）

验收改为向 `compute_source_bound_request_set_with_checkpoints` 惰性提供两个 DEV 日及其
四周期历史流，由正式入口逐日计算、保存并直接返回完整请求集合。随后使用共享来源链
恢复两日结果核对 outcome，再将同一请求集合接入 1m/Funding 验收。起点冲突和 ZIP
篡改反例保留；单扫描位 accepted 正向测试保持不变。

开始慢验收前，第 19 项已提交，工作区只有本项验收和 checkpoint 改动。Ruff、mypy
148 文件和 diff 检查通过；在隔离 CPython 3.12.13 环境显式运行两项端到端验收，
全部通过（495.86 秒）。

跨日入口完成一个空池日和一个 95 位活跃日，直接返回完整请求集合；恢复结果仍为
94 个 NO_SIGNAL、1 个 accepted plan。随后 526 根合成 1m 与 1 条 Funding 验收通过，
经原始校验但起点冲突的前缀和成功报告后的 ZIP 篡改均正确拒绝。来源 JSON 合计
3,111,141 bytes；包含运行凭证，不是跨运行尺寸不变量或全 DEV 容量证明。

本项只改变验收接线和进度文档，不改变生产公式、schema、冻结规则、历史种子或授权状态。
整体保持 9/14（约 64%）、P9 1/4。下一步在干净提交上复跑只读 preflight 与输入语义
门禁，确认剩余阻断仍全部来自真实规则/数据，而不是本批工程接线。

## 第 21 项：干净提交准备度复核（完成）

第 20 项提交 `b0a3e35`，检查时工作区干净。冻结 CPython 3.12.13 环境运行只读
preflight，报告 `87541bdb21190c73b64d17a4ae2e74d5240ea81b602fe4d42ac851a3849269ad`；
Git 提交与环境锁通过，只剩 `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS`。

输入语义报告仍为
`095a2f2e7d33e2e5247a5656a919c9fec15b38daa4e86e320331b0dd4f9f20fd`：11 类角色通过，
多周期 Candle、1m Candle、Funding 三类 deferred；18 个递归历史依赖阻断原样保留。
报告保持 `research_authorized=false`、`strategy_executed=false`、
`locked_test_consumed=false`。未运行研究、下载行情、修改证据或打开任何授权。

结论：第 17–20 项接线没有增加语义门禁错误；下一实质步骤需要历史规则证据及实际
多周期递归种子材料。整体仍为 9/14（约 64%）、P9 1/4。

## 第 22 项：冻结环境恢复脚本（完成，2026-09-07 安全恢复点）

第 21 项提交 `fdce391`，开始本项时工作区干净。新增
`scripts/p9_restore_frozen_environment.ps1`，固定 CPython 3.12.13、uv 0.12.10、
bootstrap 工件哈希、环境锁及依赖 manifest；默认只在项目外 Temp 目录创建隔离环境，
不修改现有 `.venv`、系统 PATH 或注册表。依赖只从已冻结的 29 个本地 wheel 安装，
安装后重读完整 wheel manifest、环境锁并执行 `pip check`。

已有隔离环境的幂等复验和默认 Temp 目录的从零 bootstrap 均成功，最终输出 `READY`；
CPython 为 3.12.13，29 个冻结 wheel 安装完成，环境锁仍为
`8c3d1ef3887544516ac06fa3efe7f9bcfc2b81b1f56b1da267cafa6a24574ee7`，
`pip check` 通过。项目内安装路径（含不同大小写）正确失败关闭，PowerShell 语法通过。

恢复后的默认环境运行全量回归：836 passed、1 skipped（368.92 秒）；唯一跳过仍是
Windows 环境不可创建符号链接。Ruff 全部通过，mypy 128 个源码文件通过。修改仅包含
恢复脚本、README、本依赖恢复文档和此 checkpoint；未修改冻结规则、策略、依赖锁、
证据或授权状态，也未运行真实研究、锁定测试、行情下载、push 或部署。

下一任务不是新增工程功能，而是 P9 真实输入解除阻断：需要权威历史合约规则、完整实际
多周期来源及已审核的 18 个 ATR 递归历史依赖，再生成并验收真实 1m/Funding 请求数据。
材料未具备时应继续保持 `research_authorized=false`，不得启动参数研究或 LOCKED_TEST。
整体仍为 9/14（约 64%）、P9 1/4。
