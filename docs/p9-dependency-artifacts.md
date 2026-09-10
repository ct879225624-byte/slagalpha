# P9 本地依赖安装包工件

日期：2026-09-02。补齐现有 Windows CPython 环境的安装包哈希，不增加或升级依赖。

## 真实验收

- 原 `requirements.lock` 29 个精确版本保持不变；环境字节哈希仍为
  `8c3d1ef3887544516ac06fa3efe7f9bcfc2b81b1f56b1da267cafa6a24574ee7`。
- 29 个 wheel、78,976,028 bytes，保存于 `data/dependency-artifacts/`，不纳入 Git。
- manifest：`060d25d955664a803bbdc39b1eecbd466cb57a4a9c5ee278c9c15cb4358bc567`
- manifest 文件 SHA-256：`3740153d436cc461306f8027359d9c50de367e0c2c9f0b2c16e0c70f964a0bca`
- 逐文件校验名称、版本、METADATA/WHEEL 标签、完整 SHA-256、大小和平台兼容性；
  同输入重复生成哈希一致。
- 完全离线、强制哈希、忽略已安装包的 pip dry-run 成功解析全部 29 个版本。
- 未执行安装、升级或远程推送；当前环境检查与 `pip check` 通过。

门禁不只检查 manifest 文件，而是重读其中声明的整个 wheel 目录：缺失、多余、替换、
元数据不匹配、跨平台、符号链接和路径逃逸均失败关闭。此版本有意只接受当前本地
Windows AMD64 CPython 的已支持 wheel 标签，不宣称是 Ubuntu 部署锁。

## 复现

下载命令仅用于恢复缺失的本地 wheel；现有完整目录不需要重复下载：

```powershell
.venv\Scripts\python.exe -m pip --isolated download --index-url https://pypi.org/simple --only-binary=:all: --no-deps --disable-pip-version-check --dest data/dependency-artifacts -r requirements.lock
.venv\Scripts\python.exe scripts\p9_dependency_artifacts.py
.venv\Scripts\python.exe -m pip --isolated install --dry-run --ignore-installed --no-index --find-links data/dependency-artifacts --only-binary=:all: --require-hashes --disable-pip-version-check --report artifacts/dependency-offline-dry-run.json -r artifacts/dependencies-060d25d955664a803bbdc39b1eecbd466cb57a4a9c5ee278c9c15cb4358bc567.lock
```

下载以官方 PyPI 为唯一索引，可能复用 pip 的 HTTP 下载缓存。清单冻结的是本次取得的
安装包字节，不是发布者签名，也不证明已经安装的每个文件均来自这些 wheel。
dry-run 验证兼容性、依赖解析与哈希，不等于实际重建新环境；本次没有重装当前环境。

## 冻结环境恢复

Python.org 的 CPython 3.12.13 Windows 版本只有源代码发布，没有官方 Windows 安装包。
项目使用固定的 Astral `uv 0.12.10` 在项目目录外安装对应的
`python-build-standalone` 运行时，并从上述 29 个本地 wheel 离线恢复依赖：

```powershell
pwsh -NoProfile -File scripts\p9_restore_frozen_environment.ps1
```

默认安装到系统 Temp 下的 `slagalpha-python-31213`，也可用 `-InstallRoot` 指定另一个
项目外目录。脚本固定并检查 uv ZIP、uv 可执行文件、`requirements.lock`、依赖 manifest
及每个 wheel 的 SHA-256；环境依赖安装使用 `--no-index --require-hashes`。它不会替换
现有 `.venv`，不会注册系统 Python、修改 PATH、写入 Git 数据目录或打开研究授权。

脚本成功时输出一行 `status=READY` 的 JSON，其中 `venv_python` 是后续命令应使用的
解释器路径，`research_authorized` 固定为 `false`。重复运行会重新核验并复用相同环境；
冻结文件、工具、解释器或已安装包版本不一致时失败关闭。uv 和 Python 仅在对应的固定
工件不存在时联网获取，依赖包始终只读取项目内已验收的本地 wheel。

## 研究门禁影响

依赖工件类别已通过语义检查，内容缺失类别从 3 减为 2（1m、Funding）。
历史规则在 DEV 改为显式 approximate fallback；完整多周期数据和递归历史依赖仍由实际
扫描按受影响区间失败关闭。该依赖工件本身不授权策略执行。
整体保持 9/14（约 64%），P9 1/4。新增 12 个 wheel 合成测试及 2 个门禁重验测试，
全量 385 通过、1 个 Windows 符号链接权限测试跳过；Ruff、mypy 105 文件通过。
