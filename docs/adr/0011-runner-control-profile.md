# ADR 0011：runner-control/v1 本机执行 profile

日期：2026-09-10。状态：已采纳为本机实现边界；规范仍为 `2026-09-10.draft1` 草案，真实后端与原生客户端验收另记。

本轮把提供的 runner-control/v1 作为固定版本的控制协议接入现有 Asterun 执行器。原始 [SPEC](../protocol/runner-control-v1/SPEC.zh-CN.md)、[Schema](../../src/asterun/schemas/runner-control-v1.schema.json) 与 [状态机](../protocol/runner-control-v1/state-machines.json) 保留原始内容；运行包包含 Schema 和状态机的原字节副本。协议形状校验、领域守卫和现场验证分别成立，接收完整草案不等于已实现全部草案。

Asterun 核心仍不依赖 GrokBot 或其他宿主。当前 `task.create` 要求 `bot_instance_id: null`；setup、模板实例生命周期和宿主事件属于未接入的可选入口。Task、Run、Assignment、Session、Action、原生会话引用各自保留身份，旧任务不因库升级自动变成标准协议任务。

当前 profile 是单用户、本机、`ns_local` 命名空间。主体来自服务进程身份，并通过既有工作区策略和本机 Grant 检查；JSON 中的主体、授权声明不能代替认证。CLI、stdio MCP 和同用户 Unix socket 使用同一应用服务。草案中的远程 HTTP API、SSE、多租户认证与跨主机 fencing 不在本轮承诺内。

协议实现使用 Draft 2020-12 校验器及格式检查，JCS 摘要使用 RFC 8785 库。拒绝未知字段、重复 JSON 键、无效 UTF-8/Unicode、非有限数以及超出安全范围的整数；命令及传输消息上限为 1 MiB。幂等键范围为命名空间、认证主体、方法和调用方键；摘要仅含规范指定的五个语义字段。先授权，再查已受理记录，新命令才检查到期与 revision，避免成功重发被旧 revision 拒绝。

发现响应固定 `schema_version` 与 `draft_revision`。原始 `DiscoveryResponse` 没有 Schema 摘要字段且禁止额外属性，因此本机通过 `feature_profiles` 的 `schema-sha256:<64位摘要>` 公布摘要，同时在本地辅助 `control.resources` 中返回 `schema_sha256`。这是保持原 Schema 不变的 profile 约定，不是对草案字段的追改；客户端应同时固定修订和摘要。

本轮支持 12 个标准命令：任务创建/修改、运行开始/重试/暂停/恢复/取消/接管/归还控制、领域状态 CAS、产物注册和撤销 Grant。发现只列出已接线命令。每任务只支持一个已配置的完整工作区，操作为 `agent.execute`，不能通过路径 selector 声称限制到文件子集；执行上下文的隔离声明保持 `advisory`。`wf_execute` revision 1 使用配置中的默认后端。能力清单、安装、认证、实际调用和客户端可见性仍分别核验。

命令受理、标准实体、事件、Action 意图、Grant、预算预留和 outbox 保存在同一 SQLite 库的 `control_*` 表，并共享事务边界。派发前重新验证配置摘要、Task 约定、权限、期限与租约 fencing token；外部调用在事务外。可能交付但结果不明时进入 `UNKNOWN_REMOTE`，保留预算不确定状态并等待对账，不按普通失败重复派发。租约协调的是本机执行器记录，不能证明原生外部客户端已经被阻止写入。

当前标准 Action 预写的是 `cap_submit_turn` 对应的后端轮次提交。供应商 Agent 在轮次内自行发起的工具调用没有被逐项转换为本协议 Action；因此不能声称所有文件、网络和工具副作用均已预写、逐项审批或获得相同的 fencing 保障。它们继续受原生后端及宿主权限边界约束。

预算只支持 `turns` 的硬上限，约束 runner 派发轮次；不接受把 token、费用、wall time 或供应商内部工具次数包装成已落实的硬上限。暂停/取消先保存意图，再依据未交付状态或后端终止事实确认状态；人工接管等待安全边界。产物通过受控文本上传暂存后按 SHA 和字节数注册，不能接受任意写路径；注册本身不产生验证通过结论。

`val_execution` 仅判断后端执行成功结束，`val_output` 再要求公开输出非空。它们不证明产品正确、测试通过、代码已审查或交付已发布。原有版本绑定的质量流程继续遵循 [ADR 0010](0010-version-bound-quality-workflow.md)，尚未映射为 `wf_execute` 的质量验收；标准协议托管的执行不能借旧 workflow/控制入口绕过其预算和所有权。

SQLite 升到 schema 5，打开 schema 1/2/3/4 时先通过 SQLite backup API 创建同目录一致快照，再初始化新增表。正常状态备份包括控制实体和 blob；不包含工作区内容、用户凭据或原生客户端状态。旧二进制不得直接操作 schema 5。回退时保留新库，停止执行器并对账在途调用，使用升级前快照的独立副本启动相应旧版本；不得覆盖新记录或借旧快照重发已交付动作。

可复现的本机 CLI/MCP fake 路径和逐项支持边界见 [实现说明](../protocol/IMPLEMENTATION.md)。离线与隔离进程证据只证明上述实现路径，不能替代真实账户、模型、后端权限和原生客户端现场验收。
