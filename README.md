[中文](README.md) | [English](README.en.md)

# Asterun

Asterun 在你选择的电脑或服务器上执行 Agent 任务。通过 CLI、MCP 或其他应用提交后，核心负责后台运行、会话关联、审批、结果记录和故障恢复。后端使用执行主机上已授权的账户。

当前版本为 `0.1.0a13`，采用 [Apache-2.0 许可证](LICENSE)。首版范围已冻结，已实现能力和验证情况见[发行说明](docs/release-v0.1.md)。

## 开始使用

准备 Python 3.11 或更新版本，在虚拟环境中安装下载的 wheel：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
source .venv/bin/activate
```

先运行[非代码样例](examples/non-code/README.md)，确认离线提交和验收流程；然后按[安装说明](docs/install.md)配置工作区、后端和账户。长期任务交给常驻核心执行：

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

在另一终端连接同一实例。将工作区和后端名换成配置中的值，再提交任务：

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend BACKEND --input - \
  --idempotency-key first-task < /path/to/prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID --timeout 60
```

MCP 客户端使用 `asterun --state-dir /path/to/state --connect mcp`。观察到期或客户端关闭后，核心继续执行；保存返回的任务 ID，后续用它查询进度和结果。

## Agent 能力

| Agent | 当前支持的能力 |
|---|---|
| Codex | 文件读取与编辑、命令和测试执行、原生会话续接、命令与文件变更审批、轮次中断和结果对账；可选[固定文件读取](docs/fixed-read-scope.md)和[桌面项目关联](docs/codex-projects.md) |
| Grok | 文本任务、文件读取与编辑、命令和测试执行、多轮迭代；按[执行配置](docs/install.md#grok-自主编码配置)启用相应工具 |
| Claude | 单次任务调用、文本处理和工作区文件读取；支持传入固定文件快照 |
| Antigravity | 文本任务和工作区文件读取，提供[内置 CLI 适配](docs/antigravity.md)与[独立账户插件](docs/plugins.md) |
| Cursor 插件 | 本地 Agent 任务执行、会话恢复和取消 |
| Jules 插件 | 托管代码任务、计划读取与确认、任务反馈和状态查询 |

用户按任务选择 Agent、工具权限和协作流程。各后端的配置条件与验证状态见[发行说明](docs/release-v0.1.md)，可选后端按[插件说明](docs/plugins.md)独立安装。

## 使用与集成

[用户手册](docs/user-manual.md)说明提交、观察、审批和恢复。[PR 审查](docs/pr-review.md)提供一套可选工作流示例，包括固定代码版本、读取结构化结论、关联复审和交付结果。

GrokBot 等调用方可以在云电脑中部署 Asterun，也可以通过已有授权通道调用本地进程。工作区、账户和原生会话随执行主机保存。普通任务由调用方跟踪任务和事件；[审查结果交付模块](docs/review-consumer.md)用于将审查收件箱接入宿主消息发送与定时调度。

[Asterun Bot 模板](https://x.ai/bot/4r3ysO1eA-6ZLI6cRoTNU)提供 GrokBot 的安装引导、任务跟踪和聊天交付，作为可选接入独立发布。[安装说明与配套代码](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.md)按固定版本提供。

接口集成查阅 [runner-control/v1](docs/protocol/IMPLEMENTATION.md) 和 [capability profile](docs/capability-profile.md)。
