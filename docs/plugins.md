# 可选插件

[中文](plugins.md) | [English](en/plugins.md)

先在独立环境中安装插件，再注册到 Asterun。内置后端继续支持 v1 配置，独立插件使用 v2。Antigravity 账户、Cursor local Agent 和 Jules REST 当前为预览包，真实供应商调用和计费尚待验收。

## 配置与迁移

v2 中，`plugins` 保存安装注册，`provider_accounts` 保存供应商身份引用，`connections` 固定认证、环境和模型，`billing_pools` 描述共享资源池，`backends` 别名指向连接。可先运行[离线 fake 示例](../examples/plugins/fake-v2.json)。

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

外部插件条目指定 `manifest_path`、`runner` argv、`installation_path` 及 SHA-256，并用 `runtime_path`/`runtime_sha256` 固定实际安装内容。采用独立 venv 或明确的非 Python runner，安装版本改变后重新注册并准备计划。插件运行在当前 OS 用户权限下，应使用受信任代码。

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

CLI 与 MCP 共用请求定义，MCP 工具名使用下划线。`usage-inspect` 显示本地预算账本和已收到的供应商观察。停用后，原主体仍可按原绑定读取、取消或对账已有任务，具体动作取决于插件能力。

## 凭据与恢复

配置中使用凭据引用：`env:NAME`、`file:/absolute/path`、`native-home:/absolute/path` 或 `credential-ref:REFERENCE`。env/file 凭据须符合 manifest 认证声明；凭据文件为当前用户所有的私有单行普通文件，路径各级须可核验。原生账户使用明确的私有 HOME，不透明引用由部署方提供解析器。

worker 启动环境包含固定 PATH、临时 HOME/TMPDIR 及授权连接注入的凭据。调用前重新核对 runner、manifest、wheel 和安装树；协议帧、输出和运行时间均有上限。秘密仅在启动时进入内存环境，原生 HOME 内容单独管理与备份。

执行请求发出后，若发生断流、超时或格式异常，保留 `pending_reconcile` 和 uncertain 预算预留。继续按原任务对账，取消结果以远端终态为准。插件事件按供应商观察记录，用量报告保留 `provider_observed` 来源。

v2 记录同时绑定读取主体、连接与运行身份。恢复时保留核心状态、配置、原生 HOME 和插件私有状态；旧 v1 历史沿用原授权。数据库降级使用兼容快照副本，并保留新版运行记录。
