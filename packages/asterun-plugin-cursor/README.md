[中文](README.md) | [English](README.en.md)

# Cursor local Agent 插件

本插件在独立 venv 中调用 Cursor local Agent，采用 Apache-2.0，固定依赖 `cursor-sdk==1.0.31` 和 `httpx==0.28.1`。上游 API 与 wheel 摘要见 `upstream-lock.json` 和 [Cursor Python SDK 文档](https://cursor.com/docs/sdk/python)。

## 配置连接

按核心 v2 注册 `cursor.agent`。runner 使用插件环境中 Python 的绝对路径，并附加 `-m asterun_plugin_cursor.worker`；同时固定 manifest、wheel、安装树和 runner 摘要。连接使用 `auth_mode=user_api_key`、`upstream_version=1.0.31`，通过 provider_account 的 `file:` 或 `env:` 引用注入 `CURSOR_API_KEY`。计费池由用户明确配置。

options 需填写 `execution_enabled=true`、`runtime=local`、账户可用的 `model`、`tools`、`disallowed_tools`、`mcp_servers={}`、`setting_sources=[]`、`state_root`、`bridge_bin` 和 `bridge_sha256`。`state_root` 使用工作区之外、归本用户所有的 0700 目录。bridge 使用安装包中已核对的实际文件和摘要，路径须为普通文件且禁止其他用户写入。超时默认 15 秒，上限 20 秒，同时受宿主 RPC 总期限约束。

最小文本调用设置 `tools=[]`、`disallowed_tools=["Shell"]`。每次创建或续接会话都会重新应用工具策略；配置变化后，旧会话的续接会被阻止。当前 MCP 配置须为空。SDK bridge 使用 local 运行方式，插件以当前 OS 用户权限执行。

## 续接、观察和恢复

插件提供 `task.submit`、`task.get`、`session.resume`、`task.reconcile` 和 `task.cancel`。`session.resume(native=true)` 核对同一 Agent、最近 Run、工作区和模型，返回 `resume_ready`；下一次带 conversation 的提交执行续接。

原生 ID 和连接锁会持久保存。结果不明时保留 `unknown`；取消操作需回读到 `cancelled` 才能确认。SDK Run 的累计 token 按稳定游标去重，并保留原始统计范围。备份时分别保留核心状态和 SDK `state_root`。

## 验证状态

默认测试使用已安装 wheel、SDK 替身和本地 HTTP/Connect 模拟器。设置 `ASTERUN_TEST_CURSOR_WHEELHOUSE` 后，可在隔离环境安装固定的真实 SDK 并连接模拟器。

真实账户、官方 bridge、工具权限、计费、Desktop 显示及 Linux 待验收。cloud、产物下载、交互审批、非空 MCP 和账户额度读取尚未接入。
