[中文](../antigravity.md) | [English](antigravity.md)

# Use the Antigravity CLI

Asterun calls the official `agy` CLI through `stream-json`, using the user's native account. Supports CLI 1.2.0 and later: before a run it executes `agy --version` and `agy --help` in a throwaway HOME and fails with a clear error if the version is below 1.2.0 or any of `--input-format`, `--output-format`, `--model`, `--disable-slash-commands`, `--print-timeout`, `--add-dir` is missing. Its `workspace-read-v1` profile supports text tasks and reads within a specified workspace; each Run starts a new process and native session. Text execution, file reads, and permission denials have been verified on macOS. On Linux x86_64, the official CLI 1.2.0 `webm_encoder` and `~/.cache/ms-playwright-go` are now part of the platform-pinned profile check. Live text execution, file reads, and permission denials on that platform still await acceptance. Other operating systems and other CLI versions need their own review before they can be added to the inventory.

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

The adapter uses a dedicated HOME, removes inherited authentication and configuration overrides, and validates known state structures. Built-in skills are accepted by structure: owned, non-executable `.md` files under `skills/<name>/`, plus the CLI `.checksum` and `keep.txt`, each at most 128 KiB, with no symlinks, and `.checksum` required. `bin/webm_encoder` must be an owned regular file that other users cannot write, at most 64 MiB. The Playwright cache may contain any `x.y.z` version directories, and those directories must be empty. Optional byte pinning sets `ASTERUN_ANTIGRAVITY_PINS` to the absolute path of JSON `{"builtin": {"path": "sha256"}, "webm_encoder": {"size": n, "sha256": "..."}}` and is enforced exactly when set. Every agy child receives `AGY_CLI_DISABLE_AUTO_UPDATE=1`, so a run never self-updates the binary. `.gemini/config/mcp_config.json` may only be empty or contain an empty `mcpServers` map. Authentication stays with the native account, and `useG1Credits=false`.

Public results include `agent_response`, the workspace, model, permissions, and selected statistics. Success requires valid initialization, complete steps, a matching native terminal state, and a clean process exit. Tool errors and soft denials count as failures. The native conversation ID is stored in `native.backend_session_id`; `native.usage_scope=session` denotes cumulative session usage.

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
