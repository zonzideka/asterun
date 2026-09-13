# runner-control/v1 技术规范

版本：2026-09-10.draft1  
性质：agent-runner 项目内部协议草案，不是行业标准，不是已部署 API  
基线：保留现有 SDK 调用、持续会话与人工介入路径；不改为 Desktop UI 自动化  
交付范围：数据结构、命令与事件、状态机、权限、预算、恢复、适配器契约及验收规则

## 1. 目标与实现边界

runner-control/v1 管理“用户要完成的任务”，不替代 Agent 的推理循环，不自研 IDE，也不把工作目录、Git worktree 或供应商线程当作业务任务身份。

第一版沿用现有语言和成熟存储组件，以一个模块化服务实现。本文的 TypeScript 仅表达适配器边界，不要求迁移技术栈。软件免费分发；模型账号和资源由用户提供，不建设作者强制中转服务。Grok Bot 是可选入口，不是 core 的硬依赖。

规范中的“必须”是实现要求，不代表当前 agent-runner 或上游已经具备该能力。本次核验只覆盖公开文档及既有设计文件，没有连接用户正在运行的 SDK、Grok 或 super.engineering 实例。

第一版首要工作流是“实现—确定性检查—独立审查—有上限的修复—验收”，可简化为只读第二意见。通用 DAG 编辑器、跨组织调度、复杂多级预算转移和新的聊天客户端不在首版范围内。

## 2. 外部接口事实与设计选择

Codex SDK 文档提供线程创建、继续及按 ID 恢复；App Server 提供线程、轮次、事件、审批和中断。两者作为不同适配器配置处理，不能假设任一语言 SDK 都暴露 App Server 的全部接口。[S1][S2]

Grok Build 提供 headless 和 ACP；Grok Bot 是带持久化计算环境的产品入口。本文将 `grok_build_acp` 作为执行后端，把 Grok Bot 放在北向入口或外部执行阶段；不杜撰 Grok Bot 的创建、暂停或订阅事件 API。[S3][S4][S5]

super.engineering 的 `sc` 需要应用运行；它的 live session 标识不是永久身份，团队在应用重启后可以成为 Interrupted。该适配器是桌面依赖型工程执行后端，不是 Linux 无头服务的必要组成。[S6][S7]

ACP 的接口必须经过版本与能力协商。本文的 `session/new`、`session/load`、`session/prompt`、`session/cancel` 是核验的 v1 示例，不将其套用到其他协议版本。[S9]

## 3. 协议总则

### 3.1 版本与编码

传输使用 UTF-8 JSON。对象内为 `schema_version: runner-control/v1`，发现接口另外返回 `draft_revision: 2026-09-10.draft1`。在草案阶段，双方必须固定草案修订和 Schema 摘要，不仅检查 v1 字样。

结构校验采用 JSON Schema Draft 2020-12。[S10] 规范性结构为 `schemas/runner-control-v1.schema.json`，其中 `$defs` 可以独立引用。每个输入还需要领域校验；Schema 通过不代表授权、跨对象关系或业务条件正确。

命令拒绝未知字段和重复 JSON 键。协议整数限制在 JavaScript 安全整数范围，时间使用带 Z 的 UTC 时间字符串；UI 单独转换时区。金额用整数微美元 `usd_micros`，不用浮点美元。供应商小数用量通过其专用记录表达，不强行截断成本。

扩展只进入 `extensions`，键必须带命名空间，例如 `example.feature`。扩展不能覆盖 core 的状态、权限、费用与验证规则。未知枚举应报不兼容，不猜测映射。

### 3.2 身份与引用

所有实体具有 `id`、`namespace_id`、`revision`、创建/更新时间和类型。ID 在保存期内稳定，不复用；“持久任务身份”不等于永不删除。删除和保留政策必须可配置。

任务、执行、子任务、会话、供应商会话分别使用 Task、Run、Assignment、Session、NativeBinding。不能令 `task_id = provider_session_id`。

`namespace_id`、`principal_id` 由认证层确定，不接受 LLM 自报身份。请求里的 Bot ID、任务 ID、资源 ID 只是引用，每次都必须验证可访问关系。即使单用户部署也执行该检查。

供应商会话恢复必须同时匹配 backend 实例、账号、运行环境、配置和原生存储，不凭一个相同字符串认定为同一会话。深链接可缺省，Desktop 可见性必须单独验证，不能自行拼接私有 URL scheme。

### 3.3 单一事实来源

事务型状态存储是运行态权威来源；事件记录和可重建的 Markdown/JSON 文件是投影。原生聊天历史仍由供应商保存，runner 保存引用和必要公开输出，不重建或索取模型隐藏推理。

领域 State 与系统状态分开。`domain.*` 表示协议决策和观测；`system.*`、Task 状态、审批、预算和证据结论只由 core 服务维护。聊天消息不能直接改变这些状态。

## 4. 对象模型

### 4.1 九类业务对象

| 对象 | 主职责 | 关键关系 |
|---|---|---|
| Task | 目标、约束、验收、预算 | 拥有多个历史 Run；v1 同时最多一个活动 Run |
| Run | 一次业务执行尝试 | 固定任务约定、工作流和配置版本 |
| Assignment | 一个角色的一次执行 | 依赖、执行模式、输入与验收 |
| Session | Agent 会话的 runner 身份 | 绑定后端及原生线程 |
| ExecutionContext | 工作目录、容器、worktree、远端环境 | 关联资源、隔离与写租约 |
| Capability | 注册的操作定义 | 输入/输出 Schema、风险、执行控制级别 |
| Artifact | 不可变内容及其元数据 | 哈希、大小、来源、敏感等级 |
| State | 一个版本化领域值 | ownership、CAS、证据与有效期 |
| Event | 已提交的状态变化记录 | 任务流序号、关联链、主体与证据 |

### 4.2 十类配套控制记录

Action、Grant、Approval、Budget、Reservation、Capsule、Evidence、BackendDescriptor、Operation、BotInstance 有独立 Schema。九类业务对象不是“整个系统只能有九张表”，也不是九个微服务。

其中 Action 是真正的执行审计单位，Operation 是外部命令受理记录。一次 `run.start` 的 Operation 完成，仅表示该命令成功建立/启动相应 Run，不表示业务任务完成。

Capsule 通过 Artifact 保存内容，自己记录所依赖的任务约定、事件位置、State 版本、资源哈希以及未决动作。Budget 通过 Reservation 留存并发预留和结算，不把一个可任意覆盖的数字当作账本。

### 4.3 必须执行的跨对象约束

所有关联对象处于同一 namespace，或经过明确支持的受控共享；v1 默认不跨 namespace。Run 的 task、Assignment 的 run、Session 的 assignment、Artifact/Evidence 的归属必须一致。

依赖图无环，不能依赖其他 Run 中的未验收 Assignment。历史成果需要作为已验证输入 Artifact 复用。模型切换创建新 Session；重试一个已失败角色创建新 Assignment attempt，保留旧记录；完整业务重试创建新 Run。

配置摘要、任务约定 revision、资源快照必须匹配。配置扩大权限或改变数据接收方时，先暂停并重新授权，不静默继承旧许可。

## 5. 北向接口与命令信封

### 5.1 传输轮廓

首版提供一种成熟的认证传输即可。HTTP 绑定建议如下：

```text
GET  /v1/discovery
GET  /v1/tasks/{task_id}
GET  /v1/runs/{run_id}
GET  /v1/operations/{operation_id}
GET  /v1/backends/{backend_id}
GET  /v1/tasks/{task_id}/state/{key}
GET  /v1/tasks/{task_id}/events?after={sequence}
POST /v1/commands
```

事件接口可以返回 SSE；普通 JSON 分页轮询是同语义替代，不能因为客户端不支持 SSE 就丢失状态。MCP 北向映射复用同一命令处理器，不另建状态机。实体读取返回对应 Schema，列表通过授权过滤。

大内容经独立受控的上传入口暂存，再由 `artifact.register` 接入；上传不能产生任意路径写入权限。v1 命令建议上限 1 MiB，由发现接口公布；大 prompt、补丁和日志使用 Artifact 引用。

### 5.2 修改型命令

17 个首版命令详见 `event-catalog.json` 和 Command Schema：

```text
task.create            task.amend
run.start              run.retry
run.pause              run.resume              run.cancel
run.takeover           run.return_control
state.put              message.send
artifact.register      action.propose
approval.request       grant.revoke
setup.answer           setup.verify
```

不提供 `task.set_status`、`approval.set_approved`、`grant.create`、`budget.set_remaining`、`evidence.set_pass` 给 LLM。可信授权界面与验证器使用独立内部服务身份，不经 Bot 可调用的通用写接口伪造审批。

示例：

```json
{
  "schema_version": "runner-control/v1",
  "request_id": "req_start001",
  "idempotency_key": "demo-run-start-0001",
  "expected_revision": 2,
  "expires_at": "2026-09-10T00:05:00Z",
  "method": "run.start",
  "params": {
    "task_id": "tsk_demo001",
    "workflow_id": "wf_review001",
    "workflow_revision": 1,
    "configuration_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

日期、身份和摘要是合成示例，不能直接作为实际授权使用。

### 5.3 revision 作用对象

`task.create`、`artifact.register`、`message.send`、`action.propose` 为创建操作，`expected_revision = 0`；它们仍须在事务中检查关联对象的状态及租约。`run.start`、`run.retry`、`task.amend` 比较 Task revision。各 run 控制命令比较 Run revision；`state.put` 比较指定 State，首次建立为 0；`approval.request` 比较 Action；`grant.revoke` 比较 Grant；setup 命令比较 BotInstance revision。

v1 不允许调用方通过将 `expected_revision` 设为 0 来跳过已存在对象的并发检查。未知参数、错误目标状态、过期配置和不匹配的 revision 在调用后端前拒绝。

### 5.4 受理与结果

异步命令入账后返回 202、Operation ID 和 ACCEPTED；同步完成可返回 200/SUCCEEDED。客户端查询 Operation 或订阅 Event，不能把 202 渲染为“任务成功”。重发相同幂等键返回原 Operation，不新建执行。

错误结构包含 `code`、`message`、`request_id`、`retry_class`、可选退避时间和 Operation 引用。429 及后端限流使用其提供的等待信息；未知远端结果返回 `UNKNOWN_REMOTE`/`after_reconcile`，不返回普通的“请重试”。

## 6. 状态机与完成条件

机器可读的允许边见 `state-machines.json`。以下守卫条件与边同样具有约束力，不能只检查 from/to。

### 6.1 Task

DRAFT 完成约定验证后进入 READY。首个 Run 受理时进入 ACTIVE。所有必要验收通过后 SUCCEEDED；执行终止且无安全自动恢复路径时 FAILED；取消已确认后 CANCELED。

FAILED 只能通过显式 `run.retry` 进入 ACTIVE，并创建新 Run，不修改原 Run 终态。SUCCEEDED/CANCELED 不自动重开。未改变目标的重试保留 Task ID；改变目标或验收标准则变更 contract_revision，先暂停旧执行并处理在途动作。`task.amend` 不提供资源范围扩张；更换资源或数据接收方经配置/授权服务完成并重新验证，不能借修改目标突破既有 Grant。

### 6.2 Run

主要路径：QUEUED → PREPARING → RUNNING → SUCCEEDED。WAITING 使用独立 `blocked_reason`，不把输入、审批、额度、登录等原因塞成几十个状态。

暂停：RUNNING → PAUSING → PAUSED。收到中断请求回执不等于 PAUSED；必须确认活动轮次、受管子进程及副作用状态已到安全边界。只支持轮次边界停止的后端继续显示 PAUSING。

恢复：PAUSED → RECONCILING → PREPARING/RUNNING。连接中断、租约丢失或在途结果不明也进入 RECONCILING；不能从恢复入口直接启动下一条有副作用动作。

取消：先 CANCELING，再 CANCELED。无法确认外部任务停止时保持 CANCELING，或进入 RECONCILING/WAITING，不能显示“已取消且没有后续影响”。取消不是回滚；已经发送的消息、提交或外部变更保留为已发生事实。

Run 分别保存观察状态 `status` 和持久控制意图 `desired_state`（RUNNING、PAUSED、CANCELED）。pause/cancel 命令在同一事务中写入意图和事件；连接丢失不清除意图。对账发现远端仍在运行时，继续完成暂停/取消，而不是自动转回正常执行。只有显式 resume/return_control 经过 CAS、权限与对账后才能把 PAUSED 意图改为 RUNNING；取消意图不可在同一 Run 撤回。取消与成功竞态按权威存储的提交先后判定；取消先受理时保留实际已完成产物，但不再发出后续工作。

QUEUED/PREPARING 阶段也允许先进入 PAUSING，确认没有在途执行后落为 PAUSED。FAILED 不得掩盖仍在活动或结果未知的子任务：先停止/对账已派发工作，再结束 Run。

终态 Run 不重启；同一 Task 的新执行为新的 Run。

### 6.3 Assignment 与 Session

Assignment 从 RUNNING 进入 VERIFYING，而不是直接 SUCCEEDED。必须取得本角色验收结果、完整产物和适用版本。只说“完成”可登记消息或报告，不能代替验收。

Session 的 DISCONNECTED 表示失联，不等于 LOST；先尝试定位。LOST 是确认无法续接；通过新 Session 和 Capsule 恢复任务，而非把旧 Session ID 换绑给另一个后端。CLOSED 不代表删除供应商会话。

### 6.4 Action

主要路径：PLANNED → ADMITTED → EXECUTING → VERIFYING → SUCCEEDED。必要时先 WAITING_APPROVAL；授权拒绝进入 DENIED。

每次 dispatch 前重新核对授权、有效期、租约和参数摘要。ADMITTED 后授权被撤销且尚未执行，可以 DENIED/CANCELED；一旦可能到达远端就不能假装取消成功。

UNKNOWN_REMOTE 表示请求可能产生副作用但尚无足够证据。对账确认在执行则回 EXECUTING，确认完成则 VERIFYING，权威否定结果或已验证取消后才 FAILED/CANCELED。UNKNOWN_REMOTE 不允许直接“重新执行”。FAILED 也不等于“没有产生任何副作用”：已知的部分结果必须记录到 Evidence/Artifact；确认失败但部分变更已发生时，不得把整个 Action 作为无副作用操作盲重试。补偿或后续修复使用新 Action 并重新判定权限。

### 6.5 SUCCEEDED 的统一守卫

Run 成功必须同时满足：所需 Assignment 已验收、证据对当前产物版本有效、无未决 Action、无不明副作用、必要账目已结算或按明确政策标记可接受的观测缺口、没有未经处理的关键审查问题。任务通过不代表软件绝对无缺陷，而是满足该 Task 的明确验收约定。

## 7. 幂等、行为预写和崩溃恢复

### 7.1 幂等键

键的范围是 namespace + 已认证 principal + method + idempotency_key。摘要按 RFC 8785 JCS 序列化后做 SHA-256；摘要输入为 schema_version、method、params、expected_revision、expires_at，不含传输 request_id。[S11]

相同键、相同摘要：返回第一次 Operation。相同键、不同摘要：IDEMPOTENCY_CONFLICT。必须先做授权访问检查，再查询已受理幂等记录，之后才对新请求检查 expiry/CAS。这样一个成功请求的重发不会因为 revision 已前进而误报冲突，也不会因为重发自动创建新 Run。

幂等记录和必要 tombstone 的保存期必须覆盖命令重试窗口及关联 Action 保存期。不得清理仍有 UNKNOWN_REMOTE 的记录。客户端不得复用历史幂等键。幂等是 runner 的受理保证，不自动变成供应商的执行保证。

### 7.2 两段本地事务、一次外部调用

事务 A：验证命令与上下文；建立 Operation；写 Action 意图、参数与 action 摘要；记录策略结论；原子预留预算、使用次数和租约；写待执行 outbox；提交。

执行器：领取 outbox；再次验证期限、撤销和 fencing token；将“即将交付/已交付”审计状态持久化；调用适配器。外部网络调用不持有数据库事务。

事务 B：落地回执、Evidence、Action 状态和预算结算；更新业务状态；追加 Event；标记 outbox 已处理；提交。

授权摘要绑定主体、Action 内容、策略修订、Grant 和有效范围。`action_sha256` 绑定操作语义、参数、目标、资源版本、数据接收方、执行身份和任务约定，不包含每次派发可变化的 lease token。执行租约另外核验。

### 7.3 崩溃矩阵

| 崩溃位置 | 恢复规则 |
|---|---|
| 事务 A 之前 | 无已受理执行，可以重新提交 |
| A 已提交且确认从未交付 | 检查授权/预算后从同一 outbox 派发 |
| 交付时或交付后无回执 | 对账；未知则 UNKNOWN_REMOTE |
| 已有回执，事务 B 未完成 | 验证并幂等入账回执，不重复执行 |
| B 已提交，响应或事件丢失 | 返回原 Operation，重放已提交 Event |

进程在发出网络字节前后宕机存在不可消除的观测窗口。没有远端幂等键或权威查询时，不承诺跨系统 exactly-once。持久化日志不是跨系统事务，超时不是失败证据。

### 7.4 观测范围

只有经过 runner 网关或已验证的供应商执行前拦截器的动作，才能宣称执行前授权与预写。事后日志统一标为 observation，不补造事前审批记录。

后端内置工具若绕过 runner，必须由后端沙箱/权限配置限制。无法达到所需保证时，该后端不承接对应高风险动作；仍可在明示等级下用于低风险任务。用户有意授予宽权限也必须记录实际范围，不用提示词假装权限被收窄。

## 8. 租约、并发与唯一编排者

Run 具有 `control_owner` 和 epoch，ExecutionContext 可具有独立写租约。唯一编排者可以是 runner、外部宿主或人；不能让 Grok Bot 与 runner 同时对同一工作流进行重试、修复和阶段推进。

每次接管递增 fencing token/ownership epoch。失去租约的 Worker 不得开始新动作或提交权威状态。续租失败时停止派发，进入对账；新 Worker 必须先确认旧 Worker 的在途操作。

fencing 只对核验 token 的入口有效。它不会停止绕过 runner 的独立 Desktop 操作，也不会让不理解 token 的外部服务拒绝旧请求。实际并发写控制需要 OS/容器边界或受控资源代理。

v1 对同一写上下文采用单写者；并行实现分离 worktree/目录，集成由单一角色进行。worktree 分离版本修改，不替代进程、文件权限与网络沙箱。super.engineering 自身的协调锁也仅为 advisory，不能当成文件锁。[S8]

## 9. 权限、审批与 JIT 能力

### 9.1 四层判定

Capability 说明“有哪些操作”；Policy 说明“条件是否允许”；Grant 说明“谁在何种范围和时限内获准”；Authorization 是对当前 Action 的实际决策。发现已安装/已登录，不产生 Grant。

允许执行的判定必须包含：已认证主体匹配、Bot 初始化完成、能力已验证、资源范围匹配、数据接收方允许、策略通过、Grant 有效、使用次数足够、预算与租约有效、实际参数摘要未改变。

### 9.2 授权内容

Grant 包含 subject、Bot 实例、模板权限摘要、Task 范围、capability、资源 selector、接收方、到期时间、最大次数及必要的单次 Action 摘要。用户可以显式给项目/任务持续授权，降低重复确认；发布、付费、删除和系统特权采用单次或限期的窄授权。

路径由注册的资源解析器转换为实际路径，在执行点验证真实目标；不得只做字符串前缀比较。符号链接、工作目录变化、路径穿越及检查后替换都必须纳入控制。网络目标同样需验证实际目的地，不允许用通用 shell 权限规避限制。

### 9.3 可信审批

审批摘要由程序生成，显示真实动作、目标、修改/删除/发送范围、费用、数据去向和有效期。批准与 `action_sha256` 绑定。动作变化使旧审批失效。

LLM 可以发起 `approval.request`，不能生成批准证据；“用户说已授权”不是审批事件。可信 UI 根据用户配置可使用生物认证或成熟身份验证，但不把每个低风险操作强制变成手动点击。

### 9.4 三类可声明的控制等级

`runner_mediated`：执行入口和回执由 runner 控制；`provider_gated`：由已验证的原生权限/拦截接口实施；`audit_only/opaque`：仅有事后观测或黑盒结果。等级按操作声明，不给整个后端笼统贴上“安全”标签。

测试和构建会执行代码，不属于纯读操作。只读审查默认仅访问文本和既有报告；运行测试需要独立的执行授权与受限环境。

## 10. 预算与计费

Budget 是限制，Reservation 是并发预留，UsageObservation 是带来源、时间与单位的观测。它们不能互相替代。未知值使用 null/unknown，不使用 0。

单位分开处理：turns、tool_actions、tokens、wall_ms、usd_micros。供应商订阅余额属于独立 provider_quota 观测；不同账号/入口的额度不得未经核实相加，订阅百分比不折算为固定 API 美元。

对同一 budget、meter 和 pool 的准入条件为：已结算消耗 + 在途预留 + 本次预留不超过限制。该判定和预留必须在同一事务完成；完成后只记一次实际消耗并释放差额。结果未知的预留进入 UNCERTAIN，不能自动全部返还。

`hard` 仅用于具有可靠执行上界的维度，例如 runner 控制的派发次数；`admission_only` 限制新动作准入，但不能阻止已在运行的动作继续消耗；`soft_observed` 仅根据观测触发停止/告警，不保证绝不超额。

Token/金额若无法在调用前给出可靠上界或在执行中硬终止，不得标记 hard。订阅余量不可读取时可以按用户批准的有限轮次继续，也可以 WAITING/BUDGET_UNKNOWN；禁止静默转 API、充值或提高 Fast/推理档位。

v1 使用每 Task 一个 Budget，Assignment/Action 共享原子预留。先不实现跨 Task 自动预算转账。代码工作流先预留必要审查轮次，避免实施消耗完所有可用资源后无从验收。

## 11. 行为降级、领域状态与上下文胶囊

### 11.1 确定性路径

`executor_mode=deterministic` 必须指向注册 handler 和经过 Schema 校验的参数，不执行由 LLM 临时拼接的 shell。明确规则命中时，确定性调用、确定性字段映射和数据校验直接形成结果；LLM 只能解释或提出后续任务，不能回填或修改原始测量值。

自然语言允许先形成候选意图，但置信度不是授权。歧义时澄清或返回结构化需求；不能用近似匹配自动执行生产变更。

### 11.2 State 与 CAS

State 保存 decision、observation 或 verified_fact。CAS 防止旧版本覆盖，但不能证明内容正确；verified_fact 必须由受信任验证器关联有效证据。普通 Agent 的 `state.put` 只允许 domain.* 的 decision/observation，不允许直接设置 verified_fact。

更新时先核 ownership、读取 expected_revision；版本相符才同时写新值、摘要、revision 和 Event。冲突返回 REVISION_CONFLICT，不做最后写入者获胜。消息可以建议更新，只有显式、经过授权和校验的状态命令才能生效。

### 11.3 Capsule 内容与恢复

Capsule 内容包括目标、限制、决策及证据、资源快照、完成的步骤、未决问题、待审批动作和预算引用。它是恢复任务的上下文，不是供应商完整内存镜像。

恢复依次核对 Task contract_revision、配置摘要、资源哈希、State 版本、在途 Action、Grant 有效期与预算。审批没有因为出现在旧 Capsule 里就复活；预算不能从旧快照覆盖账本。

原生恢复可用时先续接原线程；失败时保留失败记录，创建新 Session 并加载已验证 Capsule。未知外部副作用未解决前，不允许新 Session重复相关动作。

### 11.4 文件系统即活索引

建议任务目录包含 manifest.json、task.md、context.md、artifacts/、observations/。manifest 携带 task_id、revision、last_event_sequence 和哈希，Markdown 从存储投影生成。Agent 可以提交新产物或状态提案，不能通过修改 task.md 改写系统状态。

内容先写暂存文件，完成持久化和原子定位后再提交 Artifact 元数据；异常留下的孤儿文件可回收。文件投影损坏时由状态库和产物引用重建，不能两边互相覆盖猜测。

## 12. 事件、回放与观测缺口

Event 只由可信服务在事务提交时产生。一个 `(namespace_id, task_id)` 事件流的 sequence 单调递增；不同任务之间不保证顺序。客户端按 event.id 去重，断线从最后已处理 sequence 继续。

至少一次投递意味着重复是合法的，消费者必须幂等。一个事务可追加多个 Event，更新状态与写事件必须同事务或使用等价的可靠 outbox。客户端收到事件后可读取实体快照获得当前视图。

保留窗口之外的游标返回 CURSOR_EXPIRED，并提供重新读取快照的流程；绝不能悄悄跳到最新而让客户端以为完整回放。上游没有历史事件时，记录 observation.gap，使用最终状态/历史对账；runner 事件回放不意味着能重建上游丢失的 token 流。

Token 级文本增量可以走有容量上限的临时流，不要求每个 token 持久化。状态、审批、派发、回执、证据和预算事件必须持久化。原始终端文字不是权威状态信号，供应商结构化事件也只能证明其相应层次的结果。

首版事件枚举 37 项见 `event-catalog.json`。EventData 为通用审计载荷，from/to 使用状态机校验；state.updated 关联的实体 revision 和 CAS 必须匹配。该 Schema 校验形状，业务发射权限由事件生产者和服务身份强制执行。

## 13. 后端适配接口与映射

接口见 `interfaces/backend-adapter.ts`。必需接口为 discover、health、createSession、submitTurn、observe、reconcile、nativeReference；resume、interrupt、approvals、close 和 usage 按能力可选。reconcile 可以诚实返回 UNKNOWN，不得为满足接口统一伪造成功。

MutationOutcome 区分 APPLIED、NOT_APPLIED 和 UNKNOWN。APPLIED 对 `submitTurn` 仅表示提交已经发生；业务完成仍由 turn 终态、产物和 runner 验收决定。

停止观察流与停止远端执行是两件事。AbortSignal 只取消订阅，不隐含中断 Agent。中断请求成功也不等于后台子进程全部结束；需要检查本次 Assignment 范围内的子进程与副作用。

后端状态逐项记录 documented/verified/unsupported/unknown。文档确认不等于当前安装验证。版本升级导致协议/能力变化时暂停依赖能力，重新探测；不能每次执行静默使用更新后的 flag。

具体命令、JSON flag 差异和限制见 `adapters/MAPPINGS.md`。

## 14. Grok Bot 初始化与人工接管

保留已有初始化范式：说明用途、选择范围、有限探测、可信授权、验证、原子保存、交付首个结果。BotInstance 区分 phase、setup_status 和逐后端健康，不能用一个 ready=true 代替全部判断。

即使模板未正确执行欢迎指令，run.start 也必须检查：必要配置已完成、manifest/config 修订匹配、权限有效、证据仍然新鲜、必需后端可用。复制模板不复制作者账户、授权、线程、已完成状态或定时任务。

Grok Bot 的多个 Bot 可共享用户级云端计算环境；Bot 名称不是安全隔离边界。[S5] runner 的实例授权仍只约束经过它的调用，宿主独立工具权限必须在宿主配置中处理。

人工接管先暂停后续派发，再请求中断或等轮次结束，保存检查点与原生引用；确认安全边界后将 ownership 转为 human。若原生客户端活动无法可靠侦测，明确要求先暂停 runner 再进入 Desktop，不能宣称内部锁能够锁住用户。

人类交回控制后先对账原生最新轮次、工作区快照、在途动作和目标变化；更新 Capsule、撤销过期验收，再恢复派发。

## 15. 首条代码流水线

任务约定形成后，由唯一编排者建立实施 Assignment。实施输出 patch/commit 和摘要；确定性验证器在获准的受限环境运行编译、类型和测试，产出绑定 revision 的 Evidence。

独立审查使用新会话，输入是需求、固定版本、差异和验证结果，不预先喂入实施者对自身正确性的长篇辩护。实现与审查使用不同供应商是可选策略，独立上下文与同一检查点为硬要求。

Finding 至少记录来源、版本、位置、严重性、证据和解决证据。修复后产物哈希变化，相关测试和审查需重新验证；不能把旧报告套到新提交。修复循环默认最多两轮，用户可配置，达到上限返回明确的阻塞和产物，不无限重试。

完成验收与 git push、PR 创建、部署分开。通过测试不会自动获得对外发布权限。首版可只交付本地产物，发布阶段保留独立 Grant。

## 16. 安全与实现约束

后端凭据引用与 runner 身份分开。通过安全存储引用获取凭据，不把 API key、Cookie、完整环境变量写入命令、Artifact、事件和聊天。远程连接使用加密与成熟认证；本机 IPC 也需合理权限与来源校验。

不可通过通用代理接受任意目标 URL、shell 或 SDK 方法名。适配器使用结构化参数和 argv，不拼接未受约束的 shell 字符串；动态命令必须是已注册能力且有单独边界。

Event 的 append-only 是应用级规则，不代表不可被存储管理员修改。需要防篡改的部署另行采用受控存储、签名/锚定或独立审计，不把普通数据库日志描述为不可抵赖证据。

密文/代码/日志交给哪个模型或 Bot 必须在授权数据去向中说明。只把密钥留在本地不意味着源码没有出机。Artifact hash 证明内容一致性，不证明内容可信或安全。

## 17. 首版实现顺序与交付判据

先固化当前 SDK—原生会话—Desktop 人工接管的基线，再加入 Task/Run/Session 身份与事务日志；不要先重写已工作的链路。

随后实现幂等受理、Action 预写、UNKNOWN_REMOTE 和同进程单调度器，做故障注入；再把 Grant/审批与 Setup 门禁接到真正的执行入口。

第一条工作流接入既有 Codex 适配器与 Grok Build；只读独立审查、有限修复、暂停恢复先闭环。super_sc 为后续可选后端，按安装版本验收后启用，不成为免费用户的强制依赖。

每个里程碑按 CONFORMANCE.md 验证。当前包的自动测试只覆盖结构与参考决策规则，TypeScript 仅做编译校验；真实适配器、网络断线、进程终止、权限隔离、Desktop 可见性与重启持久性仍需在实现环境进行验收。

## 18. 文件与来源

完整字段以 Schema 为准；TYPE-INDEX.md 提供字段索引，state-machines.json 提供允许边，event-catalog.json 提供事件和命令枚举。examples 是合成结构样例，不是预授权工作区或测试成功记录。

来源映射见 SOURCES.md。文档中 S1–S12 为本次核验的公开一手资料；设计选择、状态名、命令名和安全守卫均由本规范提出，不是对上游已有实现的宣称。
