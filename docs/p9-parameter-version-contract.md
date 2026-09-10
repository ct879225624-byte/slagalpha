# P9 DEV 参数版本绑定契约

日期：2026-09-01。属于 P9.2b 执行准备，不是研究结果、参数优选或参数冻结。

## 目的与边界

`src/slagalpha/research/parameters.py` 将敏感度计划中的单个候选参数保存为内容寻址工件：

- 仅接受 `DEV`，没有 VALIDATION 或 LOCKED_TEST 放行开关。
- 同时绑定策略版本、策略规则原始字节 SHA-256、敏感度计划哈希和完整候选内容。
- 参数引用固定为 `parameters/0.1.0:<content_hash>`；`DevRunInputs` 不再接受任意字符串。
- 更改任一参数、候选哈希、规则哈希或计划哈希都会改变版本，旧内容哈希不能复用。
- `require_parameter_plan_binding` 会重新验证双方内容，并确认候选完整对象属于指定计划。
- 工件始终记录 `strategy_executed=false`、`locked_test_consumed=false`，不包含授权字段。

这只解决参数内容及计划归属。完整行情、Universe、历史规则、Funding、归档清单和依赖
工件的实际字节核验仍属于后续运行输入门禁；历史规则缺失时不得调用 P7 执行研究。

## 已生成工件

命令：

```powershell
.venv\Scripts\python.exe scripts\p9_parameter_versions.py
```

脚本读取冻结计划
`688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9`，为计划中的
10 个候选各生成一个 `data/manifests/parameter_version/<content_hash>.json`。重复运行只接受
逐字节相同的既有工件；未知候选或冲突内容失败关闭。

实际生成 10 个互不相同的参数内容哈希：

1. `81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b`
2. `5bf9601a38d82463cdb33b8fd4c69a019c998bee116ffa90e14528ee22a49956`
3. `24f57efe6396cef12ff70b7d6e9cea235e56c3485055acb21037ead0ede04bb4`
4. `7fa372f8cd3b7c7ee5ed68a14d4fa4291651c526bccbba9f843c4b0cde2c4859`
5. `4fa144e32ee64ca7d724edc3508e484e001b2e9de3009b7648ac38bfc28b3327`
6. `92981dbecd337f6defda80f953cff578089725ca51c74eb4a4118fdd0f3b58d5`
7. `edbfb8a5bde6bc46c898dd641b679c429d4322fbd5722a202f0266845a80360f`
8. `96f715fe74f3ea081cf43f538cb5f651e94114ad484686ea6d9267cbd2b9f37d`
9. `c557e797d8cd5ec4b1d8ec0f53f4c63732bd9b06eed89e2fdd812dda413aca29`
10. `64cc07a4009be823bb4a016e56703da2d57f4cff286811daa78d6e564253baf7`

所有工件引用已解除 DEV 历史规则 blocker 的计划；规则仍为 `UNVERIFIED/MEDIUM` 并带
approximate tick-size warning。生成参数工件没有运行策略，也没有消费验证集或锁定测试集。
