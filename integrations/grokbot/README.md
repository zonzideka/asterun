[中文](README.md) | [English](README.en.md)

# Asterun Bot 模板

本模板让 GrokBot 通过 Asterun 调用用户选择的 Agent，跟踪任务，并把结果发回发起任务的聊天。用户可以只用一个 Agent 处理文本、文档或开发任务，也可以自行安排实现、检查、审查和其他步骤。Agent 与工具权限按任务选择。

模板由 [Bot 配置文案](bot-profile.md)和三个操作技能组成：[安装接入](skills/setup/SKILL.md)、[任务跟踪](skills/tasks/SKILL.md)、[恢复与接管](skills/recovery/SKILL.md)。模板版本为 `0.1.0-rc1`，核心版本锁定为 `0.1.0a13`，见 [release-lock.json](release-lock.json)。本目录包含模板文案和配套 CLI。50 项模板离线测试已通过，实际宿主与任务交付按场景记录，见下文。

## 取得模板

模板包固定发布入口为[版本发布页](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1)。从该页取得 `asterun-grokbot-0.1.0-rc1.zip` 和 `SHA256SUMS`，按 `SHA256SUMS` 核对完整 ZIP 后再解压。包内 `manifest.json` 记录源码 commit、各发布文件的 SHA-256 和字节数。下载后记录包的版本和校验结果。

保存解压根目录的绝对路径，后续安装、任务命令和 routine 均从该目录调用 `scripts/`。将技能导入宿主全局技能库时，保留正文中的固定标签公开文档链接，并继续使用安装记录中的解压根目录。README 和配置文案保留包内相对链接，便于随包阅读。

## 选择执行位置

| 路径 | 安装与调用方式 | 工作区和账户 |
|---|---|---|
| GrokBot 云电脑 | 使用不带 `machineId` 的 `Shell`，在选定云电脑安装并调用 Asterun、模板适配器和所需后端 | 使用该主机上的工作区、原生登录和私有状态 |
| 用户本机 | 先通过 `ListMachines` 选择用户机器，再用携带该 `machineId` 的 `Shell` 安装或调用已有实例 | 使用本机真实目录、原生登录和私有状态 |

首次接入先确认执行位置，需要传输安装材料时按用户选择使用 `CopyToBox` 或 `CopyFromBox`。已有可用 Asterun 实例可直接复用；新环境按[安装接入技能](skills/setup/SKILL.md)运行 `scripts/fetch_runtime.py --output-dir ABSOLUTE`，核对返回后在独立 venv 安装对应 wheel，再检查 CLI、核心版本和后端配置。该下载脚本校验 release-lock 中的 SHA-256，安装由随后显式执行的 pip 完成。Bot 分享传递模板文案，接收方还需取得公开模板包，在自己的执行环境安装代码、完成登录和配置。

编码任务的工作区指向执行主机上的真实项目目录。需要隔离文件修改时，先创建 Git worktree，再绑定该实际目录。文档和其他非代码任务使用用户指定的目录，配置非 Git 工作区时设置 `allow_non_git: true`。远端仓库按所选后端的工作方式准备，不以模板自带目录代替用户项目。

## 配套 CLI

以下接口由 `scripts/bridge.py` 提供。以下命令先进入安装记录保存的模板根目录再运行，不依赖宿主技能库的当前目录：使用完整 Git 仓库时先进入 `integrations/grokbot`，使用分享包时进入解压后的模板目录。将路径、后端、工作区及目的地换成接收方核验的值。适配器使用标准库，`--ledger` 指向本实例的私有跟踪记录。命令行采用以下固定约定，stdout 返回单个 JSON Envelope。

```sh
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  init --binding primary --asterun /absolute/venv/bin/asterun \
  --config /absolute/instance/config.json --core-state /absolute/instance/state \
  --location local --workspace PROJECT --backend AGENT \
  --destination CHAT_ID_FROM_HOST
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  submit --binding primary --request-id REQUEST_ID \
  --workspace PROJECT --backend AGENT --input /path/to/prompt.txt
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  poll --binding primary --limit 3
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  inbox --binding primary --limit 3
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  status --binding primary
```

云电脑使用 `--location host-cloud`。`init` 的 `--workspace`、`--backend` 可重复指定。`submit --input -` 从 stdin 读取输入，需要续接时添加 `--conversation-id` 并由 Asterun 校验绑定。请求重试沿用 `REQUEST_ID`，聊天目的地从当前宿主接口取得并固定。

先使用 `claim` 领取具体消息。仅在 Envelope 的 `ok=true` 且 `data.already_claimed=false` 时，调用 `SendToUser`，设置 `type=text`，将 `content` 原样取自 `claim` 的 `data.content`。取得宿主返回的实际消息 ID 后提交 `ack`：

```sh
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  claim --binding primary --delivery-key DELIVERY_KEY
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  ack --binding primary --delivery-key DELIVERY_KEY \
  --message-id HOST_MESSAGE_ID --content-sha256 CONTENT_SHA256
```

`ack` 的 `--message-id` 使用 `SendToUser` 返回的不透明消息 ID，`--content-sha256` 使用 `claim` 的 `data.content_sha256`。记录状态为 `host_reported_delivered`，聊天显示按实际观察确认。`already_claimed=true` 时跳过发送。领取后的发送结果不明时保持 `sending`；当前宿主尚无可用历史查询入口，需实际核对或由用户明确确认未发送，再用同一账本执行 `resolve --binding primary --delivery-key DELIVERY_KEY --not-sent`，随后重新领取。

## 跟踪和交付

每次提交后，保存返回的 task/run/conversation 引用及当前聊天关联。长期任务由常驻 Asterun 核心执行。通过 `update_state` 的 `target=routine`、`action=create` 创建原生 routine，使用标准五段 cron `*/5 * * * *`；已有 routine 按需 `update` 或 `resume`。该计划已在最小探针中自动触发，并在原聊天显示标记消息。

routine 使用原聊天和同一 `binding`。每次唤醒在已绑定的执行主机运行一次 `poll --limit 3`，再读取 `inbox --limit 3`。按上节的 `claim`、`SendToUser`、`ack` 顺序处理本批消息。普通状态未变化时保持安静，结束本次观察后等待下一次唤醒。

处理后读取 `status`。`needs_followup=true` 时保留 routine，包含正在执行、待交付或 `sending` 未决记录；为 `false` 时，通过 `update_state` 的 `target=routine`、`action=pause` 暂停该绑定的 routine。提交新任务后恢复。用户主动结束跟踪时保留原任务和交付记录。

routine、聊天关联、本机选择、模板解压根目录和私有账本均在接收方环境创建或记录。`Shell` 的执行位置、`SendToUser` 的当前聊天以及绑定中的目的地保持对应。

## 使用范围和验收

Bot 按用户目标组织流程。需要 PR 审查时使用[审查配方](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/pr-review.md)，需要确定性检查或独立质量流程时采用已配置的验收步骤。普通任务直接使用任务接口。

分享内容保留通用文案、公开安装引用和示例。账户凭据、原生会话、真实项目路径、运行记录、routine 标识和聊天绑定留在各自私有环境。

接线验收从新环境的离线任务开始，再在授权范围内验证实际后端、routine 再次唤醒、同聊天交付和异常恢复。独立接收方验收待安排。已验证三文件安装材料复制到云电脑并校验 SHA、下载固定 a13 wheel、在 Python 3.13.5 独立 venv 安装并读到版本。后端调用、完整任务交付、关闭聊天后的恢复及独立接收方验收继续按实际场景验证，分层记录结果。后端能力见[发行说明](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/release-v0.1.md)。
