# ADR 0007：质量门绑定哈希与有界队列

日期：2026-09-07。状态：已采纳（A3 离线）。

## 决策

任务验收与运行终态分开。`workflow.evaluate` 必须携带或默认使用任务当前的 `config_revision` 与 `input_hash`；不匹配则拒绝，旧证据不能用于新输入。`run_succeeded` 只是检查输入，不能单独把 `acceptance` 标为 `passed`。未配置确定性检查时记 `not_configured`。

2026-09-09 修正：要求审查时，必须取得受信且绑定当前目标的实际审查证据。`can_dispatch` 或 `approved_substitute` 的配置均不能产生审查通过结论。后续接入的审查执行器与目标快照协议见 [ADR 0010](0010-version-bound-quality-workflow.md)；没有对应证据仍保持 `pending` 并暂停。确定性 contains 检查仅检查运行输出，不能以提示词回显预期代替任务产物。

调度先 `admit` 再 `commit_intent`。队列满返回 `RATE_LIMITED` 且不落库。`max_queue=32`、`per_backend_concurrency=1`、`max_repairs=2`、`max_runs_per_task=4` 来自调优规划候选默认值，不是实测 SLO。

## 回退

使用升级前状态库快照的独立副本回退，先对账在途请求；不能忽略新版本的质量状态、预算或 finding 后重派旧意图。当前 schema 与具体回退边界见 ADR 0010。
