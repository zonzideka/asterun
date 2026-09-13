# Approval policies and batches

[中文](../approval-policy.md) | [English](approval-policy.md)

Approvals are manual by default. To apply rules, an administrator sets `approval_policy` in the instance configuration, then uses `approval-apply-policy` or `decision=policy` in a batch. `review-watch --apply-policy` can keep handling matching requests.

New Codex PR reviews use [fixed-scope file access](fixed-read-scope.md), with files bound to the task. The rules below apply to native command approvals. Explicit shell wrappers remain subject to manual handling.

## Configure a rule

Merge this fragment into the instance configuration and replace the directory, expiry, input digest, and executable digest:

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

Each rule matches the full argv, workspace, backend, cwd, expiry, and task input. All fields shown are required. Rule IDs must be unique, with at most 100 rules. The backend must be Codex. `argv[0]` is an absolute executable path, and its SHA-256 is rechecked during assessment. Set the actual expiry with a time zone.

Use the preparation result's `task_input_hash` for `input_hash`, or read it from `STATE_DIR/reviews/REV_ID/manifest.json`. General tasks expose the same value as `task.input_hash`. Calculate an executable digest with:

```sh
python3 - /bin/cat <<'PYCODE'
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PYCODE
```

The workspace is the default read scope. Add other directories with `read_roots`; cwd and resolved file paths must fall within those roots. Rules take effect through the instance configuration after the operator reviews the commands.

For a PR review, run prepare to obtain the digest, write the rules, increment the revision, restart the core, and apply the configuration. Run prepare again with the original arguments to bind the manifest to the new revision, then submit. A manifest with a submission attempt retains its original binding. See [installation and configuration](install.md).

## Supported commands

The policy checks the full argv before validating the command form. Automatic matching is limited to simple commands without expansion or composition. `sh -c`, `bash -c`, `zsh -lc`, pipes, redirects, command substitution, wildcards, compound statements, and wrapper scripts require manual handling.

`pwd` accepts no arguments or `-P`. `cat` accepts regular file paths, optionally preceded by `--`. Git supports these complete argument sequences:

```text
git --no-pager -c core.fsmonitor=false -c core.untrackedCache=false status --porcelain=v1 --untracked-files=no
git --no-pager -c core.fsmonitor=false -c core.untrackedCache=false rev-parse --verify HEAD
```

GitHub rules also require `host=github.com`, `repository=owner/repo`, a positive integer `pr`, and a 40-character lowercase hexadecimal `head_sha`. Supported examples:

```text
gh pr view 55 --repo github.com/owner/repo --json number,headRefOid
gh pr diff 55 --repo github.com/owner/repo --color never
gh api --hostname github.com --method GET repos/owner/repo/pulls/55/files
```

The executable path in a rule must still be absolute. JSON fields for `pr view` are limited to `number,title,body,state,author,headRefOid,baseRefOid,files,url`. API endpoints are limited to the PR, PR files, a specified commit, and a specified Git tree. Method and hostname are required. Extra query parameters, pagination, input files, GraphQL, templates, and browser actions require manual handling. `remote_head_verified=false` means assessment checked the rule locally; the review recipe checks the PR version.

The current policy handles Codex `item/commandExecution/requestApproval`. File changes, additional permissions, unknown fields, and other native request types return to their respective handlers. Every policy application also checks the Grant, principal, configuration revision, approval expiry, and run state.

## Assess and respond

Read the ID, task/run, and `target_hash` from `approval` in `task-get`:

```sh
asterun --state-dir /path/to/state --connect approval-assess APPROVAL_ID \
  --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
asterun --state-dir /path/to/state --connect approval-apply-policy APPROVAL_ID \
  --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
```

`assess` evaluates the request and records an audit event. `apply-policy` rechecks it and forwards a matching decision. A mismatch returns `manual_required=true` and `forwarded=false`. MCP equivalents are `approval_assess` and `approval_apply_policy`.

Manual responses use `approve` or `deny`. Include the complete binding:

```sh
asterun --state-dir /path/to/state --connect approval-respond APPROVAL_ID \
  --decision deny --task-id TASK_ID --run-id RUN_ID --target-hash APPROVAL_TARGET_HASH
```

## Process a batch

`approval-batch --request FILE` handles 1-100 explicitly listed current requests. Each item contains an approval ID, expected bindings, and `decision=approve|deny|policy`:

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

The batch structure is validated first, then items are processed individually. Check each Envelope in `results`. An error or a request requiring manual action sets `all_succeeded=false` and CLI exit code 1. Successfully sent decisions retain their results; subsequent requests are handled separately.

## Uncertain results

Before sending, the core saves a `FORWARDING` intent and a run-level approval digest. `response_status=sent_unconfirmed` means the response was sent but native confirmation is pending. `send_result_unknown` means forwarding needs reconciliation. Read the task, events, and native state while retaining the original approval record.

An approval's `approved` status records that its decision was sent. Continue observing run events for command and task outcomes. Recover individual requests to avoid replaying a batch that contains decisions already sent.
