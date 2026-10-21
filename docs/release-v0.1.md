[中文](release-v0.1.md) | [English](en/release-v0.1.md)

# 首版发行说明

当前公开源码版本为 `0.1.0a27`，正式稳定版 `0.1.0` 尚未发布。本次预发布的安装资产和校验文件见[对应版本发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a27)。首版范围已冻结，发布准备集中在现有行为、兼容和打包修复。项目采用 [Apache-2.0](../LICENSE)。`v0.1.0a27` 标签尚未打出，发行页上的文件要等该标签进入 `main` 且发行工作流成功之后才会出现。

## 0.1.0a27

相对 2026-10-21 已发布的 [`v0.1.0a26`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a26)，本版是上下文胶囊与 Token 预算第二阶段（默认关闭）。`context_budget.mode` 新增 `enforce`：任务级 Token 预算准入（`admission_only`），首次在 enforce 下派发且匹配 profile 含 `task_hard` 时建立 Budget、上限按首次快照，超限 `BUDGET_EXHAUSTED` 并生成 `budget_soft` 胶囊，终态按 processed 结算、未知转 `UNCERTAIN`；enforce 需至少一个带 `task_hard` 的 profile。白名单内分段（`segmentation.backends`）：grok（`workspace-code-v1`）以原生回合上限分段；codex 仅实现角色、agy 仅 profile `segment: true`，在 run 成功、回复末行为 `ASTERUN-SEGMENT: CONTINUE`、无子代理、用量可归属且本段 processed ≥ `segment_soft` 时触发（外部 Antigravity 插件结果不保留 `subagent_count`，目前不会自动续段；完整条件见[插件文档](plugins.md)），新线程 + 胶囊续段（native_compact 不进自动流程）；审查、质量流程、外部强制审查等任务永不分段。续段幂等（`seg:{task}:{run}:{idx}`），计入 `max_runs_per_task`，重启与人工交接 resume 会对未判定的终态段重新判定。grok 续段仅在处理量与驻留峰值都已知且低于阈值时 resume 原会话。备份校验预算预留引用的 run 存在且同任务。默认 `off`，未写此键的既有配置序列化与摘要不变；schema 版本仍为 6。插件版本不变（Antigravity `1.0.7`、Cursor `1.0.1`、Jules `1.0.0`）；协议与标准 skill 最低接口 `0.1.0a14` 不变。

尚未打 `v0.1.0a27`。资产要等该标签进入 `main` 且发行工作流成功之后，才会出现在[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a27)。本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a27.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.7`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a26

相对 2026-10-20 已发布的 [`v0.1.0a25`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a25)，本版是上下文胶囊与 Token 预算第一阶段（纯观测，默认关闭）。usage-report 每行与汇总新增 `breakdown`：统一未缓存输入、缓存读、缓存写、输出四分量，processed（含缓存读）与 new（不含缓存读）两种口径；缺失记 null 不记 0；用 total 做包含关系自检，不符判 unknown；Codex 线程累计不归属到运行；Codex cacheWrite 不计入（partial）；新增 Grok 逐请求驻留曲线（按 message_id 去重）。v2 新命名空间 `context_budget` 只接受 off（默认）/observe，enforce 尚未实现会被拒绝；未写此键的既有配置序列化与摘要不变。observe 模式在运行终态把本 run 与任务累计 processed/new 及本该触发的标记（`segment_soft_exceeded`、`task_soft_exceeded`、`task_hard_exceeded`、`max_segments_exceeded`、`long_task`、`would_segment`、`capsule_candidate`）写入新表 `context_budget_observations`；task-get 在有记录时显示 `context_budget`；新只读命令 `context-budget-inspect`（MCP `context_budget_inspect`）。不改变任何派发、返回或计数。SQLite 新增观测表（旧库自动创建，旧备份仍可恢复），schema 版本仍为 6。插件版本不变（Antigravity `1.0.7`、Cursor `1.0.1`、Jules `1.0.0`）；协议与标准 skill 最低接口 `0.1.0a14` 不变。

`v0.1.0a26` 已于 2026-10-21 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a26)：核心 wheel、源码包、`asterun-skill-0.1.0a26.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.7`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a25

相对 2026-10-20 已发布的 [`v0.1.0a24`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a24)，本版完成额度方案 PR3–PR6。Codex 额度读取按真实结构解析 credits（只记有/无）与重置券（只展示、不计入、不兑换）；最紧约束不再选中过期或已翻转的窗口。可选的 Grok `_x.ai/billing` 额度探针默认关闭：v2 需同时打开 `enabled` 与 `acknowledge_undocumented`，v1 不能启用；只记已用比例、账期结束、是否开启按需付费。通用额度闩锁在 Grok 402 之外，对 Antigravity 严格匹配的 429 额度耗尽也会闩锁（单纯限流不闩锁），按 Gemini / Claude-GPT 额度桶隔离；`quota-inspect` 显示 `latched`；Antigravity 可写 `quota_epoch`。v2 候选组为 `routing.candidate_groups`，并提供只读的 `routing-explain`；候选组可跨模型，审查组不得包含写代码一方的后端、账号或同一家的模型。额度优选 `routing.quota_preference.mode` 取 `off`（默认，与 a24 相同）、`dry_run`（演练，只记录 `would_choose`）或 `on`（需 `automatic_account_switch=true`、候选组有效、`rules` 为空）；付费超额永不自动使用；显式指定后端且新鲜数据显示订阅窗口已耗尽、会转用付费额度时默认阻断（`allow_paid_on_explicit` 可关闭，默认 false）；决策记录在 `plugin_routing_decisions`，`task-get` 显示 `routing`。SQLite 新增决策表（旧库自动创建，旧备份仍可恢复），schema 版本仍为 6；不写新配置键的既有配置序列化与摘要不变。Antigravity 插件升到 `1.0.7`（vendored runtime 同步 429 分类）；Cursor `1.0.1`、Jules `1.0.0` 不变。协议与标准 skill 最低接口 `0.1.0a14` 不变。

`v0.1.0a25` 已于 2026-10-20 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a25)：核心 wheel、源码包、`asterun-skill-0.1.0a25.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.7`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a24

相对 2026-10-19 已发布的 [`v0.1.0a23`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a23)，本版加入额度观测（额度方案 PR1+PR2，不改调度）。新增本地额度缓存与 `quota-inspect` / `quota-set` / `quota-clear` / `quota-refresh`（MCP 同名下划线）：inspect 只读，不写库、不查上游；手动录入可信度为 low、最长 24 小时、不能创建闩锁，clear 只删手动录入；这些命令需要 SQLite 状态目录。Codex 的 `quota-refresh` 只调用 app-server `account/rateLimits/read`（initialize + 读取 + 关闭，不调用 `account/read`、`thread/*`、`turn/*`，不耗额度），同一 scope 最小间隔 60 秒、租约单飞；运行中收到 `account/rateLimits/updated` 时脱敏，并在运行结束写入缓存。只记已用比例、重置时间、是否有付费 credits（不存余额）、重置券到期时间（只展示、不计入、不兑换）；不保存邮箱和套餐。v2 配置新增可选 `quota` 段（`probes.codex_app_server`、`passive.codex_rate_limits_updated`）；不写该段的既有配置序列化与摘要不变。SQLite 新增两张额度表（旧库自动创建，旧备份仍可恢复），schema 版本仍为 6。额度数据暂不参与选路或准入，数据缺失时行为与 a23 相同。协议、配置 schema 版本、标准 skill 最低接口 `0.1.0a14` 不变；插件版本不变（Antigravity `1.0.6`，Cursor `1.0.1`，Jules `1.0.0`）。

`v0.1.0a24` 已于 2026-10-20 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a24)：核心 wheel、源码包、`asterun-skill-0.1.0a24.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.6`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a23

相对 2026-10-19 已发布的 [`v0.1.0a22`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a22)，本版修复 agy 1.4 派出子代理后不返回 result、任务跑满超时进入待对账并占住并发槽位的问题。内置 Antigravity 后端在看到第一个子代理信号（`invoke_subagent` 工具步骤，或被改报为 `subagent` 的步骤）时立即结束整个进程组，不再等待 result；确认进程和进程组都已退出后记为 failed、`terminated=true`、`ANTIGRAVITY_RUN_INCOMPLETE`（`failure_type=subagent_delegated`），并立即释放并发槽位；无法确认退出时保持待对账（`subagent_process_alive`），不误报终态。a22 中“收到 result 后进程仍不退出”的情况并入同一逻辑。确认退出后不再对原进程组号发信号，以免误伤被复用的组号。agy 1.4.2 没有关闭子代理的参数，本版只改代码。外部 Antigravity 插件升到 `1.0.6`：插件里 agy 不在独立进程组，无法确认整组退出，看到子代理后结束进程但保持待对账。已用真实 agy 1.4.2 复测：诱发子代理的列目录任务在看到子代理帧后立即以未完成结束，无残留进程。协议、SQLite schema 6、配置 schema 与标准 skill 最低接口 `0.1.0a14` 不变；Cursor 插件仍为 `1.0.1`，Jules 插件仍为 `1.0.0`。

`v0.1.0a23` 已于 2026-10-19 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a23)：核心 wheel、源码包、`asterun-skill-0.1.0a23.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.6`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a22

相对 2026-10-19 已发布的 [`v0.1.0a21`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a21)，本版让客户端默认跟随最新版，不再由 Asterun 关闭自动更新。Antigravity：子进程与版本探测都不再设置 `AGY_CLI_DISABLE_AUTO_UPDATE`，agy 可放在可写目录自行升级；每次启动按可执行文件身份（dev、inode、大小、mtime）重新探测，探测前后身份不一致就重探（最多 3 次，否则 `BACKEND_UNAVAILABLE`），运行证据记录 `cli_version`，探测之后二进制被替换（启动前或运行中）记 `cli_updated_during_run=true`，结果照常判定。agy 1.4 会把 `invoke_subagent` 工具步骤改报为 `subagent`：只接受原步骤与新帧都为 `invoke_subagent` 的这一种变化，工具步骤中途改名判 `step_tool_changed`；出现子代理委派时，收到 result 即停止并清理进程组，记为 `ANTIGRAVITY_RUN_INCOMPLETE`（`subagent_count`），不再等子代理跑满超时。Grok：新目录 `config.toml` 写 `[cli] auto_update = true`，校验时 `auto_update` 由操作者决定（须为布尔值），其他键仍须完全一致；不再导出 `GROK_DISABLE_AUTOUPDATER`。外部 Antigravity 插件升到 `1.0.5`（vendored profile/runtime 同步）。已用真实 agy 1.4.2 跑通只读读文件任务，并确认诱发子代理的任务按未完成结束且无残留进程。协议、SQLite schema 6、配置 schema 与标准 skill 最低接口 `0.1.0a14` 不变；Cursor 插件仍为 `1.0.1`，Jules 插件仍为 `1.0.0`。

`v0.1.0a22` 已于 2026-10-19 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a22)：核心 wheel、源码包、`asterun-skill-0.1.0a22.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.5`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a21

相对 2026-10-14 已发布的 [`v0.1.0a20`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a20)，本版去掉对客户端版本的硬性限定。内置 Antigravity 后端不再绑定 agy CLI 1.2.0：派发前在临时 HOME 中运行 `agy --version` 与 `agy --help`（仅在后端启用且 `ASTERUN_RUN_ANTIGRAVITY=1` 时；输出最多 64 KiB，15 秒超时并结束进程组），要求版本不低于 1.2.0 且具备 `--input-format`、`--output-format`、`--model`、`--disable-slash-commands`、`--print-timeout`、`--add-dir`，否则给出明确错误；`backend-inspect` 新增 `cli_version`。profile 不再按 1.2.0 哈希清单核对内置技能、按平台钉定 `webm_encoder`、固定 Playwright 缓存 `1.57.0`，改为结构化检查（内置技能只允许本人所有、不可执行、不超过 128 KiB 的 `.md` 及 CLI 的 `.checksum`/`keep.txt`；encoder 为他人不可写的普通文件，不超过 64 MiB；缓存允许任意 `x.y.z` 空目录）；字节级固定改为可选，通过 `ASTERUN_ANTIGRAVITY_PINS` 指向 JSON 清单。接受新 CLI 的 `jetbox_summaries_proto.pb` 与空的 `mcp_config.json`。子进程环境加入 `AGY_CLI_DISABLE_AUTO_UPDATE=1`；实测 1.3.2 的 `stream-json` 模式不认该变量，agy 须放在不可写目录。外部 Antigravity 插件升到 `1.0.4`：worker 要求 CLI 不低于 1.2.0 并检查同一组参数（`upstream_feature_missing`），新增可选 `pins` 选项。Cursor 插件升到 `1.0.1`：依赖改为 `cursor-sdk>=1.0.31,<2`，并在启动 bridge 前检查所用 SDK 接口（`sdk_feature_missing`）。已用真实 agy 1.3.2（Linux x86_64）跑通只读任务。协议、SQLite schema 6、配置 schema 与标准 skill 最低接口 `0.1.0a14` 不变；Jules 插件仍为 `1.0.0`。

`v0.1.0a21` 已于 2026-10-19 发布，资产见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a21)：核心 wheel、源码包、`asterun-skill-0.1.0a21.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.4`，Cursor `1.0.1`，Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a20

相对 2026-10-11 已发布的 [`v0.1.0a19`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a19)，本版把调度容量改动不再作废既有任务、同一次启动内回收进程已消失的 grok-code 运行，以及操作者终止路径标成 `0.1.0a20`。协议、SQLite schema 6、配置 schema、插件版本与标准 skill 最低接口 `0.1.0a14` 不变。插件仍为 Antigravity `1.0.3`、Cursor 与 Jules `1.0.0`，与 a19 相同。技能包摘要只因发行页链接改为 a20 而不同。

只改调度器容量（`max_queue`、`per_backend_concurrency`、`max_concurrency`、`local_check_concurrency`；`max_runs_per_task` 仍计入）不再使既有任务失效。每个任务保存 `execution_digest`；没有这份摘要的旧任务保持不等价。`task-cancel` 与 `task-reconcile` 接受旧修订上的任务。grok-code 在同一次启动内回收进程已被证明消失的运行：记录 `pid_ns`，并用环境标记 `ASTERUN_LAUNCH_MARKER` 扫描 leader、进程组与标记。无法读取的同 uid 条目按失败关闭记为未知，不会当成已消失。消失、僵尸或空 environ 的条目触发有界重扫，最多 3 次；三次都不稳定则仍为未知。限制：后代进程清掉自身环境并离开进程组，或更换 uid 后，不会被看到。操作者路径为 `task-reconcile <id> --terminate --reason ... [--confirm-process-gone]`。

尚未打 `v0.1.0a20`。资产要等该标签进入 `main` 且发行工作流成功之后，才会出现在[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a20)。本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a20.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.3`，Cursor 与 Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a19

相对 2026-10-09 已发布的 [`v0.1.0a18`](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a18)，本版把 grok-code 的默认回合与宿主重启后的进程丢失处理标成 `0.1.0a19`。默认 `max_turns` 从 12 改为 40，范围仍为 1 到 100，超时默认不变。协议、SQLite schema 6 和配置 schema v1/v2 不变。插件仍为 Antigravity `1.0.3`、Cursor 与 Jules `1.0.0`，与 a18 相同。标准 skill 最低接口仍为 `0.1.0a14`；技能包摘要只因发行页链接改为 a19 而不同。

宿主重启后进程已被证明消失的 grok-code 运行（boot id 变化，同一 `/etc/machine-id`）关闭为失败 `GROK_PROCESS_LOST`；若当时已请求取消，则记为已取消。关闭发生在启动时、`task.reconcile` 或 `task.cancel`，并释放名额，且不再重派。同一次启动内丢失或证据不足仍为 `pending_reconcile`。宿主身份在进程启动前写入后台核心与同步核心。原生权限拒绝（`permission_cancelled`）记为失败 `GROK_PERMISSION_DENIED`，不再记为已取消；权限模式仍为 `dontAsk`。`ControlRuntime` 终结器在其他线程被垃圾回收时不再抛出异常。运行尚未结束时，把 Asterun 状态目录移动或共享到克隆主机（machine id 相同）不受支持，可能把仍在活动的运行误判为 `GROK_PROCESS_LOST`。旧版本留下、没有宿主身份记录的待处理运行仍需显式路径，并保持未知。

尚未打 `v0.1.0a19`。资产要等该标签进入 `main` 且发行工作流成功之后，才会出现在[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a19)。本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a19.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.3`，Cursor 与 Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a18

相对已发布的 a17，本版把 main 上的写盘事务、环境与插件钉定、自动发版流水线标成 `0.1.0a18`。协议、SQLite schema 6 和配置 schema v1/v2 不变。标准 skill 的 `SKILL.md`、脚本和代理配置与 a17 相同，最低接口仍为 `0.1.0a14`；仅 `references/workflow.md` 的发行页链接改为 a18，因此 `asterun-skill-0.1.0a18.zip` 的摘要与 a17 不同。

结果的多步写入放在同一个事务里。失败则整次回滚，运行进入 `pending_reconcile`、操作进入 `unknown`，并释放本次并发名额。完整结果留在内存，状态库暂时写不进去时按有上限的退避重试落盘，不自动重派。并发名额收成运行记录上的 `slot_held`，调度器按 run id 幂等投影。审批结果写盘失败后，重试或重启即使只看到运行中的进度，也会写下审批结论。

Codex 与 Claude 子进程只接收白名单环境：HOME、PATH、语言区域、代理和证书路径，以及 Codex 的 `CODEX_HOME`、Claude 的 `CLAUDE_CODE_OAUTH_TOKEN` 和 `CLAUDE_CONFIG_DIR`。`extra_env` 只追加变量名，不保存秘密值，并拒绝加载器变量。Claude 普通任务默认不加载用户 hooks 与 MCP；`load_user_settings: true` 只恢复普通任务，快照审查仍隔离。外部插件 runner 只允许绝对路径解释器加 `-m` 模块，或单独一个绝对路径可执行文件。`-m` 启动使用 `-I -S -B`。`runtime_path` 与 `runtime_sha256` 必须覆盖实际将要执行或导入的代码。`allow_unpinned_runtime` 默认关闭，打开后也只能加载配置，不能执行。

推送 `v*` 标签后，发行工作流先在 Python 3.11 与 3.12 上跑完整离线校验，并要求标签提交已在 `main` 上。还没有可核对的已上传资产时才构建：`SOURCE_DATE_EPOCH` 取该提交时间，构建依赖钉为 setuptools 84.0.0 与 wheel 0.48.0。草稿续传不重建，缺的文件从同一次 `asterun-dist` workflow artifact 补上。内置 Antigravity 增加可选 `timeout_seconds`，范围 30 到 3600，省略时仍为 300 秒，同时用于 `--print-timeout` 和适配器等待。外部插件 `asterun-plugin-antigravity` 为 `1.0.3`：它不读取内置的 30 到 3600 秒字段，自己的 `timeout_seconds` 仍默认 20 秒、最多 20 秒，并同时写入 worker 期限和 `--print-timeout`。Cursor 与 Jules 插件仍为 `1.0.0`。原生 SUCCESS 但带工具错误、步骤错误或软拒绝时，仍记为 `ANTIGRAVITY_RUN_INCOMPLETE`。

升级时，依赖未列入白名单的宿主变量要把变量名写入 `extra_env`，不要写入秘密值。macOS 服务的 SAFE_ENV 没有扩大，服务进程里本来没有的变量不会被 `extra_env` 找回。依赖用户 hooks 或项目 MCP 的 Claude 普通任务要显式打开 `load_user_settings`。已经用 `python /path/worker.py`、相对路径、`-c` 或解释器加脚本写成 runner 的外部插件无法加载，需改成 `-m` 或单个绝对路径可执行文件，并按实际安装树重新注册 `runtime_path` 与 `runtime_sha256`。换装 `1.0.3` 后安装树摘要变化，已注册连接需重新注册。已发布版本的工作流重跑依赖 90 天内的 `asterun-dist` 产物；产物过期后的恢复见下方发行流水线，已发布的发行不要删除。

本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a18.zip`、三个外部插件的 wheel 与源码包（Antigravity `1.0.3`，Cursor 与 Jules `1.0.0`），以及 `SHA256SUMS`。

## 0.1.0a17

发布外部 Antigravity 插件 `asterun-plugin-antigravity` `1.0.1`。a16 已把 Linux x86_64 的 CLI 1.2.0 profile 合入插件内的 `_vendor/profile.py`，但插件版本仍为 `1.0.0`，且没有发布新的插件包；外部 v2 插件用户实际加载的 a13 `1.0.0` wheel 仍会拒绝 Linux HOME。本版把插件版本与 manifest 的 `plugin_version` 升为 `1.0.1`，并在发行页提供由本标签构建的插件 wheel 与源码包。插件代码相对 a16 未变，CLI 版本门禁仍为 `upstream_version=1.2.0`。已注册 `1.0.0` 的连接在换装后 manifest 摘要变化，需按[插件说明](plugins.md)重新注册并核对准入。核心 sdist 按既有约定只含插件 README 与 LICENSE，插件以独立分发包发布。Cursor 与 Jules 插件仍为 `1.0.0`，沿用 a13 资产。

核心代码相对 a16 无行为变化，仅版本号更新。本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a17.zip`、`asterun_plugin_antigravity-1.0.1` 的 wheel 与源码包，以及 `SHA256SUMS`。

## 0.1.0a16

Linux x86_64 支持 Antigravity CLI 1.2.0 profile。`webm_encoder` 按平台核对精确大小和 SHA-256：macOS 仍只接受原有 12781442 字节构建，Linux x86_64 只接受官方 Linux 1.2.0 构建（17056035 字节，`45c1b1edd50159fbd4eac95ffd82df97a79b8c345fc43408ddae09230a304ed6`），没有通配项，Linux aarch64 的编码器仍被拒绝。Playwright 空缓存目录在 Linux 为 `~/.cache/ms-playwright-go/1.57.0`，macOS 不变。agy 1.3.2 仍不支持。Linux 支持适用于核心内置的 `antigravity` 后端；可选的外部 `asterun-plugin-antigravity` 本次不发新版，a13 发行的 1.0.0 wheel 仍带旧 profile，会拒绝 Linux HOME。使用外部插件的 Linux 用户须从 `v0.1.0a16` 标签的 `packages/asterun-plugin-antigravity` 固定源码自行构建。

Codex 后端新增可选 `approval_policy`，可取 `untrusted`、`on-failure`、`on-request`。`on-request` 在工作区沙箱内自动读写和执行命令，越界才询问；`on-failure` 仅在沙箱导致失败时询问。省略时普通任务仍发送 `untrusted` 与 `workspace-write`。`never`、granular 对象和 `danger-full-access` 在校验时拒绝，固定读取仍为 `never` 与 `read-only`。用法见[安装说明](install.md#原生审批模式)。配置 schema 仍为 v1/v2，SQLite 不变。

已知说明：Linux 上 profile HOME 中出现 `Library` 目录现在会被拒绝，比 a15 更严格。同步 Codex `dispatch` 现在默认显式发送 `untrusted` 与 `workspace-write`，不再沿用 `~/.codex/config.toml`。已创建任务在续接 `thread/resume` 时使用当前配置中的审批模式，而不是创建时的值。

本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a16.zip` 和 `SHA256SUMS`。标准 skill 的 `SKILL.md`、脚本和代理配置与 a15 相同，仅 `references/workflow.md` 的发行页链接改为 a16，因此技能包摘要与 a15 不同；最低接口版本仍为 a14。两项改动的 PR 均通过 Python 3.11/3.12 CI；Linux 上真实 agy 与真实 Codex 的现场验收在发布后单独记录，见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a16)。

## 0.1.0a15

修复 Grok 编码运行返回已绑定会话的明确未完成/取消结束事件、同时以退出码 1 结束时被误判为未知的问题。只有匹配的明确结束证据可以确认终态；成功事件配合非零退出、超时、错会话和损坏协议仍保持未决。旧误判可通过原任务的 `task-reconcile` 显式核对持久化回执，不创建替代运行，不更改验收结论。

标准 skill 新增 `scheduler` 占位诊断和 `reconcile --task-id ID`，占位引用遵循任务读取权限。对账可能释放名额并启动原有队列，因此须核对原任务和返回的终态，不能把请求成功当成任务成功。建议核心与技能一起升级到 a15；技能最低接口版本仍为 a14，但 a14 核心不含本次终态修复，单独替换技能不能解决核心占位。

本次资产为核心 wheel、源码包、`asterun-skill-0.1.0a15.zip` 和 `SHA256SUMS`。修复阶段完整离线回归 2644 项通过、4 项真实后端测试跳过；独立安装的恢复专项 21 项通过，CLI/MCP、重启与备份恢复验证通过。本机修复构建已实际对账并释放旧占位，原生会话、用量与无关历史记录保持不变，未新增真实模型任务。详见[现场验证](https://github.com/zonzideka/asterun/blob/v0.1.0a15/docs/validation/skill-runtime-recovery-2026-10-03.md)，最终发行提交 CI 与资产验证见[发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a15)。

## 0.1.0a14

相对已发布的 `v0.1.0a13`，源码包含其后合入的外部主控紧凑观察（`task-get --compact`、`task-watch --compact --no-events`）、`workflow-snapshot`、标准 skill 入口，以及技能对核心版本的显式门禁。标准发行版最低要求 CLI 与常驻核心均为 `0.1.0a14`；仍准确报告 a13 的历史主线构建，仅在 CLI 参数与常驻核心只读接口探测均通过后兼容。公开标签版 a13 缺少这些接口，会在业务提交前返回 `CORE_VERSION_UNSUPPORTED`。探测失败不会发出变更或只读业务命令；连接失败、超时、未知版本和无法确认的能力分别报告，规则见[标准 skill](standard-skill.md)。本次还补全授权工作区发现、后端执行模式提示、用量分页和大响应读取。

本次发行资产为核心 wheel、源码包、`asterun-skill-0.1.0a14.zip` 和 `SHA256SUMS`。三个可选插件保持 `1.0.0`，沿用 [a13 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a13)的原有 wheel、源码包和校验文件。GrokBot 模板 `0.1.0-rc1` 的发行锁仍固定公开 a13 wheel 及其 SHA-256，聊天技能行为不变；该锁安装的实例不能使用标准 skill，模板锁的升级另行验证和发布。

## 运行范围

Asterun 支持 Python 3.11 及以上，按单用户、单执行主机和本地文件系统部署。核心提供 CLI、stdio MCP、常驻执行器、持久任务和会话引用、事件、幂等、审批、取消与对账、备份恢复，以及可选质量工作流和 PR 审查配方。

CLI、MCP 和 GrokBot 等入口共用核心。同机客户端通过本地 socket 连接，远程调用通过宿主已有的授权通道接入。

## 后端支持

| 后端 | 当前能力 | 验证情况与适用条件 |
|---|---|---|
| Codex | 文件读取与编辑、命令和测试执行、原生线程续接、命令/文件审批、取消与对账、可选固定读取和项目登记 | 工具执行遵循任务权限与审批。真实文本和 PR 审查有运行记录；新固定读取的模型行为、通用续接/取消及完整新会话桌面显示待现场验收，项目登记限同机 macOS Desktop |
| Grok | 默认文本模式；显式 `workspace-code-v1` 支持读取、编辑、测试和迭代，编码模式可选 `session_policy: "resume"` 续接受管原生会话 | 文本任务在 macOS/Linux 有运行记录；自主编码在 macOS 完成一次闭环。受管续接已通过离线协议替身，真实模型续接与原生取消/客户端显示仍待现场验证；文本模式原生续接、取消及审批桥接尚未接入 |
| Claude | 单次任务调用、文本处理、工作区文件读取和固定快照输入 | 普通调用配置 Read/Grep/Glob，快照调用使用传入内容。已做离线验证，真实后端待验收；原生续接尚未接入 |
| Antigravity CLI | CLI 1.2.0 的文本执行和受限工作区读取 | macOS 的文本、读取及权限拒绝检查通过。Linux x86_64 的 profile 接受官方 1.2.0 `webm_encoder` 固定摘要和空的 `~/.cache/ms-playwright-go/1.57.0`；该平台真实执行仍待现场验收。原生续接、恢复对账和审批桥接尚未接入 |
| 独立插件 | Antigravity 账户、Cursor local Agent、Jules REST | 预览包；真实供应商、计费和客户端验收待完成 |

任务目标、Agent 组合及执行流程由用户选择。按所选后端查阅[安装说明](install.md)、[Antigravity 用法](antigravity.md)或[插件说明](plugins.md)。用量按后端提供的原始范围记录；额度节省仍需同任务对照数据验证。

Codex 固定读取按实际协议和生效权限检查兼容性。项目登记根据真实目录及 Git worktree 关系关联原线程，展示重试使用 `task-present`。Grok 编码模式需显式启用，原文本配置沿用原权限。

## 发行流水线

推送匹配 `v*` 的标签后，[发行工作流](https://github.com/zonzideka/asterun/blob/main/.github/workflows/release.yml)先在该标签提交上跑完整离线校验。校验任务是 Python 3.11 与 3.12 的矩阵，两套都跑 `scripts/verify-offline.sh`（含 `verify-source.py` 与全量 pytest）。[scripts/check-release-tag.py](../scripts/check-release-tag.py) 同时要求标签等于 `v` 加核心 `pyproject.toml` 的版本，且该提交已经在 `main` 上。后续任务用 `needs: verify` 等待矩阵全部成功。还没有可核对的已上传资产时才构建，并把产物存成名为 `asterun-dist` 的 workflow artifact，再写来源证明。

资产为核心 wheel 与源码包、`packages/` 下每个外部插件的 wheel 与源码包、`asterun-skill-<核心版本>.zip`，以及 `SHA256SUMS`。文件名与 a15、a17 手工发行相同；校验文件每行是小写 SHA-256、两个空格和文件名，清单本身不计入。

构建使用 [scripts/build-release-assets.py](../scripts/build-release-assets.py)。`SOURCE_DATE_EPOCH` 固定为该标签提交的 Unix 时间，构建依赖钉为 setuptools 84.0.0 与 wheel 0.48.0。脚本随后把 tar 与 zip 的时间戳、属主和权限归一，因此同一次提交上的两次构建逐字节一致。它先按 [打包检查脚本](../scripts/check-release-package.py) 核对 wheel 与源码包，再在干净虚拟环境一并安装这些 wheel 及其依赖，导入核心与各插件声明的依赖，执行 `asterun version` 和 Antigravity 的 `prepare --home`。每个插件 worker 先按实际安装树钉定 `runtime_path` 与 `runtime_sha256` 并注册，再经 `-I -S -B` 隔离启动调用 `plugin.describe`。工作流只用 `GITHUB_TOKEN` 和 GitHub OIDC：`actions/attest-build-provenance` 为上述文件写入构建来源证明。[scripts/publish-release-assets.py](../scripts/publish-release-assets.py) 在首次发布时上传这次构建，核对远端 SHA-256 后再标成预发布。同名草稿以已上传资产和它的来源证明为准，缺的文件只从该证明指向的同一次 `asterun-dist` workflow artifact 补上。这个 artifact 保留 90 天。过期后，续传无法下载它，也不会改用新构建覆盖已上传资产。若草稿里应有的文件已经齐全，并且和 SHA256SUMS 一致，把该草稿手动标成预发布即可。若仍缺文件，删除这个尚未发布的草稿，再重跑同一标签的工作流，让它重新构建。已经发布的发行不要删除。已发布且摘要一致时，只有同一次构建的 workflow 产物尚未过期，重跑才会直接成功。摘要不同、多出文件或已发布后仍缺文件时失败，不覆盖、不删除已有资产。不需要维护者另行配置密钥，也不把包发到其他索引。已发布的 a15 与 a17 资产仍是当时的手工构建；这条流水线还没有对现有标签重跑。

## 验证与升级

2026-10-03 标准 skill 修复阶段的完整本机离线回归记录为 2610 项通过、4 项真实后端测试跳过、1 项版本探测超时失败；同一失败场景随后连续三次隔离复测通过，原整套命令并非全绿。五组合兼容矩阵、独立消费者流程及两项独立 wheel 端到端验收通过，wheel/sdist 的 102 个运行文件与固定源码一致。范围与原始失败见[本次修复验收记录](https://github.com/zonzideka/asterun/blob/v0.1.0a14/docs/validation/standard-skill-repair-2026-10-03.md)。最终发行提交的 CI 和资产校验结果以[对应版本发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a14)为准，不以历史快照的结果代替。本轮没有新增真实模型任务；真实产出、原生客户端与 Token 收益仍需分别验收。

配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。升级前备份配置、状态和原生 HOME，处理在途及未知执行，再切换服务并应用配置修订。升级后产生的新任务纳入恢复记录，操作步骤见[安装说明](install.md#诊断备份与恢复)。

wheel 用于安装，sdist 另含用户文档、样例和发行检查脚本。部署环境保存运行状态、原生会话和凭据，完整 Git 源码提供开发测试与来源核验材料。
