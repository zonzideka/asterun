# 审批策略与批处理

[中文](approval-policy.md) | [English](en/approval-policy.md)

默认使用人工审批。需要按规则处理时，由管理员在实例配置中设置 `approval_policy`，再通过 `approval-apply-policy` 或批次中的 `decision=policy` 应用。`review-watch --apply-policy` 可持续处理匹配请求。

新 Codex PR 审查默认使用[固定文件读取](fixed-read-scope.md)，按任务绑定的范围读取文件。下文策略用于原生命令审批，显式 shell 包装仍交回人工。

## 配置规则

将以下片段合并到实例配置，替换目录、有效期、输入和程序摘要：

```json
{
  "approval_policy": {
    "mode": "bounded",
    "rules": [
      {
        "id": "review-fixed-diff",
        "workspace": "review",
        "backend": "codex",
        "cwd": "/srv/asterun/review-workspace",
        "expires_at": "2030-01-01T12:00:00Z",
        "input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "argv": [
          "/bin/cat",
          ".asterun-reviews/rev_00000000000000000000000000000000/pr.diff"
        ],
        "executable_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    ]
  }
}
```

每条规则按完整 argv、工作区、后端、cwd、有效期和任务输入匹配。示例中的字段全部必填，ID 须唯一，最多配置 100 条。后端须为 Codex，`argv[0]` 使用程序绝对路径，评估时重新核对 SHA-256。有效期填写带时区的实际截止时间。

`input_hash` 取准备结果的 `task_input_hash`，也可从 `STATE_DIR/reviews/REV_ID/manifest.json` 读取。普通任务使用 `task.input_hash`。程序摘要可这样计算：

```sh
python3 - /bin/cat <<'PYCODE'
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PYCODE
```

默认读取范围为工作区，额外目录通过 `read_roots` 指定；cwd 和解析后的文件路径须位于这些范围。将操作者审核过的命令写入实例配置后，规则才生效。

PR 审查先执行 prepare 取得摘要，再写规则、递增 revision、重启并应用配置。随后用原参数再次 prepare，使清单绑定新修订，最后 submit。已尝试提交的清单保持原绑定。配置操作见[安装说明](install.md)。

## 支持的命令

策略先核对完整 argv，再检查命令形式。自动匹配限于无展开、无组合的简单命令。`sh -c`、`bash -c`、`zsh -lc`、管道、重定向、命令替换、通配符、复合语句和包装脚本保留人工处理。

`pwd` 接受无参数或 `-P`；`cat` 接受普通文件路径，可在路径前带 `--`。Git 支持下列完整参数序列：

```text
git --no-pager -c core.fsmonitor=false -c core.untrackedCache=false status --porcelain=v1 --untracked-files=no
git --no-pager -c core.fsmonitor=false -c core.untrackedCache=false rev-parse --verify HEAD
```

GitHub 规则还需 `host=github.com`、`repository=owner/repo`、正整数 `pr` 和 40 位小写十六进制 `head_sha`。可用命令示例：

```text
gh pr view 55 --repo github.com/owner/repo --json number,headRefOid
gh pr diff 55 --repo github.com/owner/repo --color never
gh api --hostname github.com --method GET repos/owner/repo/pulls/55/files
```

规则中的程序路径仍须为绝对路径。`pr view` 的 JSON 字段限 `number,title,body,state,author,headRefOid,baseRefOid,files,url`。`api` endpoint 限 PR、PR files、指定 commit 和指定 Git tree。方法和 hostname 必填；额外查询参数、分页、输入文件、GraphQL、模板和浏览器操作需人工处理。评估中的 `remote_head_verified=false` 表示该步骤只检查规则，PR 版本由配方核对。

当前规则处理 Codex `item/commandExecution/requestApproval`。文件修改、额外权限、未知字段及其他原生请求交回对应入口处理。应用规则时，同时核对 Grant、调用主体、配置修订、审批时效和运行状态。

## 评估与答复

从 `task-get` 的 `approval` 取得 ID、task/run 和 `target_hash`：

```sh
asterun --state-dir /path/to/state --connect approval-assess APPROVAL_ID \
  --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
asterun --state-dir /path/to/state --connect approval-apply-policy APPROVAL_ID \
  --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
```

`assess` 评估并记录审计；`apply-policy` 重新核对后转发匹配决策。未匹配时返回 `manual_required=true`、`forwarded=false`。MCP 对应 `approval_assess` 和 `approval_apply_policy`。

人工答复使用 `approve` 或 `deny`，建议始终携带完整绑定：

```sh
asterun --state-dir /path/to/state --connect approval-respond APPROVAL_ID \
  --decision deny --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
```

## 批处理

`approval-batch --request FILE` 处理明确列出的 1-100 项当前请求。每项包含审批 ID、预期绑定和 `decision=approve|deny|policy`：

```json
{
  "items": [
    {
      "approval_id": "APPROVAL_ID",
      "expected_task_id": "TASK_ID",
      "expected_run_id": "RUN_ID",
      "expected_target_hash": "CURRENT_TARGET_HASH",
      "decision": "policy"
    }
  ]
}
```

批次先校验结构，再逐项处理。请逐项查看 `results` 中的 Envelope；有错误或需要人工处理时，`all_succeeded=false`，CLI 退出码为 1。已成功发送的项保留原结果，后续请求另行处理。

## 不确定结果

发送前保存 `FORWARDING` 意图和运行级审批摘要。`response_status=sent_unconfirmed` 表示已发送但尚无原生确认；`send_result_unknown` 表示转发结果待对账。此时读取任务、事件和原生状态，保留原审批记录。

审批的 `approved` 记录决策发送状态，命令和任务结果继续通过运行事件观察。恢复时按具体请求处理，避免重放包含已发送项的整批。
