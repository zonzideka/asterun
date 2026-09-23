[中文](../install.md) | [English](install.md)

# Installation and configuration

Use Python 3.11 or later. The wheel is the installation artifact; the sdist also includes user guides, examples, and operations scripts. Replace `/path/to` and `/absolute` with your actual paths throughout this guide.

## Install

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
source .venv/bin/activate
```

Once the virtual environment is active, use `asterun` directly. For development, install `.[test]` from a full Git checkout and run `scripts/verify-offline.sh`. Default tests use temporary state and the fake backend.

## Configure and start the core

Create a workspace, save the example below as `/path/to/config.json`, and set `root` to an existing directory:

```json
{
  "schema_version": 1,
  "revision": 1,
  "workspaces": {
    "demo": {"root": "/path/to/workspace", "allow_non_git": true}
  },
  "backends": {
    "fake": {"kind": "fake", "enabled": true}
  },
  "default_backend": "fake"
}
```

Start the persistent core:

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

Connect to the same instance from another terminal to submit tasks, read results, or start the MCP entry point. The example text `第一次` means “first attempt”:

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --text "第一次" --idempotency-key first
asterun --state-dir /path/to/state --connect task-get TASK_ID
asterun --state-dir /path/to/state --connect mcp
```

The state directory holds the SQLite database, a single-instance lock, and `asterun.sock` with mode 0600. Clients connect as the same user on the same host. Tasks use the configuration and environment loaded when the core started. A standalone `asterun mcp` process can also host the core. Each state directory has one core process.

Set the configuration path through `--config`, `ASTERUN_CONFIG`, or `$ASTERUN_HOME/config.json`. Relative workspace paths resolve from the configuration file's directory. Live backends execute in the workspace's actual root directory.

Choose one input source per submission. `task-submit --input -` reads up to 512 KiB of UTF-8 text from stdin; `--input FILE` reads a file inside the workspace. Keep the idempotency key unchanged when retrying the same submission:

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend codex --input - \
  --idempotency-key review-attempt-1 < /path/to/prompt.txt
```

Observe a task with `task-watch TASK_ID --timeout 60 --request-timeout 5`. A timeout exits with code 124; Ctrl+C exits with 130. Continue from the returned run ID and cursor using `--run-id RUN_ID --cursor N`. The core keeps executing after a client disconnects. Stopping the core leaves in-flight tasks unresolved; reconcile them after restarting.

## Codex

Run `asterun codex-discover` to find candidates, including executables in macOS ChatGPT.app/Codex.app bundles. Add `--probe-version` to query versions. Verify the executable and set `backends.<name>.bin` to its path. If `bin` is omitted, discovery checks `CODEX_BIN`, then PATH.

Live execution requires `enabled: true`, an executable, native login, and `ASTERUN_RUN_CODEX=1` in the core process. `backend-inspect` and the default `connection-verify` check installation and configuration; authentication is reported as `not_tested`.

Persistent mode integrates native thread continuation, command/file approvals, and turn interruption. Use `task-get` to inspect pending approval objects. Native requests for user input or additional permissions still require their dedicated handlers. New PR reviews can use [fixed-scope file reads](fixed-read-scope.md). Incompatible protocol or permission behavior returns an error.

Set `desktop_projects: true` to enable [project registration](codex-projects.md). If presentation is incomplete, retry with CLI `task-present` or MCP `task_present`. Existing configurations leave this option disabled by default.

## macOS service configuration

`service-plan` prepares a plan to enable Codex in an existing clean-env LaunchAgent installation. It can register a workspace at the same time. Configuration, state directory, launch file, and runtime program must belong to the current user and pass private-permission checks. Create the plan's parent directory first; the plan directory itself must not exist yet. The revision on disk must match the applied revision.

```sh
/absolute/service-venv/bin/asterun \
  --config /path/to/instance/config.json --state-dir /path/to/instance/state \
  service-plan --plist /path/to/LaunchAgents/com.asterun.core.plist \
  --plan-dir /path/to/plans/enable-codex-1 --codex-bin /absolute/bin/codex
/absolute/service-venv/bin/asterun service-apply /path/to/plans/enable-codex-1/plan.json \
  --plan-sha256 PLAN_SHA256 --verify-connection
```

Review the plan, then apply it using its SHA-256. The program rechecks configuration, launch files, installed content, and state. In-flight tasks, UNKNOWN results, or unresolved outbox entries block a restart. Connection verification performs an initialize/account handshake.

If an operation fails, read back state from the recorded phase. When there is no new state or file drift, `service-rollback PLAN --plan-sha256 PLAN_SHA256` restores the original files and revision. Rollback leaves the service stopped for inspection before restart. This entry point enables Codex in an existing installation; prepare a complete upgrade and rollback plan for a version replacement.

## Grok and Claude

Executable discovery checks configuration `bin`, then `GROK_BIN`/`CLAUDE_BIN`, then `grok`/`claude` on PATH. Live execution requires `ASTERUN_RUN_GROK=1` or `ASTERUN_RUN_CLAUDE=1`, respectively, in the core process. Default checks read installation and configuration. The Grok model cache is read only when `GROK_MODELS_CACHE` is explicitly set.

Claude runs a single task through `claude -p`. Grok offers text and coding modes, both using a native HOME that the user has authorized separately.

## Grok text configuration

Install Grok CLI on the execution host, then prepare a private HOME and complete device authorization:

```sh
asterun-grok prepare --home /absolute/private/grok-profile
asterun-grok login --home /absolute/private/grok-profile --bin /absolute/bin/grok
asterun-grok inspect --home /absolute/private/grok-profile
```

`prepare` initializes a new directory or verifies an existing matching profile. `inspect` checks configuration and file metadata. Linux sandboxing requires `bubblewrap`. Systems with AppArmor userns restrictions also need an administrator to configure the application's permissions.

Place this object under `backends.<name>`:

```json
{
  "kind": "grok",
  "enabled": true,
  "bin": "/absolute/bin/grok",
  "home": "/absolute/private/grok-profile",
  "model": "grok-4.6"
}
```

`model` defaults to `grok-4.6`. An explicitly selected model is passed to the CLI unchanged. `text-only-v1` runs at most one turn, with a default timeout of 180 seconds, an empty tool set, and the native read-only sandbox. Dispatch uses the dedicated HOME, removes inherited authentication and configuration overrides, and disables configuration sync, automatic updates, and extension tools.

The dedicated configuration, extensions, and additional `.grok` sources in workspace ancestor directories must match the profile. Native sandbox isolation varies by platform. Use a separate OS user or execution host for untrusted material. Automatic native continuation, cancellation, and approval bridging are pending integration.

A startup failure confirmed to occur before the prompt was sent is recorded as failed with 0 turns. An unclear result after sending retains UNKNOWN status and any known native session reference. Query the original task for follow-up.

## Grok autonomous coding

`workspace-code-v1` lets Grok read, edit, run tests, and iterate in the same native session within a real workspace. Prepare the named sandbox first:

```sh
asterun-grok prepare --home /absolute/private/grok-profile --execution-profile workspace-code-v1
```

Then configure a separate backend alias:

```json
{
  "kind": "grok",
  "enabled": true,
  "bin": "/absolute/bin/grok",
  "home": "/absolute/private/grok-profile",
  "model": "grok-4.6",
  "execution_profile": "workspace-code-v1",
  "max_turns": 12,
  "timeout_seconds": 900
}
```

For v1, place the object under `backends.grok-code`. For v2, place bin/home/model and the coding options under the built-in Grok connection's `options`. Apply the configuration revision, then submit with `task-submit --workspace PROJECT --backend grok-code --text TR`. To isolate file changes, create a Git worktree first and register its actual directory as a workspace.

The native turn limit defaults to 12, with a range of 1-100. The timeout defaults to 900 seconds, with a range of 1-3600. Submission and dispatch both check `workspace.read`, `workspace.write`, and `process.execute`. Existing configurations that omit `execution_profile` retain text mode.

Coding mode uses headless streaming-json and executes file reads/writes, search, and terminal management tools directly under the configuration. Web, MCP, and subagents remain disabled. The native runtime must successfully apply the named sandbox. Its read scope covers the filesystem; its write scope includes the workspace, native HOME, and temporary directories. macOS subprocesses may still have network access. Choose the execution user and host according to how much you trust the repository.

Events record public tool progress and native references. A matching `end_turn` plus a normal process exit establishes execution success; quality acceptance follows the task configuration. Disconnection, timeout, or an unclear exit retains UNKNOWN status for reconciliation.

`run.native` preserves native `num_turns`, `usage`, and `modelUsage`. runner-control/v1 counts turns by Asterun dispatches. Provider reports and core budgets retain their respective accounting scopes.

## Session bindings and configuration revisions

Save the `conversation_id` returned on submission. `session-resume --conversation-id ID` restores the local index and checks the account, host, and workspace. Codex's `--native` also checks the native thread. A subsequent `task-submit --conversation-id ID` continues after validating the latest native turn. Unfinished or unresolved runs, externally added turns, and binding changes block continuation.

Increment `revision` when changing configuration. If the applied revision is 1 and the candidate is 2, resolve in-flight tasks, restart the core to load the candidate, then run:

```sh
asterun --state-dir /path/to/state --connect config-validate
asterun --state-dir /path/to/state --connect config-apply --expected-applied-revision 1
```

`config-validate` returns `loaded_config_revision` and `applied_config_revision`. Set the CAS parameter to the currently applied revision. After a successful apply, both fields are 2. The client's `--config` selects its own configuration; the core loads changes on restart. Existing tasks retain their original bindings.

## Quality workflows and scheduling

`workflow-evaluate` binds checks to the task's revision/input_hash. Configured checks produce an acceptance conclusion. With no checks, or only `run_succeeded`, the status is `not_configured`. `contains` checks output; `file_contains` and `file_exists` check workspace files.

`workflow-start` runs checks, independent review, repair, and re-review for a successful implementation task. Configure `review_backend` first. The current reviewer adapters are Claude and offline fake. Example request:

```json
{
  "idempotency_key": "quality-1",
  "target_paths": ["src/example.py"],
  "checks": [{"kind": "file_contains", "path": "src/example.py", "text": "def main"}],
  "auto_repair": true
}
```

```sh
asterun --state-dir /path/to/state --connect workflow-start TASK_ID --request quality.json
```

Choose checks that match the task's objective. A snapshot holds at most 64 regular UTF-8 files, totaling 256 KiB. The full prompt is limited to 96 KiB. Git workspaces also bind HEAD. Use `task-get` to read `quality.phase`, evidence, findings, and blocking reasons.

`workflow-repair` creates a new run under the original task and respects the configured repair limit. A retry with the same key reads the existing run. Tasks already owned by a quality workflow continue or hand off through that workflow. `auto_repair: false` returns control to a person when findings appear. Pause and cancellation stop later stages. A changed target invalidates the old evidence and requires a newly prepared workflow.

The queue defaults to at most 32 tasks, one concurrent run per backend, and four runs per task. Runs in the same actual workspace execute serially. Waiting approvals and unknown results occupy capacity. Restart rebuilds the undispatched queue and rechecks authorization and bindings. Reconcile dispatched, unfinished tasks first. Continue paused tasks with `task-handoff TASK_ID --action resume`.

## Diagnostics, backup, and recovery

```sh
asterun --state-dir /path/to/state --connect diagnose
asterun --config /path/to/config.json --state-dir /path/to/state \
  state-backup --output /path/to/backups/state.tar.gz
asterun --config /path/to/config.json --state-dir /path/to/restored-state \
  state-restore --input /path/to/backups/state.tar.gz
```

Backup uses the SQLite backup API. Write the archive outside the state directory; its mode is 0600. Restore checks archive members, digests, database integrity, and compatible structure. Restore to a new directory for inspection first. Before replacing live state, stop the core and preserve the current records.

The core backup contains the database. Back up configuration, user workspaces, native HOME directories, plugin private state, and `STATE_DIR/reviews` recipe material separately. If a review acknowledgment file is lost, the original result will be notified again.

The current database is schema 6. Opening schema 1-5 saves a consistent snapshot before upgrading. Older binaries use a compatible snapshot copy. Preserve and reconcile tasks and unknown operations added after the upgrade.

`entries.cli.enabled` and `entries.mcp.enabled` separately control new-task admission. Diagnostics and reads of existing tasks remain available. `diagnose` redacts accounts, hosts, and HOME paths; sensitive environment variables are reported only as present or absent.

## Verify the installation

From a Git checkout or sdist, run offline verification against the installed package:

```sh
python3 scripts/verify-installed-control.py --venv /absolute/venv \
  --work-dir /absolute/new-fake --output /absolute/reports/fake.json
```

Live Grok text verification calls the model and consumes account quota. After separate authorization, run it in a fresh directory:

```sh
python3 scripts/verify-installed-control.py --venv /absolute/venv \
  --work-dir /absolute/new-live --backend grok --live \
  --grok-bin /absolute/bin/grok --grok-home /absolute/private/grok-profile \
  --grok-model grok-4.6 --output /absolute/reports/grok.json
```

The script checks CLI/MCP, output and artifacts, events, idempotency, core restart, and backup and recovery. Run it with ordinary Python so assertions remain enabled.

See the [Antigravity guide](antigravity.md) for its dedicated HOME, permission bindings, migration, and runtime commands. See the [plugin guide](plugins.md) for optional plugins and v2 configuration.

## Grok native session log sync

Grok logs stay under the isolated home. To backfill a local usage reader without changing its configuration, run `asterun-grok sync-sessions --home /absolute/isolated-home --destination-home /absolute/user-grok-home`. Only `summary.json` and `updates.jsonl` are copied; the latter contains conversation content. Credentials and native configuration are excluded. `--session-id ID` restricts the scan.

Set `session_sync_home` on a v1 Grok backend, or in a v2 connection's `options`, to enable export after each Grok process is cleaned up. It is opt-in per instance, for both text and coding profiles, with no continuous mirroring during a run. Follow the configuration revision procedure when enabling it. Export failures are logged separately and never change task results or acceptance. Use the backfill command to recover missed exports after a crash or failure.

Original IDs, timestamps and usage are preserved. Atomic writes and a destination lock protect readers and concurrent Asterun writers. Unchanged exports are skipped; conflicting native sessions, externally modified copies, source log rewinds, symlinks and incomplete JSONL are never overwritten. Results report copied/unchanged/conflicts/skipped, with nonzero exit for conflicts or skipped logs. Removing the option stops future exports without deleting history. Resume sessions in the original home, not in the exported copy.
