# 可选插件

[中文](plugins.md) | [English](en/plugins.md)

先在独立环境中安装插件，再注册到 Asterun。内置后端继续支持 v1 配置，独立插件使用 v2。Antigravity 账户、Cursor local Agent 和 Jules REST 当前为预览包，真实供应商调用和计费尚待验收。

## 配置与迁移

v2 中，`plugins` 保存安装注册，`provider_accounts` 保存供应商身份引用，`connections` 固定认证、环境和模型，`billing_pools` 描述共享资源池，`backends` 别名指向连接。可选 `quota` 段配置 Codex 额度探针、被动采集，以及默认关闭的 Grok 额度探针，字段和默认值见下文。可选 `context_budget` 段配置上下文胶囊与分段预算。`enforce`：Token 预算准入（详见下一 PR 的账本说明）。grok 分段见下文「grok 分段」。未写该键时配置摘要不变，字段见下文。可先运行[离线 fake 示例](../examples/plugins/fake-v2.json)。

查看 v1 迁移候选：

```sh
asterun --config old.json config-migrate
```

审阅候选与 `source_sha256` 后，写入全新配置和备份：

```sh
asterun --config old.json config-migrate \
  --output new.json --backup old.backup.json \
  --expected-source-sha256 REVIEWED_SOURCE_SHA256
```

源文件、目标文件和备份使用三个不同路径，目标与备份须尚不存在。迁移保留原配置，备份权限为 0600，revision 增一。真实连接迁移后先关闭，核对账户、环境和计费配置后再启用；fake 保持原开关。旧任务和会话保留原绑定。

内置兼容插件为 `asterun.fake`、`openai.codex`、`xai.grok`、`anthropic.claude` 和 `google.antigravity-cli`，复用原适配器及执行开关。

## 安装与注册

外部插件条目指定 `manifest_path`、`runner` argv、`installation_path` 及 SHA-256。`runner` 只允许两种形态：绝对路径解释器、`-m`、模块名，或单独一个绝对路径可执行文件。`-c`、其他解释器选项、相对路径，以及「解释器加脚本文件」都会在注册时拒绝。注册时必须同时给出 `runtime_path` 和 `runtime_sha256`，而且钉定的必须是将要执行或导入的那一份代码。`-m package.worker` 的 `runtime_path` 必须盖住该模块文件和它的包目录，可以是包目录，也可以是包含该包的安装树。单独的脚本则钉住该脚本或其父目录。入口只在这份钉定树里查找，注册不会为了探测导入路径去启动解释器，因此不会执行 `site`、`.pth` 或 `sitecustomize`。实际启动 `-m` 时使用 `-I -S -B`，只把已钉定的导入根放进 `sys.path`。只钉解释器、wheel 文件，或另一份不会被这次运行导入的副本，注册会被拒绝。未钉定默认拒绝。`allow_unpinned_runtime: true` 是显式开发开关，默认关闭，只放行配置加载，执行前仍会拒绝没有运行摘要的插件。调用前重新计算这份代码的摘要，并确认入口仍落在其中。wheel 的 `installation_sha256` 只钉发行文件，不能代替运行代码。采用独立 venv，安装版本改变后重新注册并准备计划。插件运行在当前 OS 用户权限下，应使用受信任代码。

`plugin-register --request registration.json` 接受 `plugin_id`、完整 `registration`、`output`、`backup` 和 `expected_source_sha256`。该命令备份原配置并生成候选。审阅后重启核心加载候选，再按[配置修订流程](install.md)应用。

各包准备方式见 [Antigravity](../packages/asterun-plugin-antigravity/README.md)、[Cursor](../packages/asterun-plugin-cursor/README.md) 和 [Jules](../packages/asterun-plugin-jules/README.md)。

## 使用与停用

`plugin-list`、`plugin-inspect PLUGIN_ID`、`connection-setup CONNECTION_REF` 提供静态检查和引导。`plugin-disable` 写入实例持久停用记录，阻止新动作；`plugin-enable` 解除同一安装身份的停用记录。配置本身仍须启用。

Agent 使用 `task-submit`。结构化工具能力使用 `capability-invoke --request invoke.json`：

```json
{
  "workspace": "demo",
  "backend": "external",
  "capability": "local.echo",
  "input": {"text": "hello"},
  "idempotency_key": "demo-1"
}
```

输入和输出按 manifest Schema 校验，分别保存在 `Task.capability_input` 和 `Run.output`。需要可审阅的固定调用计划或代码质量流程时，使用 [capability profile](capability-profile.md)。

CLI 与 MCP 共用请求定义，MCP 工具名使用下划线。`usage-inspect` 显示本地预算账本和已收到的供应商观察。`quota-inspect`（MCP `quota_inspect`）只读本地额度缓存，不查询上游。`quota-set` 手动录入额度，可信度为 low，最多 24 小时。`quota-clear` 只清除手动录入。`quota-refresh`（MCP `quota_refresh`）只读 Codex app-server `account/rateLimits/read`，最小间隔默认 60 秒，不建线程、不耗额度，不保存余额与邮箱。Codex 运行中收到 `account/rateLimits/updated` 时，运行结束写入额度缓存，来源 `passive_codex`，不保存余额、邮箱或套餐。v2 的 `quota` 段控制探针和这项被动采集：`probes.codex_app_server.enabled` 默认 true，`ttl_seconds` 默认 600（60–3600），`min_interval_seconds` 默认 60（60–3600），`timeout_seconds` 默认 10（1–30）；`passive.codex_rate_limits_updated` 默认 true。未知字段会被拒绝。`routing-explain`（MCP `routing_explain`）只解释候选组选路，不改变派发，详见下文。停用后，原主体仍可按原绑定读取、取消或对账已有任务，具体动作取决于插件能力。

Grok 额度读取走未公开 ACP 扩展 `_x.ai/billing`，默认关闭。需要在 v2 的 `quota.probes.grok_acp_billing` 同时设置 `enabled` 与 `acknowledge_undocumented`（二者默认都是 false）。探针只调用 `initialize` 和 `_x.ai/billing`，不建会话、不发提示。可信度最高为 medium。只记录已用比例、账期结束时间，以及是否开启按需付费，不保存金额。按需付费只读取布尔字段 `onDemandEnabled`，不读取金额、上限、已用量或未知字段；该字段为 false 时，即使上限大于 0 也不记为开启。`billingPeriodEnd` 可以缺省；一旦给出，过期或非法则整次解析失败，不产生观测。`ttl_seconds` 默认 900（60–3600），`min_interval_seconds` 默认 300（60–3600），`timeout_seconds` 默认 10（1–30）。v1 配置不能启用该探针。探针错误码只有三种：`AUTH_REQUIRED`（认证或登录失败）、`PROBE_TIMEOUT`（调用超时）、`SCHEMA_MISMATCH`（二进制缺失、连接关闭、进程失败，或响应无法按账单结构解析，含账期结束时间过期或非法）。不返回 `LIVE_DISABLED` 或 `PROBE_FAILED`。未设置 `ASTERUN_RUN_GROK=1` 时，服务层记 `skipped=disabled` 且不调用探针；两个开关未同时打开时，服务层记 `PROBE_DISABLED`。这两种禁用都不是探针错误码。

持久的额度闩锁来自已保存的原生失败分类，不保留错误原文。Grok 的 HTTP 402 额度耗尽会停止同一计费池的后续派发。Antigravity 只在严格匹配的 429 额度耗尽时闩锁：文本含独立 token `429`（`\b429\b`）或 `RESOURCE_EXHAUSTED`，并且含 quota（不分大小写）；单纯限流不闩锁。Antigravity 按额度桶隔离，Gemini 为一组，Claude 与 GPT 为一组。解除闩锁时，v1 修改该后端的 `quota_epoch`，v2 更新计费窗口。`quota-inspect` 在对应 scope 的窗口最前显示 freshness 为 `latched` 的窗口；该窗口只展示，不写入额度缓存。

## 上下文预算（context_budget）

v2 可以写可选命名空间 `context_budget`，用来声明上下文胶囊和分段预算。它与付费、`routing`、`quota` 没有字段关联，也不改变这三处的校验。未写此键时，回读不注入默认值，`to_dict` 和配置摘要与未写时逐字节相同。只有写了此键，序列化里才出现 `context_budget`；写出的胶囊、分段和分配字段会补上默认值。`profiles` 里省掉的阈值不补，表示没有该阈值。v1 没有这一段，读取结果是 `mode=off` 的默认值。

`mode` 接受 `off`（默认）、`observe` 和 `enforce`。`enforce`：Token 预算准入（admission_only，见下文"Token 预算账本"），以及白名单内的 grok 分段（见下文"grok 分段"）。`mode=enforce` 时至少一个 profile 须含 `task_hard`，否则拒绝。其它取值拒绝。`token_basis` 只接受 `processed`（默认）。

`capsule.nonmandatory_soft_bytes` 为 1024–65536，默认 16384。`capsule.nonmandatory_hard_bytes` 为 1024–131072，默认 24576，且必须大于等于 soft。`capsule.notes_max_bytes` 为 0–16384，默认 4096。`capsule.progress_markers` 为布尔值，默认 true。

`segmentation.backends` 是已配置后端名的列表，默认 `[]`。`segmentation.roles` 是 `implementation` 与 `repair` 的子集，默认两者都在。`segmentation.min_long_task_processed` 为 0–1000000000，默认 600000。`segmentation.resume_max_residency` 为 0–100000000，默认 100000。`segmentation.observe_capsule_min_processed` 为 0–1000000000，默认 600000。

`ab_assignment.enabled` 为布尔值，默认 false。`ab_assignment.enforce_ratio_bp` 为 0–10000，默认 5000。本版不执行 enforce，这个比例只是配置。

`profiles` 的键格式是 `后端/角色`。后端必须已经写在 `backends` 里。角色只能是 `implementation`、`repair`、`review` 或 `planning`。可写字段都可省略：`segment_turns` 1–1000，`segment_soft`、`residency_soft`、`task_soft`、`task_hard` 各为 1–10000000000，`max_segments` 1–100，`segment` 为布尔值。给出的阈值必须满足 `task_hard ≥ task_soft ≥ segment_soft`。`review` 不允许 `segment=true`。

未知字段会被拒绝。名为 `enforcement` 的字段，以及值 `hard`，一律拒绝。布尔值不能当作整数。数值都必须是整数。

observe 不改变派发、返回或计数；enforce 同样记录观测，另外执行预算准入。

### Token 预算账本（enforce）

任务首次在 enforce 下派发、且匹配 profile 有 `task_hard` 时建立任务 Budget，上限按首次快照，之后改配置不改已有上限；之后该任务所有 run 都受其约束。每次派发在本地校验之后预留 `segment_soft`（无则剩余额度，至少 1，不超过上限）；已结算+在途+本次 > 上限时拒绝派发，错误码 `BUDGET_EXHAUSTED`，并为上一个终态 run 生成 `budget_soft` 胶囊；需显式加额（本版无加额命令）。终态按 processed 结算；用量不可归属时转 `UNCERTAIN` 并按预留额继续占用。不使用付费额度、不切换后端或角色。`task-get` 与 `context-budget-inspect --task` 在 enforce 下显示 budget（`task_hard`/`settled`/`held`/`uncertain_reservations`）。结算由运行是否已有预留决定，与终态时的 mode 无关（off 下终态只做一次只读查询）。

### grok 分段（enforce，白名单）

只在 `mode=enforce`、后端列在 `segmentation.backends`、角色在 `segmentation.roles`、匹配 profile 有 `segment_turns` 且 `segment` 不为 false 时生效。本版实现 grok（fake 仅用于离线测试），审查角色永不分段。质量流程、修复目标绑定、read_scope、capability、dependencies、runner-control 持有的任务不分段。每段派发原生 `--max-turns` 为 `min(segment_turns, 后端 max_turns)`。

段以原生回合上限结束（源 run 仍记 failed/`GROK_RUN_INCOMPLETE`）时：未达 `max_segments`、未达 `scheduler.max_runs_per_task`、预算有余、胶囊 ready 才提交后继段（幂等键 `seg:{task}:{source_run}:{index}`）。后继用胶囊加续段说明，默认新会话（只有处理量与驻留量都已知且低于阈值时才 resume）。`run_count` 递增，`repair_count` 不变。续段计入 `max_runs_per_task`。源 run 记 `native.segment_end`。

停下的原因记在 `segment_end.reason`（`max_segments` / `max_runs` / `budget` / `capsule_not_ready` / `capsule_too_large` / `segmentation_disabled` / `control_owned`）。`max_segments` 与 `budget` 另生成 `budget_soft` 胶囊。续段派发前再核对：分段仍启用、源 run 已终止且指向本段、任务当前 run、胶囊 id 与 mandatory 未变，且 `run_count` 不超过 `max_runs_per_task`。分段已停用，或任务由 runner-control 持有，则不派发，错误码 `CAPABILITY_UNSUPPORTED`；源 run、当前 run、胶囊 id 或 mandatory 已变化则不派发，错误码 `REVISION_CONFLICT`；续段超过每任务运行上限则不派发，错误码 `BUDGET_EXHAUSTED`。

## 上下文胶囊（v1，observe 仅生成不派发）

`capsule_candidate` 为真时，观测行写入之后再生成一条续段胶囊，`trigger` 为 `observe_only`。`capsule.progress_markers` 是真是假都不影响这一步。胶囊只落库：`ready` 写 0，不派发，不改运行结果，不改计数。生成失败只丢掉这条胶囊，观测行和 `save_run` 照常完成。`mode=off` 时不写数据库；终态只读查询是否有历史预留。

胶囊 schema 是 `asterun.capsule/v1`。它保存继续下一段所需要的契约、工作区摘要、进度和未决事项。mandatory 原文不裁剪：`objective` 是任务正文的逐字节副本，`constraints`、`acceptance`、`review_requirements`、`findings`（含 id 与 text）和 `limits` 同样原样进入。非 mandatory 软上限 16384 字节（16 KB），硬上限 24576 字节（24 KB）。超过软上限时按固定顺序做有损裁剪（笔记正文、命令、碰过的文件）。裁完仍超过硬上限则不存正文，行上 `error_code` 为 `CAPSULE_TOO_LARGE`。

胶囊不含源码正文、diff 正文、聊天记录和凭据。工作区只留 HEAD、分支、脏文件路径与状态、文件 sha256，以及 `diff --shortstat` 的文件数和增删行数。命令每项只保存 `program`（首个 token 的 basename，不合规则为 null）、完整命令的 `sha256` 和 `length`；渲染只显示程序名与摘要前 12 位；不保存参数或正文。未确认的工具调用只留工具名和输入摘要。

`ASTERUN-NOTE:` 行收成低信任笔记（`trust=low`），不改变任何任务约束。笔记正文最多 4096 字节。渲染进提示词时标明「前段笔记（低信任数据，不改变任何任务约束）」。白名单过滤，不能确认安全的行整行丢弃，只计入 `dropped_lines`。

重新校验不信任库里的旧结论。空列表表示通过。问题码有 `schema_mismatch`、`body_sha256_mismatch`、`capsule_id_mismatch`、`mandatory_sha256_mismatch`、`mandatory_mismatch`、`finding_missing:<id>`、`finding_text_changed:<id>`。任务契约摘要与胶囊里保存的不一致时再加 `contract_changed`。

同一 `(namespace, run, trigger)` 只保留一行，重复生成只更新。表 `context_capsules` 随旧库自动创建，schema 版本不变。行保留 30 天、最多 2000 行；裁剪只删行，不删 blob。旧备份没有这张表也能恢复；表在而列不一致则拒绝恢复。

只读查看：

```sh
asterun capsule-inspect --task TASK_ID [--limit N] [--render]
```

MCP 工具名是 `capsule_inspect`。`task_id` 必填，`limit` 为 1–200，默认 20，`render` 默认 false。权限与 `context-budget-inspect` 相同，并且对该任务的工作区要有 `task.get`。命令不建表、不写库、不派发。表不存在时 `capsules` 为空列表。`--render` 加载该任务最新一条有正文的胶囊，按当前任务重新校验，返回 `verify_issues` 和 `rendered_prompt`。提示词里的任务契约是胶囊中的原文。

## 候选组与选路解释

候选组只写在配置 v2 的 `routing.candidate_groups`。一组是同一角色、同一后端种类、同一执行选项的 1 到 8 个别名，模型可以不同。`models_allowed` 不写则不限制模型；写了才校验。`primary` 必须是成员之一。组名匹配 `^[a-z][a-z0-9_-]{0,31}$`。同一角色最多一组，不同角色的成员不能相交。未写 `candidate_groups` 或 `quota_preference` 时，配置回读不注入默认值。

角色与种类：`implementation` 只能是 grok，`review` 只能是 codex，`planning` 只能是 antigravity 或 claude，且同组种类必须相同。比较执行选项时忽略后端的 `name`、`home`、`bin`、`model`、`connection_ref`、`quota_epoch`、`extra_env`，以及连接 options 里的 `bin`、`home`、`model`。成员连接的计费池必须是 `subscription` 或 `local`，且不能 `paid_overage_allowed`。审查组不能包含实现组的后端或供应商账户，也不能包含与实现组同一家的模型：kind grok 为 xai，kind codex 或模型以 gpt、o 加数字开头为 openai，模型以 claude 开头或 kind claude 为 anthropic，模型以 gemini 开头为 google。

`routing.quota_preference.mode` 默认 `off`，与以前完全相同。`enabled` 为 false 等价于 `off`。`dry_run` 照常派发 primary，只记录 `would_choose`；`on` 派发选中者。详见下文「额度优选（先演练后生效）」。默认视界 72 小时，浪费阈值 1000 bp，切换余量 500 bp，可信度至少 medium，短窗口不计入价值，短窗口地板 500 bp，长窗口地板 1000 bp。`count_reset_grants` 只能是 false。付费超额和重置券不计入可用量或浪费。

评分只读本地额度缓存。耗尽、闩锁或已用达到 10000 bp 的候选被排除。剩余低于地板加单次估计时降为 tier 3。只有距重置不超过视界、且时长至少 24 小时的窗口计入浪费；5 小时窗口默认只做门控。在途运行按周窗口 100 bp、5 小时窗口 500 bp 扣减。没有新鲜窗口时记为未知。全部未知、primary 未知或全部被闩锁时退回 primary。只有新鲜证据并且超出切换余量，才会建议离开 primary。

`routing-explain --group NAME` 或 `routing-explain --backend @NAME`（MCP `routing_explain`）对组成员做上述只读计算，返回建议，并带 `dispatch_changed: false` 与 `preference_enabled`。权限与 `quota-inspect` 相同。v1 配置或没有候选组时返回 `INVALID_CONFIG`。该命令只解释、不派发。任务是否派发选中者由 `mode` 决定。

```json
{
  "routing": {
    "candidate_groups": {
      "implementer": {"role": "implementation", "members": ["grok-a", "grok-b"], "primary": "grok-a"},
      "reviewer": {"role": "review", "members": ["codex-a"], "primary": "codex-a"},
      "planning": {"role": "planning", "members": ["agy-opus", "agy-gemini"], "primary": "agy-opus"}
    },
    "quota_preference": {"enabled": false}
  }
}
```

`agy-opus` 与 `agy-gemini` 都是 antigravity，执行选项相同，模型分别是 Claude 与 Gemini。

### 额度优选（先演练后生效）

`routing.quota_preference.mode` 取 `off`、`dry_run` 或 `on`。`off` 是默认，与以前完全相同。`enabled` 为 false 等价于 `off`。`dry_run` 是演练：照常派发 primary，只记录 `would_choose`。`on` 派发选中者。

`on` 须同时满足：`automatic_account_switch` 为 true，候选组非空且全部通过校验，`rules` 为空。`automatic_paid_fallback` 永远为 false。`automatic_account_switch` 为 true 而 `mode` 不是 `on` 时，配置会被拒绝。

入口是 `backend` 写成 `@组名`，或省略 `backend` 且 `default_backend` 为 `@组名`。显式写普通后端名不参与优选。组不存在返回 `INVALID_REQUEST`。v1 配置写 `@…` 返回 `INVALID_CONFIG`。`workflow.review_backend` 本版不支持候选组别名，质量审查仍用固定后端；`default_backend` 可写 `@组名`（仅 v2）。

显式后端付费保护：该后端有新鲜、可信度不低于 medium 的窗口显示订阅额度已耗尽，且同一 scope 存在付费超额条目时，默认以 `BUDGET_EXHAUSTED` 阻断。`routing.quota_preference.allow_paid_on_explicit` 设为 true 可关闭，默认 false。数据未知或过期不阻断。

续接与修复轮次沿用首次选定的后端。被选者准入失败直接报错，不自动换下一个。数据异常时退回 primary，`reason_code` 为 `quota_state_invalid`。

决策写入 `plugin_routing_decisions`，最多 1000 行、保留 30 天，只存分数摘要，不存凭据或余额。走了上述入口时，`task-get` 在任务级显示 `routing`：`mode`、`group`、`chosen`、`primary`、`reason_code`、`would_choose`。未走优选时不增加该键。

先用 `dry_run` 观察 `routing.would_choose`，再把 `mode` 改为 `on`。付费超额永不自动使用，重置券永不自动兑换。

## 凭据与恢复

配置中使用凭据引用：`env:NAME`、`file:/absolute/path`、`native-home:/absolute/path` 或 `credential-ref:REFERENCE`。env/file 凭据须符合 manifest 认证声明；凭据文件为当前用户所有的私有单行普通文件，路径各级须可核验。原生账户使用明确的私有 HOME，不透明引用由部署方提供解析器。

worker 启动环境包含固定 PATH、临时 HOME/TMPDIR 及授权连接注入的凭据。`-m` 启动另加 `-I -S -B`，不加载 `site`。调用前重新核对 runner、manifest、wheel，以及将要执行的那份运行代码；协议帧、输出和运行时间均有上限。秘密仅在启动时进入内存环境，原生 HOME 内容单独管理与备份。

执行请求发出后，若发生断流、超时或格式异常，保留 `pending_reconcile` 和 uncertain 预算预留。继续按原任务对账，取消结果以远端终态为准。插件事件按供应商观察记录，用量报告保留 `provider_observed` 来源。

v2 记录同时绑定读取主体、连接与运行身份。恢复时保留核心状态、配置、原生 HOME 和插件私有状态；旧 v1 历史沿用原授权。数据库降级使用兼容快照副本，并保留新版运行记录。
