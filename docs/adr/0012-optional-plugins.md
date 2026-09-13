# ADR 0012：可选插件与现有后端兼容

本 ADR 接纳 2026-09-11 交接包的 M1 范围，按 P00→P05 实施；不提前承诺 P06 之后的供应商和工作流。当前代码已比调研 a7 更新到 a8，Antigravity 的受限读取、soft-deny、取消阶段凭据沿用现有实现。

插件 manifest 使用独立 `asterun-plugin/v1`。注册仅读静态 metadata 和 Schema，明确指定安装 artifact、摘要与运行 argv；不扫描 HOME 或全部 entry points。支持任意命名空间 plugin_id，不改核心供应商白名单。安装属于受信代码部署；首版不提供在线市场或服务启动时自动 pip。外部 wheel 在独立环境运行，核心不导入其 SDK；子进程提供故障隔离，不是恶意插件沙箱。

worker 使用 JSON-RPC 2.0 NDJSON，`params.protocol_version=asterun-worker/v1`；业务输入与核心生成的上下文分别放 `input`、`context`。响应须回同一请求 ID 和协议版本。不支持的能力显式报错，退出码或插件自报完成不能成为业务验收。

兼容内置 registry factory 返回原适配器类，保留可选方法和原生生命周期。既有 Grok/Antigravity 的发送前回执仍只由受信内置实现判定；外部插件不能自报未发送来释放预算。旧 import 路径至少在本开发发布周期保留。

v1 配置解析和 `to_dict()` 语义保持，避免改变历史 RCv1 的 configuration_sha256。v2 显式 opt-in，旧 alias 仍存在；迁移只输出候选并由用户明确写到新文件，不覆盖旧配置、账户主体、原生 session、任务或证据。ProviderAccount 与现有 Principal/account_ref 分离，不能把旧 account_ref 推断成供应商账户。

新计划和计费绑定采用独立扩展记录，复用现有核心事务、Grant 与执行意图；冻结 `runner-control/v1 / 2026-09-10.draft1` Schema 和状态机不修改。`wf_execute@1`、旧 task.submit、QualityRuntime 与旧原生恢复分别回归。共享池预留只限制 Asterun 自身，未知余额/费用不能写成无限或账户级账单保证；未知远端保留预留并等待对账。

阶段实现、实际命令、数量、兼容/回滚和未验证项见 M1 记录（仅仓库历史记录）。真实账户、权限执行效果、费用、原生续接和客户端可见性需要独立授权验收，本次保持 not_tested。

P05 将 a8 固定提交的 profile、NDJSON 运行器与命令构造机械复制到独立标准库包，来源清单与核验脚本同时交付；原内置文件不修改。外部包只在显式注册的实例中替换同 ID 入口，不自动迁移旧连接。唯一运行差异是原生 CLI 留在 worker 进程组，使本地超时可回收子进程；这不证明远端取消。单次同步 RPC 在终态回执中交付原生 ID，途中宿主死亡可能尚无引用，核心保留 unknown 与预留。没有伪造跨 RPC 原生生命周期、额度读取或原生客户端验收。

新 v2 的执行主体绑定也应用于任务、事件、会话和审批读取，阻止同工作区跨主体访问。旧 v1 没有插件绑定的记录仍走原规则。该检查复用现有 Task/Run/Session 与同库绑定，不新增第二套作业状态机。
