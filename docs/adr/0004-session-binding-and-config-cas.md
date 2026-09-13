# ADR 0004：会话绑定与配置 CAS

日期：2026-09-07。状态：已采纳（A2-01 离线）。

## 决策

状态库保存 `SessionBinding`：`conversation_id`、后端、工作区、`account_ref`、`runtime_ref`、可选原生会话 ID、最近任务。账户与主机来自进程环境 `ASTERUN_ACCOUNT_REF` / `ASTERUN_RUNTIME_REF` 或构造参数，不接受请求 JSON 自报。

`session.resume` 先查本地索引再核对绑定。账户或主机不一致返回 `BINDING_MISMATCH`，不返回可复用的原生线程去重放。本地索引续接与原生线程核对分开记录；本轮默认 `native_checked=false`、`native_resumed=false`。

配置文件 revision 与状态库 `applied_config_revision` 分开。首次打开空库时写入当前文件 revision。文件被改高后，副作用调用返回 `REVISION_CONFLICT`，直到 `config.apply` 用期望的已应用值做 CAS。apply 不改写用户的 `config.json`。

`Grant.expires_at` 过期后副作用返回 `AUTH_REQUIRED`，不派发后端。

## 回退

删除 sessions 表与 `applied_config_revision` 后，`session.resume` 改回缺少索引时的拒绝。已有任务行可保留。把 schema 从 2 降回 1 需要备份后重建，不自动降级。
