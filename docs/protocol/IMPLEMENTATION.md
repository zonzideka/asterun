# runner-control/v1 本机实现

[中文](IMPLEMENTATION.md) | [English](../en/protocol/IMPLEMENTATION.md)

Asterun 通过 CLI、stdio MCP 和同用户 Unix socket 提供 runner-control/v1 的本机 profile。本机任务使用 `bot_instance_id: null`，核心可以独立运行。本文说明已接入的行为；[原始草案](runner-control-v1/SPEC.zh-CN.md)保留完整目标契约，架构取舍见 [ADR 0011](../adr/0011-runner-control-profile.md)。

## 版本与发现

`schema_version` 为 `runner-control/v1`，`draft_revision` 为 `2026-09-10.draft1`。以下文件保留原始字节，读取时核对摘要。

| 文件 | SHA-256 |
|---|---|
| [JSON Schema](../../src/asterun/schemas/runner-control-v1.schema.json) | `462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76` |
| [状态机](runner-control-v1/state-machines.json) | `99ceb7f0b8a30160d06085d0c2b645e618506a393941a7ebf78ff5c3bfa5ebb6` |

包内路径为 `asterun/schemas/runner-control-v1.schema.json` 和 `asterun/schemas/state-machines.json`。客户端同时核对草案修订与摘要：`feature_profiles` 包含 `asterun-local-v1` 和 `schema-sha256:<摘要>`，辅助接口 `control.resources` 另返回 `schema_sha256`。原始 `DiscoveryResponse` 没有摘要字段且禁止扩展，因此沿用上述表达。

`backend_ids` 列出本机配置的后端。安装、登录、调用和客户端显示的验证结果由后端状态与现场记录提供。

## 支持范围

| 能力 | 本机实现 |
|---|---|
| `task.create`、`task.amend` | 创建独立 Task，要求至少一个必需验收条件。amend 可修改 READY 任务的目标、约束和验收条件，资源范围保持 |
| `run.start`、`run.retry` | 使用 `wf_execute` revision 1 和默认后端，核对 Task revision、配置、账户与运行环境摘要。retry 为最近失败的 Run 创建新 Run |
| `run.pause`、`run.resume`、`run.cancel` | 保存期望状态并等待安全边界；resume 暂未接入 Capsule 恢复，cancel 依据后端证据确认终止 |
| `run.takeover`、`run.return_control` | 支持 human 接管和归还，外部编排者接管待接入 |
| `state.put` | 写入 `domain.*`，校验主体、CAS 和跨对象证据引用，原子更新值摘要、revision 与事件 |
| `artifact.register` | 注册已授权的暂存上传，核对 Task/Run/Assignment 归属、SHA 和字节数；输入使用暂存引用，结果标记 unverified |
| `grant.revoke` | 核对 Grant revision 后撤销，派发前再次检查 |
| 资源 | 每任务绑定一个完整的已配置工作区，`operations: ["agent.execute"]`、`selector: null`；上下文 `enforcement: advisory` |
| 预算 | 接受 `turns`、`hard`、`billing_pool_ref: null`；其他硬计量限制返回不支持 |
| 验收 | `val_execution` 检查执行正常结束，`val_output` 另检查公开输出非空。原代码质量流程通过其专用入口运行 |
| 身份 | 固定 `ns_local`，主体由进程建立，再经工作区策略和 Grant 校验 |
| 事件 | 按 Task 持久递增序号分页，单页上限 100；过旧和未来游标返回对应错误 |

`message.send`、`action.propose`、`approval.request`、`setup.answer`、`setup.verify` 在 Schema 中定义，运行能力尚待接入，发现接口会省略这些命令。远程租户身份、HTTP 路由和 SSE 服务也在完整草案范围内。

human 接管协调本机编排所有权并等待受管轮次结束。完整工作区 Capsule、资源哈希和外部原生客户端活动检测仍待接入，草案 C26 的完整现场归还条件尚未验收。

`cap_submit_turn` 的 Action 记录一次后端轮次提交。供应商内置工具动作由原生权限控制，尚未逐项进入本层 Action/outbox。执行成功和公开输出检查提供执行层证据；业务质量由任务配置的检查与独立审查确认。

## 编码、幂等与状态库

入口使用严格 UTF-8 JSON，校验重复键、Unicode、有限数、JavaScript 安全整数、未知字段、枚举、UTC 时间及字段范围。浮点数按 IEEE 754 和 RFC 8785 处理；微美元金额使用整数。

命令最多 1 MiB，MCP/IPC 的完整消息同样最多 1 MiB，包含封装开销。文本上传和直接内容读取最多 512 KiB。

幂等域为命名空间、认证主体、方法和 `idempotency_key`。输入摘要由 RFC 8785 JCS 编码的 `schema_version`、`method`、`params`、`expected_revision`、`expires_at` 计算 SHA-256。重发时保留原语义和到期时间，可以更换传输 `request_id`。授权检查先于幂等回读；到期和 CAS 检查只针对新命令。通信中断后先读取原 Operation 或幂等记录。

标准实体、Operation、事件、预算、Grant、租约、outbox、内容 blob 和旧记录共用 SQLite。事务受理和结果保存分开执行，等待外部调用时释放事务。未知结果保留 `UNKNOWN_REMOTE` 及未确认预算占用。需要恢复的任务使用显式状态目录或常驻核心。

当前数据库为 schema 6。schema 1–5 升级前创建 `asterun.schema-<旧版本>.*.sqlite` 一致快照，再初始化当前控制表和插件表。旧 Task/Run 保留原含义。`state-backup` 包含状态表与 blob，恢复预检校验结构和内容哈希；工作区文件、用户凭据及原生会话目录由各自备份负责。回退时保留新库用于对账，并用升级前快照的独立副本配合旧版本运行。

发现中的 `event_retention_seconds: 86400` 表示最低保留声明。当前事件持续保存在数据库中，自动清理尚待接入；内部裁剪边界用于识别过旧游标并返回 `CURSOR_EXPIRED`。

## 调用入口

| 用途 | CLI | MCP 工具 |
|---|---|---|
| 发现与资源 | `control-discovery`、`control-resources` | `control_discovery`、`control_resources` |
| 标准命令 | `control-command --request <JSON文件>` | `control_command`，参数为 `{"command": <标准信封>}` |
| 实体与事件 | `control-get <Kind> <id>`、`control-events <task_id> --after <序号>` | `control_get`、`control_events` |
| 上传与内容 | `control-upload <task_id> --request <含text的JSON文件>`、`control-content <artifact_id>` | `control_upload`、`control_content` |

标准数据位于 Asterun 外层 `Envelope.data`，错误位于 `Envelope.error`。MCP 的 JSON-RPC tools/call 响应将 Envelope 作为 JSON 文本放在 `result.content` 中；stdio 按行传输 JSON。命令返回后继续读 Run/Task，确认业务执行状态。

长期任务使用 `asterun --config <配置> --state-dir <目录> serve`，调用方加 `--connect`；stdio MCP 也可拥有常驻执行器。单次 CLI 只推进当次可执行阶段。

## 离线样例

在仓库根目录创建环境，然后将下面的 Python 代码保存为 `/tmp/asterun-control-demo.py`。脚本为每次运行创建临时工作区与状态目录，使用 fake 后端，并在退出时清理。

```sh
python3 -m venv /tmp/asterun-demo-venv
/tmp/asterun-demo-venv/bin/pip install -e '.[test]'
```

```python
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

transport = os.environ.get("ASTERUN_DEMO_TRANSPORT", "cli")
assert transport in {"cli", "mcp"}
with tempfile.TemporaryDirectory(prefix="asterun-control-demo-") as directory:
    root = Path(directory)
    workspace = root / "workspace"
    workspace.mkdir()
    config = root / "config.json"
    config.write_text(json.dumps({
        "schema_version": 1, "revision": 1,
        "workspaces": {"demo": {"root": str(workspace), "allow_non_git": True}},
        "backends": {"fake": {"kind": "fake", "enabled": True}},
        "default_backend": "fake",
    }), encoding="utf-8")
    env = dict(os.environ, ASTERUN_ACCOUNT_REF="account:demo", ASTERUN_RUNTIME_REF="host:demo")
    base = [sys.executable, "-m", "asterun", "--config", str(config),
            "--state-dir", str(root / "state")]
    process = None
    rpc_sequence = 0

    def rpc(method, params):
        global rpc_sequence
        rpc_sequence += 1
        message = {"jsonrpc": "2.0", "id": rpc_sequence, "method": method, "params": params}
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert "error" not in response, response
        return response["result"]

    def call(method, arguments=None):
        arguments = arguments or {}
        if transport == "mcp":
            result = rpc("tools/call", {"name": method.replace(".", "_"), "arguments": arguments})
            envelope = json.loads(result["content"][0]["text"])
        else:
            args = [method.replace(".", "-")]
            if method == "control.command":
                request = root / "command.json"
                request.write_text(json.dumps(arguments["command"], ensure_ascii=False), encoding="utf-8")
                args += ["--request", str(request)]
            elif method == "control.get":
                args += [arguments["kind"], arguments["id"]]
            elif method == "control.events":
                args += [arguments["task_id"]]
            completed = subprocess.run(base + args, env=env, text=True, capture_output=True)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            envelope = json.loads(completed.stdout)
        assert envelope["ok"], envelope.get("error")
        return envelope["data"]

    def command(method, params, revision=0):
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        return call("control.command", {"command": {
            "schema_version": "runner-control/v1", "request_id": "req_" + uuid4().hex,
            "idempotency_key": uuid4().hex, "expected_revision": revision,
            "expires_at": expires.isoformat().replace("+00:00", "Z"), "method": method, "params": params,
        }})

    try:
        if transport == "mcp":
            process = subprocess.Popen(base + ["mcp"], env=env, text=True,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "asterun-control-demo", "version": "1"}})
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
        found = call("control.discovery")
        assert found["draft_revision"] == "2026-09-10.draft1"
        expected_sha = "462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76"
        assert "schema-sha256:" + expected_sha in found["feature_profiles"]
        resource = call("control.resources")["resources"][0]
        created = command("task.create", {
            "title": "协议离线示例", "objective": "输出一段 fake 验证结果", "bot_instance_id": None,
            "resource_scopes": [{key: resource[key] for key in ("resource_id", "operations", "selector")}],
            "constraints": [],
            "acceptance": [{"id": "acc_demo", "validator_id": "val_output", "input_artifact_ids": [],
                            "required": True, "subject_revision_sha256": None}],
            "budget_limits": [{"meter": "turns", "limit": 1, "enforcement": "hard", "billing_pool_ref": None}],
        })
        # 在开始执行前重新读取真实 Task revision 与实际配置摘要。
        task = call("control.get", {"kind": "Task", "id": created["result_ref"]})
        resources = call("control.resources")
        started = command("run.start", {"task_id": task["id"], "workflow_id": "wf_execute",
                          "workflow_revision": 1, "configuration_sha256": resources["configuration_sha256"]},
                          revision=task["revision"])
        deadline = time.monotonic() + 10
        while True:
            run = call("control.get", {"kind": "Run", "id": started["result_ref"]})
            if run["status"] in {"SUCCEEDED", "FAILED", "CANCELED"}:
                break
            assert time.monotonic() < deadline, run
            time.sleep(0.05)
        assert run["status"] == "SUCCEEDED", run
        for evidence_id in run["completion_evidence_ids"]:
            evidence = call("control.get", {"kind": "Evidence", "id": evidence_id})
            assert evidence["validator_id"] == "val_output" and evidence["verdict"] == "PASS"
        events = call("control.events", {"task_id": task["id"]})
        print(json.dumps({"transport": transport, "run_status": run["status"],
                          "evidence_ids": run["completion_evidence_ids"],
                          "last_event_sequence": events["next_sequence"], "backend": "fake"}, ensure_ascii=False))
    finally:
        if process is not None:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
```

分别运行 CLI 和 MCP 路径：

```sh
ASTERUN_DEMO_TRANSPORT=cli /tmp/asterun-demo-venv/bin/python /tmp/asterun-control-demo.py
ASTERUN_DEMO_TRANSPORT=mcp /tmp/asterun-demo-venv/bin/python /tmp/asterun-control-demo.py
```

样例验证本机配置、标准受理、持久化、fake 派发和执行层 Evidence。真实账户、权限效果、原生续接、模型质量和客户端显示按对应后端分别验收。
