[中文](../antigravity.md) | [English](antigravity.md)

# Use the Antigravity CLI

Asterun calls the official `agy` CLI through `stream-json`, using the user's native account. The current adapter targets CLI 1.2.0. Its `workspace-read-v1` profile supports text tasks and reads within a specified workspace; each Run starts a new process and native session. Text execution, file reads, and permission denials have been verified on macOS. Other platforms await validation.

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

The adapter uses a dedicated HOME, removes inherited authentication and configuration overrides, and validates known state structures. A fixed inventory checks the files shipped with CLI 1.2.0. When an upgrade introduces unknown files or configuration, review the differences before updating the adapter. Authentication stays with the native account, and `useG1Credits=false`.

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
