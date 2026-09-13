[中文](README.md) | [English](README.en.md)

# Jules REST 插件

本插件调用 Jules REST v1alpha，采用 Apache-2.0，独立 wheel 仅依赖 Python 标准库。API 来源见 `upstream-lock.json`。使用前先在 Jules 中授权目标仓库；任务按远端 source 和分支执行。

## 配置并提交任务

按核心 v2 注册 `google.jules`。runner 使用插件环境中 Python 的绝对路径，并附加 `-m asterun_plugin_jules.worker`；同时固定 manifest、wheel、runtime 和 runner 摘要。连接使用 `auth_mode=user_api_key`、`upstream_version=v1alpha`，通过 provider_account 引用注入 `JULES_API_KEY`。

options 包含 `execution_enabled=true`、已授权的 `source`（如 `sources/github-org-repo`）、`starting_branch` 和 `state_root`。`state_root` 使用工作区之外、归本用户所有的 0700 目录。目标分支记录为 `mutable_branch`，会随远端更新变化。

`task.submit` 创建需要计划批准的任务，返回原生 session 和 URL。使用 `task.reconcile` 读取会话和活动：等待计划批准或反馈时返回等待输入，排队或运行中的任务继续观察。

## 审阅计划和发送反馈

通过 `capability.invoke` 的 `remote.plan.inspect` 读取计划，输入为 session。批准时调用 `remote.plan.approve`，传入同一 session 和 `plan_sha256`。反馈使用 `remote.message.send`，输入为 session 和 text。每项操作都会核对已绑定的账户、工作区和仓库。

批准前会重读最新计划和源分支；摘要变化时需重新审阅。上游批准接口没有条件摘要参数，复核与提交之间仍可能发生竞争。

## 观察和恢复

每次观察最多读取两页，并持久保存游标。批准最多读取八页，分页不完整时暂停。会话轮询至少间隔五秒；429/503 的 Retry-After 按账户保存，最多等待一小时。

插件在 POST 前保存意图。创建、审批或反馈的回执丢失时，结果保留 `unknown`，由操作者核对原生状态后处理。`state_root` 保存原生回执和绑定，恢复时需与核心状态一并保留。

取消、账户额度和真实费用读取尚未接入。预算使用核心配置的本地上限和未知额度策略。

## 验证状态

默认测试使用离线 REST fixture，生产端点固定为 `https://jules.googleapis.com/v1alpha/`。本地模拟需设置 `offline_fixture=true`，并使用固定的无效测试 key。

真实账户与仓库授权、Jules 扣量、托管运行、代码结果、质量审查和现场恢复待验收。
