# ADR 0006：Grok/Claude 发现路径与握手不等于续接

日期：2026-09-07。状态：已采纳（A2-03 离线）。

## 决策

产品代码不默认作者家目录或 Homebrew 路径。发现顺序是配置可选 `bin`、`GROK_BIN`/`CLAUDE_BIN`、PATH 上的同名命令。模型缓存只在 `GROK_MODELS_CACHE` 显式给出时读取，导入期不读 `~/.grok`。

`backend.inspect` 与默认 `connection.verify` 只做安装发现，不 spawn。真实派发要求配置启用、找到可执行文件，并且分别设置 `ASTERUN_RUN_GROK=1` 或 `ASTERUN_RUN_CLAUDE=1`。

Grok ACP 的 `initialize`/`loadSession` 只是握手声明，`resume_session` 的适配器支持保持 unsupported，验证状态 `not_tested`。Claude 只做 `claude -p` 一次性调用，续接明确 unsupported。递归限制使用 `asterun`，不带作者项目 MCP deny 名单。

## 回退

`build_backends` 把 `kind=grok|claude` 改回 `DisabledBackend`。协议模块可保留作对照。
