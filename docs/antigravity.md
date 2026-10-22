[中文](antigravity.md) | [English](en/antigravity.md)

# 使用 Antigravity CLI

Asterun 通过官方 `agy` CLI 的 `stream-json` 调用用户的原生账户，支持 CLI 1.2.0 及以上版本：启动前在临时 HOME 中运行 `agy --version` 与 `agy --help`，版本低于 1.2.0 或缺少 `--input-format`、`--output-format`、`--model`、`--disable-slash-commands`、`--print-timeout`、`--add-dir` 任一参数时给出明确错误。该探测只在后端已启用且真实派发开关 `ASTERUN_RUN_ANTIGRAVITY=1` 打开时运行，最多读取 64 KiB，15 秒超时并杀死其进程组。`workspace-read-v1` profile 支持文本任务和指定工作区读取，每个 Run 创建新的进程和原生会话。macOS 已验证文本执行、文件读取和权限拒绝。Linux x86_64 上，官方 CLI 1.2.0 的 `webm_encoder` 与 `~/.cache/ms-playwright-go` 已纳入按平台固定的 profile 检查；该平台的真实文本执行、文件读取和权限拒绝仍待现场验收。其他系统与其他 CLI 版本需单独核验后才能加入清单。

## 准备原生 HOME

先安装[官方 CLI](https://antigravity.google/docs/cli/install/)，核对程序的绝对路径。Asterun 按配置 `bin`、`ANTIGRAVITY_BIN`、PATH、平台标准安装位置的顺序查找程序。

下面使用临时示例目录。长期运行时，请换成私有持久目录，分别存放原生 HOME 和工作区：

```sh
umask 077
mkdir -p /private/tmp/asterun-agy-demo/workspace
asterun-antigravity prepare --home /private/tmp/asterun-agy-demo/native-home
asterun-antigravity inspect --home /private/tmp/asterun-agy-demo/native-home
```

`prepare` 初始化新建或空 HOME；目录已有内容时，会按 profile 核验。然后在该 HOME 中启动原生 CLI，完成 Google 登录。将下面的程序路径替换为实际安装路径：

```sh
cd /private/tmp/asterun-agy-demo/workspace
/usr/bin/env -i \
  HOME=/private/tmp/asterun-agy-demo/native-home \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  /absolute/bin/agy
```

若 CLI 询问工作区信任，选择这个独立目录。当前 profile 接受一个有效的 `trustedWorkspaces` 绑定，后续执行目录须与其一致。登录完成后退出 CLI，再运行 `inspect` 核对配置。

接着绑定读取范围：

```sh
asterun-antigravity bind-workspace \
  --home /private/tmp/asterun-agy-demo/native-home \
  --workspace /private/tmp/asterun-agy-demo/workspace
```

该命令添加精确的 `read_file` allow，并保留六项 deny。读取绑定、原生信任和执行工作区须指向同一目录。启动前会检查工作区及其祖先；存在 `.agents`、`.agent`、`.gemini`、`AGENTS.md` 或 `GEMINI.md` 时，需先准备独立源码快照，再绑定该快照目录。

## 配置并提交任务

将下例保存为 `/private/tmp/asterun-agy-demo/config.json`，替换 `bin` 和模型。模型 ID 通过同一原生账户的 `agy models` 核对：

```json
{
  "schema_version": 1,
  "revision": 1,
  "workspaces": {
    "agy-demo": {
      "root": "/private/tmp/asterun-agy-demo/workspace",
      "allow_non_git": true
    }
  },
  "backends": {
    "antigravity": {
      "kind": "antigravity",
      "enabled": true,
      "bin": "/absolute/bin/agy",
      "home": "/private/tmp/asterun-agy-demo/native-home",
      "model": "MODEL_ID_FROM_AGY_MODELS"
    }
  },
  "default_backend": "antigravity"
}
```

可选字段 `timeout_seconds` 同时限制原生 `--print-timeout` 和适配器等待时间。省略时为 300 秒，命令行仍是 `5m`。允许 30 到 3600 的整数；能被 60 整除时传给 CLI 的是分钟，例如 600 秒为 `10m`，否则是秒，例如 90 秒为 `90s`。v1 写在 `backends.<name>`，v2 写在内置 `google.antigravity-cli` 连接的 `options`。外部插件不读取这个内置字段。插件自己的 `timeout_seconds` 默认 20 秒，必须大于 0 且不超过 20，同时用于 worker 期限和 `--print-timeout`。省略时插件命令行是 `20s`。小数向上取整后只传给 CLI，worker 仍按原小数截止。

确认安装、profile 和模型后，在核心服务进程中开启执行。以下操作会使用原生账户额度：

```sh
ASTERUN_RUN_ANTIGRAVITY=1 asterun \
  --config /private/tmp/asterun-agy-demo/config.json \
  --state-dir /private/tmp/asterun-agy-demo/state serve
```

在另一终端提交任务，再用返回的任务 ID 观察结果：

```sh
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-submit --workspace agy-demo --backend antigravity \
  --text '回复 ASTERUN_AGY_OK。' --idempotency-key agy-demo-text-1
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-watch TASK_ID --timeout 60 --request-timeout 5
```

`backend-inspect` 和 `connection-verify` 检查安装与配置，登录状态仍记为 `not_tested`。提交重试使用原幂等键；观察到期后继续查询同一任务。

## 权限和结果的含义

profile 使用 `request-review`，只授予精确工作区的文件读取权限，拒绝写文件、执行命令、网页读取与执行、unsandboxed 和 MCP 工具。原生 CLI 仍会保存自身日志、会话和缓存。上述权限由原生策略执行，OS 强制隔离待验收。

适配器使用专用 HOME，清理继承的认证和配置覆盖，并核对已知状态形状。内置技能按结构接受：`skills/<name>/` 下归本用户所有、不可执行的 `.md`，以及 CLI 的 `.checksum` 和 `keep.txt`；每个文件不超过 128 KiB，不能是符号链接，且必须有 `.checksum`。`bin/webm_encoder` 必须是归本用户所有的普通文件，其他用户不可写，大小不超过 64 MiB。Playwright 缓存可以包含任意 `x.y.z` 版本目录，这些目录必须为空。可选字节钉定把环境变量 `ASTERUN_ANTIGRAVITY_PINS` 设为 JSON 文件的绝对路径，内容为 `{"builtin": {"path": "sha256"}, "webm_encoder": {"size": n, "sha256": "..."}}`，仅在设置该变量时按清单精确核验。Asterun 不关闭 agy 的自动更新，默认跟随最新版：agy 可以装在可写目录并自行升级。每次启动都按可执行文件身份（inode、大小、修改时间）重新探测版本与所需参数，并把本次所用版本记入运行证据 `cli_version`；运行中途发生自更新时，正在运行的进程继续使用原来的映像，结果照常判定，另记 `cli_updated_during_run=true`，下一次启动按新版本重新探测。CLI 1.4 起模型可能通过 `invoke_subagent` 派出子代理（该步骤随后被改报为 `subagent`）；这时 agy 可能一直不给结果，或给出结果后不退出。Asterun 一看到 `invoke_subagent` 工具步骤或 `subagent` 步骤就立即结束整个进程组，确认进程组已退出后记为 `ANTIGRAVITY_RUN_INCOMPLETE`（`failure_type=subagent_delegated`，并记 `subagent_count`），并发位随之释放；无法确认进程组已退出时保留为待对账（`subagent_process_alive`）。agy 1.4.2 没有关闭子代理的参数，可在提示词里要求不使用子代理。`.gemini/config/mcp_config.json` 只能为空，或只包含空的 `mcpServers` 映射。认证保留在原生账户中，`useG1Credits=false`。

公开结果包含 `agent_response`、工作区、模型、权限和选定统计。成功结果须同时具备有效 init、完整步骤、匹配的原生终态和正常退出回执。只有存在策略硬拒绝时才要求有最终回答。硬拒绝只认 agy 原生 deny 规则的完整报错：错误对象的键只能是 `type` 和 `message`，`message` 必须整句匹配 `permission check failed for <kind> "...": Permission denied for <kind>(...). Matches user-configured deny rule.`（前后 `<kind>` 一致）。这种硬拒绝且模型给出最终回答时仍算成功，并记录 `policy_denied_count` 与 `policy_denied_tools`；没有最终回答则记为 `ANTIGRAVITY_RUN_INCOMPLETE`。句子被截断、前后另有文字，或错误对象带有其它字段，都不算硬拒绝。软拒绝记为 `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED`，并记录 `soft_denied_tools`。其中一种从工具步骤的原生报错整句识别：错误对象的键只能是 `type` 和 `message`，`message` 必须整句匹配 `permission check failed for <tool> "<arg>": user denied permission for <tool>(<arg>)`，换行后接 `Do not attempt to circumvent this denial by rephrasing the command, using alternative tools/scripts (e.g. python, sh, curl), or accessing the same target resource. Proceed without performing this action.`。前后 `<tool>` 必须相同。硬拒绝和这种复核拒绝的引号参数都是 Go `%q` 正文，必须还原后与括号内原文完全一致（原文可以包含真实换行）；Asterun 会在发给 CLI 的提示前加上一段写明已解析工作区的固定只读前言，不改变已保存的任务文本。句子被截断、前后另有文字、错误对象带有其它字段，或两边参数不一致，都不算这种软拒绝。识别到的步骤计入 `review_denied_count`，并视为明确的复核证据。stderr 上的宽泛信号（例如 `permission denied`）只会置软拒绝标志，本身不是复核证据，因此单独出现时仍是 `ANTIGRAVITY_RUN_INCOMPLETE`。stderr 上明确的 soft-deny 或审批措辞仍可报复核码。其他工具错误记为 `ANTIGRAVITY_RUN_INCOMPLETE`。`native.unexplained_error_count` 始终输出，等于工具错误与步骤错误去掉策略硬拒绝和这类复核拒绝后的个数。`native.review_denied_count` 始终输出。软拒绝若还伴随与权限无关的错误，摘要会说明修正授权后仍可能失败，错误码仍是 `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED`。原生 conversation ID 保存在 `native.backend_session_id`，`native.usage_scope=session` 表示会话累计用量。

当前每次独立执行创建新 Asterun 会话；原生续接、恢复对账和机器审批桥尚未接入。取消会尝试中断原生进程，收到原生 CANCELED/INTERRUPTED 和退出回执后记录 `cancelled`；结果不明时保留 `pending_reconcile`，由操作者核对原生状态。

## 迁移旧权限配置

早期 strict profile 需显式迁移。先停止使用该 HOME 的 CLI 和任务，再选择 HOME、工作区之外的私有备份目录：

```sh
mkdir -m 700 /private/tmp/asterun-agy-demo/migration
asterun-antigravity upgrade-permissions \
  --home /private/tmp/asterun-agy-demo/native-home \
  --backup /private/tmp/asterun-agy-demo/migration/settings.strict.json
```

迁移会校验旧配置、备份原始字节，再原子更新 `toolPermission` 并返回摘要。已有备份、配置偏离或并发变化时会停止覆盖。需要回退时，先停止 CLI，将备份原子恢复到 `.gemini/antigravity-cli/settings.json`，保持 0600 权限，并使用支持 strict 的旧适配器。

原生 CLI 的细节见 [headless](https://antigravity.google/docs/cli/headless/)、[设置](https://antigravity.google/docs/cli/settings/)、[权限](https://antigravity.google/docs/cli/permissions/)和[额度](https://antigravity.google/docs/cli/credits/)文档。
