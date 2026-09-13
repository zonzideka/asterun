# ADR 0003：Codex 发现路径与真实派发开关

日期：2026-09-07。状态：已采纳（A1-04）。

## 决策

产品代码不再默认 `/Applications/Codex.app` 或作者家目录。发现顺序是配置可选 `bin`、`CODEX_BIN`、`PATH` 上的 `codex`。客户端名是 `asterun`。

`backend.inspect` 与默认 `connection.verify` 只做安装发现，不 spawn App Server，不调用 `account/read`。真实派发要求配置启用、找到可执行文件，并且 `ASTERUN_RUN_CODEX=1`。缺二进制返回 `BACKEND_UNAVAILABLE`；有二进制但未开开关返回 `AUTH_REQUIRED`。网页登录不能单独证明 CLI/App Server 可用。

适配器支持与现场验证分开写：Codex 的 `adapter_support=supported`，账户、真实任务、续接、取消、Desktop 可见均为 `not_tested`。

## 回退

`build_backends` 把 `kind=codex` 改回 `DisabledBackend`，并删除 live 开关。协议模块可保留作对照，不再从应用服务调用。
