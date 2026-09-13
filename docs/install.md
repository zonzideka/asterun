[中文](install.md) | [English](en/install.md)

# 安装与配置

准备 Python 3.11 或更新版本。wheel 用于安装，sdist 另含用户文档、样例和运维脚本。下文的 `/path/to` 和 `/absolute` 均需换成实际路径。

## 安装

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
source .venv/bin/activate
```

激活虚拟环境后，直接使用 `asterun`。开发时从完整 Git 检出安装 `.[test]`，再运行 `scripts/verify-offline.sh`；默认测试使用临时状态和 fake 后端。

## 配置并启动核心

先创建工作区，将以下示例保存为 `/path/to/config.json`，并将 `root` 换成已存在的目录：

```json
{
  "schema_version": 1,
  "revision": 1,
  "workspaces": {
    "demo": {"root": "/path/to/workspace", "allow_non_git": true}
  },
  "backends": {
    "fake": {"kind": "fake", "enabled": true}
  },
  "default_backend": "fake"
}
```

启动常驻核心：

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

在另一终端连接同一实例，提交任务、查询结果或启动 MCP 入口：

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --text "第一次" --idempotency-key first
asterun --state-dir /path/to/state --connect task-get TASK_ID
asterun --state-dir /path/to/state --connect mcp
```

状态目录保存 SQLite 数据库、单实例锁和权限为 0600 的 `asterun.sock`。同机同一用户的客户端可连接，任务使用核心启动时的配置与环境变量。独立 `asterun mcp` 也可持有核心；每个状态目录由一个核心进程管理。

配置位置通过 `--config`、`ASTERUN_CONFIG` 或 `$ASTERUN_HOME/config.json` 指定。工作区相对路径按配置文件所在目录解析，真实后端在该工作区的实际根目录执行。

提交时选择一种输入来源：`task-submit --input -` 从 stdin 读取最多 512 KiB 的 UTF-8 文本，`--input FILE` 读取工作区内文件。同一次提交重试时，保持幂等键一致：

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend codex --input - \
  --idempotency-key review-attempt-1 < /path/to/prompt.txt
```

使用 `task-watch TASK_ID --timeout 60 --request-timeout 5` 观察任务。到期退出码为 124，Ctrl+C 为 130；按返回的 run ID 和游标，以 `--run-id RUN_ID --cursor N` 续读。客户端断开后核心继续执行。停止核心后，在途任务保留未决状态，重启时先对账。

## Codex

先用 `asterun codex-discover` 查找候选；它会列出 macOS ChatGPT.app/Codex.app 中的可执行文件，加上 `--probe-version` 可查询版本。核对后将路径写入 `backends.<name>.bin`。省略 `bin` 时，按 `CODEX_BIN`、PATH 查找。

真实执行需要 `enabled: true`、可执行文件、原生登录，以及核心进程中的 `ASTERUN_RUN_CODEX=1`。`backend-inspect` 和默认 `connection-verify` 用于检查安装配置，认证状态显示 `not_tested`。

常驻模式已接入原生线程续接、命令/文件审批和轮次中断。待批对象通过 `task-get` 查看；用户输入和额外 permissions 等原生请求仍需对应的专用处理。新 PR 审查可用[固定文件读取](fixed-read-scope.md)，实际协议或权限不兼容时会返回错误。

设置 `desktop_projects: true` 可启用[真实项目登记](codex-projects.md)。展示未完成时，用 CLI `task-present` 或 MCP `task_present` 重试。旧配置默认关闭该选项。

## macOS 服务配置

`service-plan` 为现有安装中的 clean-env LaunchAgent 准备 Codex 启用计划，也可同时登记工作区。配置、状态目录、启动文件和运行程序须属于当前用户，并通过私有权限检查。先创建计划的父目录，保留计划目录为未创建状态；磁盘 revision 应等于已应用值。

```sh
/absolute/service-venv/bin/asterun \
  --config /path/to/instance/config.json --state-dir /path/to/instance/state \
  service-plan --plist /path/to/LaunchAgents/com.asterun.core.plist \
  --plan-dir /path/to/plans/enable-codex-1 --codex-bin /absolute/bin/codex
/absolute/service-venv/bin/asterun service-apply /path/to/plans/enable-codex-1/plan.json \
  --plan-sha256 PLAN_SHA256 --verify-connection
```

审阅计划，再使用其 SHA-256 应用。程序会重新核对配置、启动文件、安装内容和状态；在途任务、UNKNOWN 或未决 outbox 会阻止重启。连接验证执行 initialize/account 握手。

发生失败时，按记录阶段回读状态。若无新增状态和文件变化，可执行 `service-rollback PLAN --plan-sha256 PLAN_SHA256` 恢复原文件与 revision。回退后服务保持停止，检查后再启动。此入口处理现有安装的 Codex 启用，跨版本替换另行准备完整升级与回退计划。

## Grok 与 Claude

可执行文件按配置 `bin`、`GROK_BIN`/`CLAUDE_BIN`、PATH 上的 `grok`/`claude` 顺序查找。真实执行分别需要核心进程中的 `ASTERUN_RUN_GROK=1` 或 `ASTERUN_RUN_CLAUDE=1`。默认检查读取安装和配置；显式设置 `GROK_MODELS_CACHE` 后才读取 Grok 模型缓存。

Claude 通过 `claude -p` 执行一次任务。Grok 提供文本和编码两种模式，均使用用户独立授权的原生 HOME。

## Grok 文本配置

在执行主机安装 Grok CLI，再准备私有 HOME 并完成设备授权：

```sh
asterun-grok prepare --home /absolute/private/grok-profile
asterun-grok login --home /absolute/private/grok-profile --bin /absolute/bin/grok
asterun-grok inspect --home /absolute/private/grok-profile
```

`prepare` 初始化新目录，或核验已有同一 profile；`inspect` 检查配置和文件元数据。Linux 沙箱依赖 `bubblewrap`。系统若启用了 AppArmor userns 限制，需由管理员配置应用对应权限。

将以下片段放入 `backends.<name>`：

```json
{
  "kind": "grok",
  "enabled": true,
  "bin": "/absolute/bin/grok",
  "home": "/absolute/private/grok-profile",
  "model": "grok-4.6"
}
```

`model` 默认请求 `grok-4.6`，显式指定的模型原样交给 CLI。`text-only-v1` 最多运行一轮，默认超时 180 秒，使用空工具集和原生 read-only 沙箱。派发时使用专用 HOME，清理继承的认证和配置覆盖，并关闭配置同步、自动更新和扩展工具。

专用配置、扩展和工作区祖先目录中的额外 `.grok` 配置来源须符合 profile。原生沙箱的隔离效果依平台而异；处理不可信材料时，使用独立 OS 用户或执行主机。自动原生续接、取消和审批桥接尚未接入。

启动失败若确认发生在发送 prompt 前，记录为失败和 0 turns；发送后的不明结果保留 UNKNOWN 与已知原生会话引用。后续查询沿用原任务。

## Grok 自主编码配置

`workspace-code-v1` 用于在真实工作区读取、编辑、运行测试，并在同一原生会话内迭代。先准备命名沙箱：

```sh
asterun-grok prepare --home /absolute/private/grok-profile --execution-profile workspace-code-v1
```

再配置独立后端别名：

```json
{
  "kind": "grok",
  "enabled": true,
  "bin": "/absolute/bin/grok",
  "home": "/absolute/private/grok-profile",
  "model": "grok-4.6",
  "execution_profile": "workspace-code-v1",
  "max_turns": 12,
  "timeout_seconds": 900
}
```

v1 将该对象放入 `backends.grok-code`；v2 将 bin/home/model 和编码选项放入内置 Grok 连接的 `options`。应用配置修订后，使用 `task-submit --workspace PROJECT --backend grok-code --text TR` 提交。若需隔离文件修改，先创建 Git worktree，再将其实际目录登记为工作区。

原生回合默认 12，范围 1-100；超时默认 900 秒，范围 1-3600。提交和派发时均检查 `workspace.read`、`workspace.write`、`process.execute`。旧配置省略 `execution_profile` 时沿用文本模式。

编码模式使用 headless streaming-json，按配置直接执行文件读写、搜索和终端管理工具。Web、MCP 和子代理保持关闭。原生必须成功应用命名沙箱：读取范围覆盖文件系统，写入范围包括工作区、原生 HOME 和临时目录；macOS 子进程仍可能访问网络。执行用户与主机按仓库信任程度选择。

事件记录公开工具进度和原生引用。匹配的 `end_turn` 与进程正常退出共同形成执行成功，质量验收继续按任务配置处理。断线、超时或不明退出保留 UNKNOWN，等待对账。

`run.native` 保存原生 `num_turns`、`usage` 和 `modelUsage`。runner-control/v1 的 turns 按 Asterun 派发次数统计，供应商报告和核心预算各自保留原口径。

## 会话绑定与配置修订

提交后保存返回的 `conversation_id`。`session-resume --conversation-id ID` 恢复本地索引，并核对账户、主机和工作区。Codex 的 `--native` 另核对原生线程；再次提交 `task-submit --conversation-id ID` 时，通过原生最新轮次校验后续接。未结束或未决运行、外部新增轮次和绑定变化会阻止继续。

修改配置时递增 `revision`。假设已应用值为 1、候选为 2，先处理在途任务，再重启核心加载候选，然后执行：

```sh
asterun --state-dir /path/to/state --connect config-validate
asterun --state-dir /path/to/state --connect config-apply --expected-applied-revision 1
```

`config-validate` 返回 `loaded_config_revision` 和 `applied_config_revision`。CAS 参数填写当前已应用值，应用成功后两者均为 2。客户端 `--config` 指定客户端配置，核心配置通过重启加载。原任务保留创建时的绑定。

## 质量工作流与调度

`workflow-evaluate` 将检查绑定到任务 revision/input_hash。配置检查后产生验收结论；未配置检查或仅使用 `run_succeeded` 时，状态为 `not_configured`。`contains` 检查输出，`file_contains` 和 `file_exists` 检查工作区文件。

`workflow-start` 对已成功的实现任务执行检查、独立审查、修复和复核。先配置 `review_backend`，当前 reviewer 适配为 Claude 或离线 fake。请求示例：

```json
{
  "idempotency_key": "quality-1",
  "target_paths": ["src/example.py"],
  "checks": [{"kind": "file_contains", "path": "src/example.py", "text": "def main"}],
  "auto_repair": true
}
```

```sh
asterun --state-dir /path/to/state --connect workflow-start TASK_ID --request quality.json
```

检查条件按任务目标选择。快照最多包含 64 个 UTF-8 普通文件，总量 256 KiB；完整提示词最多 96 KiB。Git 工作区同时绑定 HEAD。通过 `task-get` 查看 `quality.phase`、证据、finding 和阻塞原因。

`workflow-repair` 在原任务下创建新运行，遵守配置中的修复上限。同键重试回读原运行，质量流程已持有的任务通过流程继续或接管。`auto_repair: false` 在发现问题后交回人工；暂停和取消会停止后续阶段。目标发生变化后，原证据失效，需重新准备流程。

队列默认最多 32 项，每后端并发 1，每个任务最多运行 4 次。同一实际工作区串行执行，等待审批或结果未知的运行占用名额。重启后重建未派发队列，并重新核对授权和绑定。已派发未完成的任务先对账，暂停项通过 `task-handoff TASK_ID --action resume` 继续。

## 诊断、备份与恢复

```sh
asterun --state-dir /path/to/state --connect diagnose
asterun --config /path/to/config.json --state-dir /path/to/state \
  state-backup --output /path/to/backups/state.tar.gz
asterun --config /path/to/config.json --state-dir /path/to/restored-state \
  state-restore --input /path/to/backups/state.tar.gz
```

备份使用 SQLite backup API，输出放在状态目录之外，文件权限为 0600。恢复时核对归档成员、摘要、数据库完整性和兼容结构。先恢复到新目录检查；替换运行状态前，停止核心并保留当前记录。

核心备份包含数据库。配置、用户工作区、原生 HOME、插件私有状态和 `STATE_DIR/reviews` 配方材料分别备份。审查确认文件丢失后，系统会再次通知原结果。

当前数据库为 schema 6。打开 schema 1-5 时先保存一致快照，再升级；旧二进制使用兼容快照副本。升级后新增任务和未知操作继续保留并对账。

`entries.cli.enabled` 和 `entries.mcp.enabled` 分别控制新任务准入，诊断与已有任务读取继续可用。`diagnose` 对账户、主机和 HOME 脱敏，敏感环境变量仅显示是否存在。

## 安装验证

从 Git 检出或 sdist 运行已安装包的离线验证：

```sh
python3 scripts/verify-installed-control.py --venv /absolute/venv \
  --work-dir /absolute/new-fake --output /absolute/reports/fake.json
```

真实 Grok 文本验证会调用模型并使用账户额度。完成独立授权后，使用全新目录执行：

```sh
python3 scripts/verify-installed-control.py --venv /absolute/venv \
  --work-dir /absolute/new-live --backend grok --live \
  --grok-bin /absolute/bin/grok --grok-home /absolute/private/grok-profile \
  --grok-model grok-4.6 --output /absolute/reports/grok.json
```

脚本检查 CLI/MCP、输出与产物、事件、幂等、核心重启和备份恢复。使用普通 Python 运行，以保留断言。

Antigravity 的专用 HOME、权限绑定、迁移和运行命令见[操作说明](antigravity.md)。可选插件与 v2 配置见[插件说明](plugins.md)。
