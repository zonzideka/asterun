# 可选插件

[中文](plugins.md) | [English](en/plugins.md)

先在独立环境中安装插件，再注册到 Asterun。内置后端继续支持 v1 配置，独立插件使用 v2。Antigravity 账户、Cursor local Agent 和 Jules REST 当前为预览包，真实供应商调用和计费尚待验收。

## 配置与迁移

v2 中，`plugins` 保存安装注册，`provider_accounts` 保存供应商身份引用，`connections` 固定认证、环境和模型，`billing_pools` 描述共享资源池，`backends` 别名指向连接。可选 `quota` 段配置 Codex 额度探针、被动采集，以及默认关闭的 Grok 额度探针，字段和默认值见下文。可先运行[离线 fake 示例](../examples/plugins/fake-v2.json)。

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

## 候选组与选路解释

候选组只写在配置 v2 的 `routing.candidate_groups`。一组是同一角色、同一后端种类、同一执行选项的 1 到 8 个别名，模型可以不同。`models_allowed` 不写则不限制模型；写了才校验。`primary` 必须是成员之一。组名匹配 `^[a-z][a-z0-9_-]{0,31}$`。同一角色最多一组，不同角色的成员不能相交。未写 `candidate_groups` 或 `quota_preference` 时，配置回读不注入默认值。

角色与种类：`implementation` 只能是 grok，`review` 只能是 codex，`planning` 只能是 antigravity 或 claude，且同组种类必须相同。比较执行选项时忽略后端的 `name`、`home`、`bin`、`model`、`connection_ref`、`quota_epoch`、`extra_env`，以及连接 options 里的 `bin`、`home`、`model`。成员连接的计费池必须是 `subscription` 或 `local`，且不能 `paid_overage_allowed`。审查组不能包含实现组的后端或供应商账户，也不能包含与实现组同一家的模型：kind grok 为 xai，kind codex 或模型以 gpt、o 加数字开头为 openai，模型以 claude 开头或 kind claude 为 anthropic，模型以 gemini 开头为 google。

`routing.quota_preference` 默认关闭。本版即使 `enabled` 为 true，也只影响解释，不改变派发，也不放开 `automatic_account_switch`。默认视界 72 小时，浪费阈值 1000 bp，切换余量 500 bp，可信度至少 medium，短窗口不计入价值，短窗口地板 500 bp，长窗口地板 1000 bp。`count_reset_grants` 只能是 false。付费超额和重置券不计入可用量或浪费。

评分只读本地额度缓存。耗尽、闩锁或已用达到 10000 bp 的候选被排除。剩余低于地板加单次估计时降为 tier 3。只有距重置不超过视界、且时长至少 24 小时的窗口计入浪费；5 小时窗口默认只做门控。在途运行按周窗口 100 bp、5 小时窗口 500 bp 扣减。没有新鲜窗口时记为未知。全部未知、primary 未知或全部被闩锁时退回 primary。只有新鲜证据并且超出切换余量，才会建议离开 primary。

`routing-explain --group NAME` 或 `routing-explain --backend @NAME`（MCP `routing_explain`）对组成员做上述只读计算，返回建议，并带 `dispatch_changed: false` 与 `preference_enabled`。权限与 `quota-inspect` 相同。v1 配置或没有候选组时返回 `INVALID_CONFIG`。本版不自动切换账号，也不改变任务派发。

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

## 凭据与恢复

配置中使用凭据引用：`env:NAME`、`file:/absolute/path`、`native-home:/absolute/path` 或 `credential-ref:REFERENCE`。env/file 凭据须符合 manifest 认证声明；凭据文件为当前用户所有的私有单行普通文件，路径各级须可核验。原生账户使用明确的私有 HOME，不透明引用由部署方提供解析器。

worker 启动环境包含固定 PATH、临时 HOME/TMPDIR 及授权连接注入的凭据。`-m` 启动另加 `-I -S -B`，不加载 `site`。调用前重新核对 runner、manifest、wheel，以及将要执行的那份运行代码；协议帧、输出和运行时间均有上限。秘密仅在启动时进入内存环境，原生 HOME 内容单独管理与备份。

执行请求发出后，若发生断流、超时或格式异常，保留 `pending_reconcile` 和 uncertain 预算预留。继续按原任务对账，取消结果以远端终态为准。插件事件按供应商观察记录，用量报告保留 `provider_observed` 来源。

v2 记录同时绑定读取主体、连接与运行身份。恢复时保留核心状态、配置、原生 HOME 和插件私有状态；旧 v1 历史沿用原授权。数据库降级使用兼容快照副本，并保留新版运行记录。
