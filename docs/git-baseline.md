# 首次本地 Git 基线

日期：2026-08-31；用户已确认整理跟踪范围并创建首次本地提交，不推送远程。

## 跟踪范围

- 项目源码、测试、开发脚本、规格与文档。
- `.editorconfig`、`.gitattributes`、`.gitignore`、安全配置模板 `.env.example`、
  `pyproject.toml` 和当前本地环境的 `requirements.lock`。
- 29 份已存在的关键 JSON 清单：合约身份/规则注册表、研究 split/audit/plan、规则缺口
  与验收摘要，以及归档/规范化/Universe 批次摘要等。具体目录白名单在 `.gitignore`。
- 这些已跟踪输入若发生变化，应使工作区变脏；不能为了通过研究前检而自动忽略修改。

不跟踪但保留原地：

- `data/raw/`、`data/normalized/` 原始行情与 Parquet。
- 逐文件下载/规范化清单、全量日度 Universe 快照及批次进度指针。
- `data/manifests/dev_preflight/` 每次 Git 前检输出，避免前检报告本身让工作区变脏。
- 其他未列入白名单的数据目录、虚拟环境、缓存、日志/运行产物和实际 `.env` 配置。

这是代码与关键证据基线，**不是行情数据备份**。被忽略的文件没有删除，仍可供本机
续跑；新克隆只靠 Git 不能重跑完整真实数据审计，需要另行恢复本地数据与批量清单。
没有复制、上传或购买外部存储。

## 原始字节与来源边界

当前 Git 配置为 `core.autocrlf=true`，冻结工件却包含原始字节哈希。因此新增
`.gitattributes` 的 `* -text` 只禁止 Git 自动换行转换，不批量格式化或修改原文件。
`.editorconfig` 仍要求新编辑文本使用 LF；涉及冻结证据的内容改动必须建立新版本。

提交前逐个比较暂存 blob 与工作文件的字节身份，并复核策略规则文档、环境锁及
关键 P9 工件。敏感信息检查未发现真实凭证；`LEVERAGED_TOKEN` 等领域枚举不属于凭证。
不读取账户 Cookie/API Key，也不修改现有 Git 作者身份。无自定义提交钩子/签名配置。

## 验收方式与恢复

首次提交在原有 `main` 分支上完成，不新建分支，不推送远程。验收顺序：

1. 验证待提交文件范围、体积、字节一致性，运行测试/静态检查/依赖检查。
2. 创建本地提交后确认 `git status --porcelain` 为空。
3. 运行 `scripts/p9_dev_preflight.py`，应能读取真实提交且 `dirty_worktree=false`，
   同时保留 `NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS` 数据阻断。
4. 重复前检必须产生相同报告哈希，且不弄脏 Git 工作区。

通过后才创建本地回执 `artifacts/git-baseline-2026-08-31.json`，记录实际提交号、树 ID、
文件数量、检查结果及提交后的前检报告。回执属于已忽略的运行产物，不把提交号写回
已提交文件造成新的 dirty 状态。**回执不存在时，不把本说明视为提交成功证明。**

```powershell
git log -1 --format="%H %s"
git status --short
.venv\Scripts\python.exe scripts\p9_dev_preflight.py
```

Git 基线只解除代码溯源阻断，不验证历史 tick/step，不执行真实回测或锁定测试。
整体工程进度仍为 9/14（约 64%），P9 内部 1/4。
