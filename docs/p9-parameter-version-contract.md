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
`c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9`，为计划中的
10 个候选各生成一个 `data/manifests/parameter_version/<content_hash>.json`。重复运行只接受
逐字节相同的既有工件；未知候选或冲突内容失败关闭。

实际生成 10 个互不相同的参数内容哈希：

1. `56d5d45f745656ee7730ed152c2474f8a72aee3251f8d4f3fce02492f7da6cda`
2. `5a2dda8736cd10056d10d17dbd0fa381f55c04206f4ac4cc628583c7753b0919`
3. `707ec6a3bd7a0494aa5167d95af9d282398f0381368c8cf4a227ec1f131e94d9`
4. `4efa58859947b94d87a094a8af2534f4deca01aa06fc65e812d9d8240fdecf75`
5. `e0bebfecac0fc44a214a7eb9bb2ebbfebd20bc678ce67261a64edbaf31e0699f`
6. `4a81f88a798202a020aca089e493aee266bf07a6fb567a98a2cc4eef051bb0b1`
7. `29c3c531946adecfbd78db607b7bfa7a0e31959c3c06f27ff8b9dccf099b8226`
8. `8ca881a93d8c17882630e3b464410ca5bbd8f6405958c446b17ec72013eb9165`
9. `e9c52a089a19e04b645612d23e64b97418f04b1fc8dec7499ee380dc704cb3e3`
10. `66d92bd54932d5668e0d541e0df1312fdaef3f50b7706af1865f22efafe1bdd0`

所有工件仍引用带有
`NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS` 阻断的计划；生成它们不改变 P9 的
1/4 完成口径，也没有消耗锁定测试集。
