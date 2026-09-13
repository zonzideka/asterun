# 固定能力计划

[中文](capability-profile.md) | [English](en/capability-profile.md)

使用 `asterun-capability/v1` 时，先发现能力并准备固定计划，审阅后提交，再查询执行结果。该入口要求配置 v2 和 SQLite。CLI 使用 `capability-discovery` 等连字符命令，MCP 使用下划线；prepare/submit/get 请求通过 `--request` 文件传入。

## 准备与提交

prepare 核对本地注册、输入 Schema、权限、安装摘要、配置、计费策略和代码基线，返回有效期为 15 分钟的计划。计划列明输入、连接/账户引用、模型、认证、插件版本、预算估计和摘要。实际执行和预算预留从提交阶段开始。

通用调用示例，backend 与 capability 取自当前注册：

```json
{
  "schema_version": "asterun-capability/v1",
  "namespace_id": "ns_local",
  "workspace": "demo",
  "backend": "external",
  "capability": "local.echo",
  "input": {"text": "offline", "mode": "success"},
  "workflow_id": "wf_capability_call",
  "idempotency_key": "prepare-example-0001"
}
```

审阅计划后，使用 `plan_id`、`plan_sha256`、`schema_version`、`namespace_id` 和新的幂等键提交。账户、模型和输入保持计划值。每个计划对应一次执行，同键重试读取原收据。

提交事务保存标准 Task/Run、分派、授权、预留和 outbox，返回 `ACCEPTED`。常驻核心随后推进队列；单机客户端也可通过 capability-get 或标准 control 读取推进已提交记录。

## 代码工作流

使用 `workflow_id=wf_code_change`、`capability=agent.execute` 和文本 input，附加 quality：

```json
{
  "target_paths": ["src/example.py"],
  "checks": [{"kind": "file_contains", "path": "src/example.py", "text": "def example"}],
  "max_repairs": 1
}
```

`target_paths` 指定验收快照范围，执行权限来自已配置工作区。prepare 保存文件字节、模式和 Git HEAD，提交及派发前再次核对。确定性检查沿用文件存在/内容检查，随后由 QualityRuntime 执行独立审查，按 findings 修复并复核。

计划固定 reviewer、替代后端、修复上限和轮次预算。实现、审查和修复各计一个 hard turn，共享原连接预算。预算不足、reviewer 不可用、远端未知或达到修复上限时停在 `WAITING`，保留引用和 findings。

代码工作流通过验收时生成 `val_code_quality` Evidence，记录初始基线、最终快照、检查、独立审查引用和问题闭合。标准成功以该证据为条件。`capability-get` 另返回 `quality_verdict`。验收后文件变化时显示 `STALE`，历史运行保留原验收版本。通用 `wf_capability_call` 的质量状态为 `NOT_APPLICABLE`。

## 恢复与兼容

标准 control 读取同一 Task、Run、产物和 Evidence。暂停、取消与恢复沿用 runner-control/v1 和 revision CAS。暂停后继续观察原生活动轮次；取消以原生终态确认。停用插件阻止新动作，已有记录按原绑定对账。

本 profile 沿用既有 runner-control/v1 和 `wf_execute@1` 契约。当前流程已通过离线 fake 和协议替身验证，真实代码质量及独立插件的供应商调用尚待验收。各后端能力见[发行说明](release-v0.1.md)。
