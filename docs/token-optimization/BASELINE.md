# 输入 Token 优化基线与边界

依据用户提供的 v1.1 复核计划执行第一轮 R0—R3。远程审阅锚点为 main@84ec165；实际本机主工作区为 codex/grok-native-workflow-0924@0ce86a9，干净。上一批独立工作树已提交 cef1cbc，包含阶段门禁、依赖调度、只读历史导入；本轮在该干净工作树创建 codex/input-token-optimization-r0-r3，继承新增能力，没有退回审阅锚点。已读取同仓“规划 Asterun GitHub 自动维护”任务的当前状态（idle），不修改其工作区或自动化。

## 现有入口与复用位置

| 边界 | 当前代码与已有行为 | 本批处理 |
|---|---|---|
| 投递 | application._dispatch_committed → 后端 dispatch/dispatch_stream；Task.text 为原始目标，Run.prompt 为修复/审查载荷 | 在原入口增加默认关闭的边界元数据与显式材料选择；不重写黑盒逐轮请求 |
| 观察 | observe.compact_task_snapshot、compact 事件和 watch --no-events | 复用紧凑投影与游标；分别记录返回载荷估算 |
| 用量 | usage/report.py：run 总计、累计值排除、未知与零区分、分页 | 补未归属会话观察、来源/时间/语义；不把累计值摊给 run |
| 局部检查 | local_checks 流式有界尾部、check_runtime 持久检查引用 | 补已有诊断的范围读取与精确截断元数据，不扩充原始日志保留 |
| 交接 | snapshot、stage_gates、checkpoints、SQLite blob、已有 Task/Run | 增量派生视图、CAS、当前版本复核，保留原契约与审批；不派发新会话 |

## 能力矩阵

“代码/离线”只说明适配器入口，不能代表当前安装的原生客户端或生产账户已验证。所有真实客户端版本、逐请求粒度与现场效果本轮均为 unknown，未启动原生程序探测。

| backend/profile | 可控投递/返回 | 原生工具/轮数 | 新会话/续接/取消 | 实测用量来源 | 逐请求改写/工具结果发送前拦截/原生压缩 |
|---|---|---|---|---|---|
| fake | 代码及离线夹具可验 | 替身，不是模型 | 仅替身生命周期 | 无真实用量 | 不代表原生能力 |
| grok / ACP 文本 | grok.py 的 session/prompt；主控返回可投影 | 非编码 profile 不推断 | 以 grok.py 当前路径和相应协议测试为准，现场 unknown | 文本用量粒度未知 | 当前适配器未实现 |
| grok / workspace-code-v1 | grok_code.py 的 prompt file | 固定 tools/max-turns 构造可离线验证 | grok_session.py 管理绑定/锁/显式 resume；取消保留未知语义，现场 unknown | native_headless_report 的 run 报告，结束前不保证完整 | 当前适配器未实现 |
| codex / App Server | codex.py、codex_runtime.py 的 turn/start 文本 | 不把工具配置当作逐工具改写 | 独立线程/轮次及取消协议替身，现场 unknown | thread/tokenUsage/updated 累计观察，不是单次模型请求 | 当前适配器未实现；不以文档宣称安装版本可用 |
| claude | 适配器单次入口/核心返回 | 只认现有构造，不扩权限 | 一次性会话；原生恢复未支持，现场 unknown | 原生一次性费用；Token 语义未知 | 当前适配器未实现 |
| antigravity / 其它插件 | 按 manifest 与固定插件入口，不推断通用文本控制 | 插件自身边界 | 按既有 capability 声明，现场 unknown | 未知口径不加总 | 未验证，不启用 |

证据：src/asterun/plugins/builtin_capabilities.py、backends/{grok,grok_code,grok_session,codex,codex_runtime,claude}.py、usage/report.py、observe.py，以及 tests/test_grok_native_resume.py（如名称变化以仓库测试索引为准）、test_grok_failures.py、test_codex_runtime.py、test_usage_report.py。逐项最终测试结果记在 STATUS.md，不把本表当成已运行的测试记录。

## 最小设计决策

不建立 Model Gateway、第二任务账本或通用 Context Compiler。原生 Agent 内部模型请求、工具 schema、工具结果与隐藏内容不由核心接管。策略只支持 dispatch_prompt/caller_result；其它 scope 或严格原生 Token 上限拒绝配置。新增行为默认 off；observe 保持原投递字节、参数和权限。硬限制使用明确字节上限，粗略 Token 估算只作说明，不冒充 tokenizer 或模型安全窗口。

交接包是持久原始 Task/Run/证据的派生件。原目标和用户限制不自动摘要；有界读取重新验证权限与文件版本，未决事项不触发接管或重派。R4 会话轮换/压缩与 R5 真实 A/B 不进入本批。
