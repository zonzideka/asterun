[中文](../antigravity.md) | [English](antigravity.md)

# Use the Antigravity CLI

Asterun calls the official `agy` CLI through `stream-json`, using the user's native account. Supports CLI 1.2.0 and later: before a run it executes `agy --version` and `agy --help` in a throwaway HOME and fails with a clear error if the version is below 1.2.0 or any of `--input-format`, `--output-format`, `--model`, `--disable-slash-commands`, `--print-timeout`, `--add-dir` is missing. The probe runs only when the backend is enabled and the live gate `ASTERUN_RUN_ANTIGRAVITY=1` is on, reads at most 64 KiB, times out after 15 s and kills its process group. Its `workspace-read-v1` profile supports text tasks and reads within a specified workspace; each Run starts a new process and native session. Text execution, file reads, and permission denials have been verified on macOS. On Linux x86_64, the official CLI 1.2.0 `webm_encoder` and `~/.cache/ms-playwright-go` are now part of the platform-pinned profile check. Live text execution, file reads, and permission denials on that platform still await acceptance. Other operating systems and other CLI versions need their own review before they can be added to the inventory.

## Prepare a native HOME

Install the [official CLI](https://antigravity.google/docs/cli/install/) and confirm its absolute path. Asterun looks for the executable in this order: the configured `bin`, `ANTIGRAVITY_BIN`, PATH, then standard platform installation locations.

The example below uses temporary directories. For regular use, choose private, persistent directories and keep the native HOME separate from the workspace:

```sh
umask 077
mkdir -p /private/tmp/asterun-agy-demo/workspace
asterun-antigravity prepare --home /private/tmp/asterun-agy-demo/native-home
asterun-antigravity inspect --home /private/tmp/asterun-agy-demo/native-home
```

`prepare` initializes a new or empty HOME. If the directory already contains files, it validates them against the profile. Start the native CLI with that HOME and complete Google sign-in. Replace the executable path below with your installation path:

```sh
cd /private/tmp/asterun-agy-demo/workspace
/usr/bin/env -i \
  HOME=/private/tmp/asterun-agy-demo/native-home \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  /absolute/bin/agy
```

If the CLI asks you to trust a workspace, select this separate directory. The profile accepts one valid `trustedWorkspaces` binding, which must match the execution directory. After signing in, exit the CLI and run `inspect` again to check the configuration.

Bind the allowed read scope:

```sh
asterun-antigravity bind-workspace \
  --home /private/tmp/asterun-agy-demo/native-home \
  --workspace /private/tmp/asterun-agy-demo/workspace
```

This command adds an exact `read_file` allow rule and preserves the six deny rules. The read binding, native trust entry, and execution workspace must identify the same directory. Before launch, the adapter checks the workspace and its ancestors for `.agents`, `.agent`, `.gemini`, `AGENTS.md`, or `GEMINI.md`. If any are present, prepare a separate source snapshot and bind that directory.

## Configure and submit a task

Save this example as `/private/tmp/asterun-agy-demo/config.json`, replacing `bin` and the model. Check the model ID with `agy models` under the same native account:

```json
{
  "schema_version": 1,
  "revision": 1,
  "workspaces": {
    "agy-demo": {
      "root": "/private/tmp/asterun-agy-demo/workspace",
      "allow_non_git": true
    }
  },
  "backends": {
    "antigravity": {
      "kind": "antigravity",
      "enabled": true,
      "bin": "/absolute/bin/agy",
      "home": "/private/tmp/asterun-agy-demo/native-home",
      "model": "MODEL_ID_FROM_AGY_MODELS"
    }
  },
  "default_backend": "antigravity"
}
```

Optional `timeout_seconds` limits both the native `--print-timeout` and the adapter wait. Omitting it keeps 300 seconds, and the command line stays `5m`. The value must be an integer from 30 to 3600. Multiples of 60 are passed to the CLI in minutes, so 600 seconds becomes `10m`; other values use seconds, so 90 becomes `90s`. In v1, put it on `backends.<name>`. In v2, put it in the built-in `google.antigravity-cli` connection's `options`. The external plugin does not read this built-in field. The plugin's own `timeout_seconds` defaults to 20 seconds, must be greater than 0 and at most 20, and sets both the worker deadline and `--print-timeout`. Omitting it makes the plugin command line `20s`. A fractional value is rounded up only for the CLI; the worker still stops at the original fraction.

Once the installation, profile, and model are configured, enable execution in the core service process. These operations consume native account quota:

```sh
ASTERUN_RUN_ANTIGRAVITY=1 asterun \
  --config /private/tmp/asterun-agy-demo/config.json \
  --state-dir /private/tmp/asterun-agy-demo/state serve
```

Submit from another terminal, then watch the returned task ID:

```sh
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-submit --workspace agy-demo --backend antigravity \
  --text 'Reply ASTERUN_AGY_OK.' --idempotency-key agy-demo-text-1
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-watch TASK_ID --timeout 60 --request-timeout 5
```

`backend-inspect` and `connection-verify` check installation and configuration; authentication status remains `not_tested`. Retry submissions with the original idempotency key. When a watch times out, continue querying the same task.

## Permissions and results

The profile uses `request-review` and grants file reads for the exact workspace. It denies file writes, commands, web reads and execution, unsandboxed operations, and MCP tools. The native CLI still writes its own logs, sessions, and cache. Native policy enforces these permissions; OS-level isolation awaits validation.

The adapter uses a dedicated HOME, removes inherited authentication and configuration overrides, and validates known state structures. Built-in skills are accepted by structure: owned, non-executable `.md` files under `skills/<name>/`, plus the CLI `.checksum` and `keep.txt`, each at most 128 KiB, with no symlinks, and `.checksum` required. `bin/webm_encoder` must be an owned regular file that other users cannot write, at most 64 MiB. The Playwright cache may contain any `x.y.z` version directories, and those directories must be empty. Optional byte pinning sets `ASTERUN_ANTIGRAVITY_PINS` to the absolute path of JSON `{"builtin": {"path": "sha256"}, "webm_encoder": {"size": n, "sha256": "..."}}` and is enforced exactly when set. Asterun does not disable agy's automatic updates and follows the latest release by default: agy may live in a writable directory and upgrade itself. Every launch re-probes the version and required flags by executable identity (inode, size, modification time) and records the version used in the run evidence as `cli_version`. If the CLI updates itself mid-run, the running process keeps its original image, the result is judged as usual, `cli_updated_during_run=true` is recorded, and the next launch probes the new version. From CLI 1.4 the model may delegate through `invoke_subagent` (the step is later re-labelled as `subagent`); agy may then never send a result, or send one and keep running. At the first `invoke_subagent` tool step or `subagent` step Asterun ends the whole process group and, once the group is confirmed gone, records `ANTIGRAVITY_RUN_INCOMPLETE` (`failure_type=subagent_delegated`, plus `subagent_count`), which releases the scheduler slot at once. If the group cannot be confirmed gone, the run stays pending reconciliation (`subagent_process_alive`). agy 1.4.2 has no flag to disable subagents; the prompt can ask the model not to use them. `.gemini/config/mcp_config.json` may only be empty or contain an empty `mcpServers` map. Authentication stays with the native account, and `useG1Credits=false`.

Public results include `agent_response`, the workspace, model, permissions, and selected statistics. Success requires valid initialization, complete steps, a matching native terminal state, and a clean process exit. A final answer is required only when a hard policy denial is present. A hard denial is recognized only as agy's complete native deny-rule error: the error object may contain only `type` and `message`, and `message` must match the whole sentence `permission check failed for <kind> "...": Permission denied for <kind>(...). Matches user-configured deny rule.` with the same `<kind>` in both places. That denial still succeeds when the model gives a final answer, and records `policy_denied_count` and `policy_denied_tools`. With no final answer it is `ANTIGRAVITY_RUN_INCOMPLETE`. A truncated sentence, extra text before or after it, or any other error field is not a hard denial. A soft denial is `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED` and records `soft_denied_tools`. One form is recognized from the tool step's complete native error: the error object may contain only `type` and `message`, and `message` must match the whole sentence `permission check failed for <tool> "<arg>": user denied permission for <tool>(<arg>)` followed by a newline and `Do not attempt to circumvent this denial by rephrasing the command, using alternative tools/scripts (e.g. python, sh, curl), or accessing the same target resource. Proceed without performing this action.` The same `<tool>` appears in both places. The quoted argument of a hard denial and of this review denial is a Go `%q` body that must unquote exactly to the parenthesized text, including when that text contains real newlines, and Asterun prepends a fixed read-only preamble naming the resolved workspace to the prompt sent to the CLI without changing the stored task text. A truncated sentence, extra text, another field, or mismatched arguments is not this denial. A recognized step counts in `review_denied_count` and is explicit review evidence. A broad stderr signal such as `permission denied` sets a soft-deny flag but is not review evidence, so that signal alone stays `ANTIGRAVITY_RUN_INCOMPLETE`. Explicit soft-deny or approval wording on stderr can still report the review code. Other tool errors are `ANTIGRAVITY_RUN_INCOMPLETE`. `native.unexplained_error_count` is always reported and equals the number of tool or step errors left after hard policy denials and these review denials are removed. `native.review_denied_count` is always reported. If a soft denial also has unrelated errors, the summary notes that the run may still fail after authorization is corrected, and the error code stays `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED`. The native conversation ID is stored in `native.backend_session_id`; `native.usage_scope=session` denotes cumulative session usage.

Each independent execution currently creates a new Asterun session. Native resume, recovery reconciliation, and the machine approval bridge are not yet implemented. Cancellation attempts to interrupt the native process. Asterun records `cancelled` after receiving native CANCELED/INTERRUPTED and process-exit receipts. An uncertain result remains `pending_reconcile` until an operator checks the native state.

## Migrate an older permission profile

The earlier strict profile requires an explicit migration. Stop every CLI process and task using that HOME, then choose a private backup directory outside both the HOME and workspace:

```sh
mkdir -m 700 /private/tmp/asterun-agy-demo/migration
asterun-antigravity upgrade-permissions \
  --home /private/tmp/asterun-agy-demo/native-home \
  --backup /private/tmp/asterun-agy-demo/migration/settings.strict.json
```

The migration validates the old configuration, saves its original bytes, atomically updates `toolPermission`, and returns a summary. It stops if the backup already exists, the configuration differs from the expected profile, or a concurrent change is detected. To roll back, stop the CLI, atomically restore the backup to `.gemini/antigravity-cli/settings.json` with mode 0600, and use an older adapter that supports the strict profile.

For native CLI details, see the official documentation for [headless operation](https://antigravity.google/docs/cli/headless/), [settings](https://antigravity.google/docs/cli/settings/), [permissions](https://antigravity.google/docs/cli/permissions/), and [credits](https://antigravity.google/docs/cli/credits/).
