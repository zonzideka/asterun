[中文](../user-manual.md) | [English](user-manual.md)

# Asterun user manual

Asterun accepts tasks through the CLI and MCP, then invokes an Agent backend in a configured workspace. Follow the [installation guide](install.md) to prepare the software and accounts. The [release notes](release-v0.1.md) list backend capabilities and validation status.

## Submit work and follow progress

Start `asterun serve`, then connect using the same state directory. Replace the paths, workspace, and backend below with values from your instance configuration:

```sh
asterun version
asterun --state-dir /path/to/state --connect diagnose
asterun --state-dir /path/to/state --connect backend-inspect --backend BACKEND
asterun --state-dir /path/to/state --connect task-submit \
  --workspace WORKSPACE --backend BACKEND --input - \
  --idempotency-key task-attempt-1 < /path/to/prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID \
  --timeout 60 --request-timeout 5
```

`version` reports the client version. The `data.version` field from `--connect diagnose` reports the core version. Check both after an upgrade, then have persistent MCP clients rediscover tools.

Save the task, run, and conversation IDs returned by submission. Reuse the original idempotency key when retrying the same request. `task-watch` exits with code 124 when its timeout expires. Continue with the same task ID; add the returned `--run-id` and `--cursor` to read only new events. The background core keeps executing after the client exits.

## Handle approvals and read results

When a task enters `waiting_input`, use `task-get` to retrieve the approval ID, task/run references, and target summary. Respond as described in the [approval guide](approval-policy.md). New Codex PR snapshot reviews use [fixed-scope file reads](fixed-read-scope.md) by default, with the file scope set at submission.

A run marked `succeeded` has finished backend execution. For tasks with deterministic checks or independent review, continue by checking acceptance and quality status. For PR reviews, read `gate_verdict` and findings through `review-status`. Prepare a [publication plan](pr-review.md) before publishing the review externally.

`task-submit` provides the compatibility task interface. Use [runner-control/v1](protocol/IMPLEMENTATION.md) for standard Task/Run records, turn budgets, artifacts, and events. Use the [capability profile](capability-profile.md) for fixed capability plans.

## Continue conversations and recover tasks

A Task records the work; a Run records one execution. Asterun links conversations with `conversation_id`, while the backend's native session ID identifies its own conversation. Keep each ID in its corresponding field. Queries and retries with the same key read existing records.

Codex can continue a native thread with `task-submit --conversation-id ID` after binding checks pass. See the release notes for continuation support in other backends.

After a timeout, disconnection, or unknown result, read the task and events first, then reconcile with `task-reconcile`. Resolve pending runs, approvals, and externally added turns before continuing. After requesting cancellation, keep observing until the backend confirms termination.

Enable [project registration](codex-projects.md) to associate the same native Codex thread with its real project directory. If presentation is incomplete, retry that step with `task-present TASK_ID`.

## Configure the rest of the workflow

The [installation guide](install.md) covers configuration revisions, session bindings, workflows, backups, and recovery. The [PR review guide](pr-review.md) covers snapshots, follow-up reviews, and publication. See [result delivery](review-consumer.md) for host notifications and [plugins](plugins.md) for separate plugin configuration and credential references.
