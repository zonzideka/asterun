# GitHub PR reviews

[中文](../pr-review.md) | [English](pr-review.md)

A review pins the PR head/base, source snapshot, and diff, then runs an enabled backend. The result includes a structured gate verdict and findings. New Codex reviews use [fixed-scope file access](fixed-read-scope.md) by default. The caller arranges testing, fixes, and publication.

## Prepare a workspace

To associate the conversation with a real project, create a separate detached worktree at the PR HEAD you have already fetched. Register both the project and review directories in the instance configuration. Keep the directories separate, for example `/srv/projects/repository` and `/srv/reviews/repository-pr55`:

```sh
git -C /srv/projects/repository worktree add --detach /srv/reviews/repository-pr55 EXPECTED_HEAD_SHA
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace project-review --project-workspace project --backend codex \
  --repo OWNER/REPOSITORY --pr 55 --attempt review-1 --head EXPECTED_HEAD_SHA
```

Preparation checks the Git common directory, worktree pointers, origin, detached HEAD, and pinned SHA. The review directory must be clean. Untracked, ignored, or index-hidden project changes, symlinks, submodules, and a tracked `.asterun-reviews` directory will block preparation. The main project may retain the user's changes.

Snapshots are stored under `.asterun-reviews/REV_ID/` in the review worktree; the native `cwd` remains the worktree root. Create another pinned worktree for a new SHA and retain the old directory for recovery. See [Codex project registration](codex-projects.md) for project matching and display settings.

For a standalone snapshot, use an empty non-Git workspace whose ancestors also contain no `.git` entry:

```sh
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace pr-review --backend codex --repo OWNER/REPOSITORY --pr 55 \
  --attempt review-1 --head EXPECTED_HEAD_SHA
```

This mode uses the configured standalone workspace as the native `cwd`; the client may display the project under that directory's name. `--repo` identifies the GitHub review target. The current GitHub adapter uses `github.com`, and `gh` must already be authenticated as the operator.

## Submit and watch

`review-prepare` downloads the source archive at the pinned head and the base-to-head diff, then rechecks both SHAs. Limits are 32 MiB compressed, 64 MiB for the decompressed stream, and 20,000 archive members. Preparation validates paths, member types, and duplicates, and removes executable bits from regular source files.

Save the returned `id`, head/base, `task_input_hash`, `config_revision`, and idempotency key. Later operations use these values to verify the review binding. The manifest is stored at `STATE_DIR/reviews/REV_ID/manifest.json`. A retry of the same review keeps the original repo, PR, head/base, workspace, backend, and attempt.

```sh
asterun --state-dir /path/to/state --connect review-submit REV_ID
asterun --state-dir /path/to/state --connect review-watch REV_ID --json --timeout 60
asterun --state-dir /path/to/state --connect review-status REV_ID
```

Submission rechecks the snapshot, versions, and configuration. If a task reference already exists, it reads the original record. After a lost reply, continue with the original review ID and idempotency key.

`review-watch --json` writes one final JSON Envelope to stdout. Exit code 124 means the observation window expired; watch the same review ID again. A result, approval request, unknown state, or pause ends the watch early. Without `--json`, output includes progress records.

Use `--apply-policy` to apply administrator-configured [approval rules](approval-policy.md). When no rule matches, `policy_result` explains why; the pending request is in `last_snapshot.execution.approval`. Explicit shell wrappers require manual handling. Fixed-scope mode reads files through its dedicated tool.

`review-status.result` reports the gate verdict, P0/P1/P2 counts, publication readiness, and next step. `invalid_result` means there is no valid conclusion bound to this review. When Codex labels output phases, the conclusion comes from `final_answer`; progress text remains in run events.

This flow uses the compatibility `task.submit` interface. Its `standard_control_budget` is `not_applicable` and `automatic_quality_gate` is `not_configured`. Use the corresponding protocol entry points for standard budgets or automatic quality workflows.

## Link a follow-up review

A formal review of a new SHA starts a new conversation and includes the previous result:

```sh
asterun --state-dir /path/to/state --connect review-prepare \
  --workspace project-review-next --project-workspace project --backend codex \
  --repo OWNER/REPOSITORY --pr 55 --attempt review-2 --head NEXT_HEAD_SHA \
  --previous-review PREVIOUS_REV_ID --max-rounds 3
```

The linked review records the previous SHA, valid output digest, and findings. The new round checks those findings first, then looks for regressions. The round limit defaults to 3 and accepts 1-10. Later rounds may retain or lower the original limit. At the limit, remaining issues return to the operator; the gate verdict still follows the findings.

Use separate conversations for different PRs. General tasks can resume a Codex thread with an Asterun `conversation_id`; the formal review recipe creates a session for each new review identity. Queries and same-key retries of a submitted review read the original task.

## Deliver the result

`review-inbox --consumer-id chat-review` returns results and blockers that the current principal has not acknowledged, paginated by review ID. If `next_cursor` is present, continue with `--after`; start the next scan from the first page.

The host reads the complete result and sends it to the designated chat. After confirming delivery, acknowledge it:

```sh
asterun --state-dir /path/to/state --connect review-ack REV_ID \
  --consumer-id chat-review --notification-sha256 EXACT_NOTICE_SHA256
```

Assign a stable `consumer_id` to each principal and chat destination. If the digest changes, read and deliver the new result. The host provides scheduling and message delivery; the [review consumer](review-consumer.md) connects the inbox, delivery receipt, and acknowledgment. MCP tools are `review_status`, `review_inbox`, and `review_ack`.

## Publish a review

The model result must be JSON containing this review's `target_hash`. Each finding includes a summary, relative path, and valid line number. Summaries start with `[P0]`, `[P1]`, or `[P2]`. P0/P1 findings produce `REQUEST_CHANGES`; otherwise the verdict is `APPROVE`. Parsing errors, target mismatches, or an unsuccessful run leave the result invalid.

```sh
asterun --state-dir /path/to/state --connect review-publish-plan REV_ID
```

This creates `publication.json`, which pins the repo/PR, commit SHA, body, verdict, and unique marker. Findings are published as file and line references in the body. Review the complete plan and confirm publication is authorized, then use its SHA-256 in the local publication command:

```sh
asterun --state-dir /path/to/state review-publish REV_ID \
  --plan-sha256 APPROVED_PLAN_SHA256
```

Publication rechecks the snapshot and PR head/base. If the current GitHub identity is the PR author, the event is `COMMENT` and the body retains the technical verdict. The publication intent is saved before POST. After an uncertain reply, query existing reviews for the same author, commit, marker, and body. An unresolved result remains unknown for reconciliation.
