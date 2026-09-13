---
name: asterun-bot-setup
description: 为 GrokBot 接入 Asterun，核对云电脑或本机执行位置、公开安装版本、后端和真实工作区。Set up Asterun for a GrokBot cloud computer or local invocation.
---

# 安装接入 / Setup

## 中文

用户首次使用本模板、变更执行主机或更换实例时使用本技能。先读取[模板说明](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.md)，将当前调用位置、Asterun 执行主机和后端账户对应起来。

先从[固定版本发布页](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1)取得 `asterun-grokbot-0.1.0-rc1.zip` 和 `SHA256SUMS`，按校验文件核对完整 ZIP 后解压。包内 `manifest.json` 记录源码 commit、逐文件 SHA-256 和字节数。下载后记录版本和校验结果。把解压根目录的绝对路径保存到私有安装记录；技能移入宿主全局技能库后，文档使用正文中的固定标签链接，命令继续从该解压根目录执行。

云电脑路径使用不带 `machineId` 的 `Shell`，在选定云电脑安装 Asterun、配套适配器和所需后端。本机路径先用 `ListMachines` 选择用户机器，再以该 `machineId` 调用 `Shell`，复用已有实例或新安装。安装材料按用户选择通过 `CopyToBox` 或 `CopyFromBox` 传输。共享模板不携带安装代码，接收方按[安装说明](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/install.md)准备选定公开版本，再选择后端账户并完成本人的原生登录。

已有可用实例时直接复用，先核对其版本和连接。新安装采用模板 `0.1.0-rc1` 的 [release-lock.json](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/release-lock.json)，其中固定核心 `0.1.0a13` 和公开 wheel 的摘要。取得模板代码后，在模板根目录运行以下步骤；完整仓库的对应目录为 `integrations/grokbot`。路径换成接收方的私有目录：

```sh
(
  set -eu
  umask 077
  cd /path/to/asterun-grokbot-0.1.0-rc1
  mkdir -p /path/to/private/setup
  python3 scripts/fetch_runtime.py --output-dir /path/to/private/downloads \
    > /path/to/private/setup/fetch-result.json
  ASTERUN_WHEEL=$(python3 -c 'import json, sys; result = json.load(open(sys.argv[1])); assert result["ok"] is True; print(result["data"]["path"])' \
    /path/to/private/setup/fetch-result.json)
  python3 -m venv /path/to/private/venv
  /path/to/private/venv/bin/python -m pip install "$ASTERUN_WHEEL"
  /path/to/private/venv/bin/asterun version
)
```

`fetch_runtime.py` 只下载并验证固定 wheel，返回 `data.path`、`sha256` 和 `installed: false`。上例随后显式创建 venv、安装返回路径并核对版本。已有文件匹配摘要时复用；下载或校验失败时按原错误处理。`scripts/package.py` 用于开发发行，接收方无需运行。

先发现实际可执行文件和连接方式，核对 `asterun version` 与核心 `diagnose` 返回的版本，并读取所选后端的 `backend-inspect`。安装、登录、实际可调用和客户端可见分别核验。已有正确配置保留，调整时使用配置修订和既有备份恢复流程。

编码任务的工作区绑定真实项目目录或已准备的 Git worktree，核对 Asterun 工作区实际根目录与其一致。非代码任务绑定用户指定目录；非 Git 工作区使用 `allow_non_git: true`。账户、原生 HOME、工作区和核心均按实际执行主机记录。

先运行[离线样例](https://github.com/zonzideka/asterun/blob/v0.1.0a13/examples/non-code/README.md)检查提交和结果读取。真实后端检查按用户授权范围执行，并记录是否发起模型调用及其结果。

确认配套代码及运行包已安装后，按[配套 CLI](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.md#配套-cli)使用 `init` 保存私有绑定，指定 `--ledger`、Asterun 可执行文件、核心配置、状态目录、执行位置、允许的工作区与后端及当前聊天目的地。云电脑使用 `--location host-cloud`，本机使用 `--location local`。`init` 结果用于核对安装和配置，后端登录与调用按实际结果记录。同名绑定内容有变化时，使用新 binding，保留旧任务关系。

通过 `update_state` 的 `target=routine`、`action=create` 创建该 binding 在原聊天中的 routine，使用标准五段 cron `*/5 * * * *`。已有 routine 使用 `update` 或 `resume`，每次唤醒在同一执行主机运行一次小批 `poll` 和 `inbox`。该 cron 形式的最小探针已自动触发，并在原聊天显示标记消息。

发送入口为 `SendToUser`。仅在 `claim` 返回 `ok=true`、`data.already_claimed=false` 时，使用 `type=text` 原样发送 `data.content`。宿主返回的消息 ID 与 `data.content_sha256` 用于 `ack`，记录 `host_reported_delivered`。`status` 的 `needs_followup=false` 时通过 `update_state` 的 `action=pause` 暂停 routine，新任务提交后恢复。完整过程见[任务技能](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/skills/tasks/SKILL.md)。

云电脑的材料复制、摘要校验、固定 a13 wheel 下载、Python 3.13.5 独立 venv 安装与版本读取已有实际记录。后端调用、完整任务闭环、关闭聊天后恢复和独立接收方验收继续按[验收记录](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/acceptance.md)逐项完成。

接入记录保存在接收方私有目录，包含版本、解压根目录、调用方式、执行位置、工作区、后端、routine 与聊天关联及逐项检查结果。分享文案仅保留通用配置示例和公开安装引用。

## English

Use this skill on first setup, a change of execution host, or an instance replacement. Read the [template guide](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.en.md) and establish how the caller, Asterun execution host, and backend account relate.

Obtain `asterun-grokbot-0.1.0-rc1.zip` and `SHA256SUMS` from the [fixed release page](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1), check the complete ZIP against the checksum file, and extract it. The included `manifest.json` records the source commit, each file's SHA-256, and byte count. Record the version and checksum result after downloading. Save the absolute extraction-root path in private setup records. After importing the skill into the host's global library, use its fixed-tag documentation links and continue running commands from that extraction root.

For cloud execution, use `Shell` without `machineId` to install Asterun, its adapter, and selected backends on the chosen cloud computer. For local execution, select the user's machine with `ListMachines` and call `Shell` with that `machineId`, reusing an instance or installing a new one. Use `CopyToBox` or `CopyFromBox` to transfer material according to the user's choice. A shared template does not carry installed code. Recipients follow the [installation guide](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/install.md), choose a public version, then choose their backend account and complete native login.

Reuse a working instance when available and check its version and connection first. A new installation uses template `0.1.0-rc1` and its [release-lock.json](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/release-lock.json), which pins core `0.1.0a13` and the public wheel digest. After obtaining the template code, run the following from the template root, which is `integrations/grokbot` in a full checkout. Replace the paths with the recipient's private directories:

```sh
(
  set -eu
  umask 077
  cd /path/to/asterun-grokbot-0.1.0-rc1
  mkdir -p /path/to/private/setup
  python3 scripts/fetch_runtime.py --output-dir /path/to/private/downloads \
    > /path/to/private/setup/fetch-result.json
  ASTERUN_WHEEL=$(python3 -c 'import json, sys; result = json.load(open(sys.argv[1])); assert result["ok"] is True; print(result["data"]["path"])' \
    /path/to/private/setup/fetch-result.json)
  python3 -m venv /path/to/private/venv
  /path/to/private/venv/bin/python -m pip install "$ASTERUN_WHEEL"
  /path/to/private/venv/bin/asterun version
)
```

`fetch_runtime.py` downloads and verifies the pinned wheel, returning `data.path`, `sha256`, and `installed: false`. The commands above then explicitly create a venv, install the returned path, and check the version. An existing file with the matching digest is reused. Handle download or validation failures according to the returned error. `scripts/package.py` is a development release tool; recipients do not need to run it.

Discover the actual executable and connection method. Compare `asterun version` with the version returned by core `diagnose`, then read `backend-inspect` for the selected backend. Check installation, login, live invocation, and client visibility separately. Preserve correct configuration; use configuration revisions and the existing backup and recovery procedures for changes.

Bind the code workspace to the real project directory or a prepared Git worktree, and verify that the Asterun workspace root matches it. For non-code tasks, bind the user's chosen directory and set `allow_non_git: true` when appropriate. Record the account, native HOME, workspace, and core against the actual execution host.

Run the [offline example](https://github.com/zonzideka/asterun/blob/v0.1.0a13/examples/non-code/README.en.md) first to check submission and result reads. Perform live backend checks within the user's authorization and record whether a model was called and what happened.

After installing the supporting code and runtime, follow the [adapter CLI](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.en.md#adapter-cli) to save a private binding with `init`. Specify `--ledger`, the Asterun executable, core configuration and state directory, execution location, allowed workspaces and backends, and the current chat destination. Use `--location host-cloud` for the cloud computer or `--location local` for local execution. Use `init` to check installation and configuration, and record backend login and invocation from actual results. If a named binding changes, create a new binding and preserve old task associations.

Use `update_state` with `target=routine` and `action=create` to create the binding's routine in the original chat, with the standard five-field cron schedule `*/5 * * * *`. Use `update` or `resume` for an existing routine. Each wake runs one small batch of `poll` and `inbox` on the same execution host. A minimal probe using this cron form fired automatically and displayed a marker in the original chat.

Send through `SendToUser` only after `claim` returns `ok=true` and `data.already_claimed=false`. Set `type=text` and send `data.content` unchanged. Use the host's returned message ID and `data.content_sha256` in `ack` to record `host_reported_delivered`. When `status` returns `needs_followup=false`, pause the routine with `update_state`, `action=pause`; resume after a new submission. See the [task skill](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/skills/tasks/SKILL.md) for the complete sequence.

Cloud installation records cover copying material, verifying digests, downloading the pinned a13 wheel, installing it in a separate Python 3.13.5 venv, and reading its version. Complete backend invocation, task delivery, recovery after closing the chat, and independent recipient checks through the [acceptance record](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/acceptance.md).

Store setup records in the recipient's private directory: versions, extraction root, invocation method, execution location, workspace, backend, routine/chat association, and individual check results. Shared text contains generic configuration examples and public installation references.
