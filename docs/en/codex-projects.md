[中文](../codex-projects.md) | [English](codex-projects.md)

# Place Codex tasks in the actual project

Enable `desktop_projects: true` on the Codex backend to register projects from their actual workspace directories, associate native threads with those projects, and ask Codex Desktop to open the tasks. The execution host must have the macOS Codex app installed and use the same native HOME as the backend.

## Enable project registration

For configuration v1, add the field to the backend:

```json
{
  "kind": "codex",
  "enabled": true,
  "bin": "/absolute/path/to/codex",
  "desktop_projects": true
}
```

In configuration v2, place this boolean in `connections.<ref>.options`. Apply the change through the [configuration revision procedure](install.md#session-bindings-and-configuration-revisions), including the required restart. The feature is disabled by default in older configurations. Existing backend accounts, workspace permissions, and execution switches continue to apply.

## How projects are matched

Asterun first uses the system directory-opening mechanism to register the Desktop project. It then reads the project mapping and uses native `project/read`, `thread/read`, and `thread/metadata/update` calls to associate the same thread. The adapter validates actual API responses and keeps the task's original execution `cwd`.

Matching uses canonical paths and verified Git worktree relationships. Existing projects are reused. Ambiguous mappings, ownership conflicts, or missing APIs produce a separate presentation result. A GitHub repository needs a local directory on the execution host before it can be registered.

The adapter reads native metadata before and after an update. Another client moving the thread at the same time can still cause a race, so keep its project assignment stable during registration.

## Inspect status and retry

In `task-get`, `run.native.desktop_project` records `registration`, `association`, and `client_visibility` separately. `desktop` holds the project receipt and the result of the request to open the thread. A `client_visibility` value of `not_verified` means the actual Desktop display still needs to be checked.

Background execution saves the thread and accepted turn before attempting project registration. The presentation report is stored separately. If it is missing or reports `receipt_persistence: unknown`, retry the readback with the original task:

```sh
asterun --state-dir /path/to/state --connect task-present TASK_ID
```

The MCP equivalent is `task_present` with `{"task_id":"TASK_ID"}`. The returned `presentation` describes the outcome; `re_dispatched: false` means the original execution record is being reused. If the task is still being observed locally, allow automatic registration to finish first. Retries preserve the original account, configuration revision, workspace, and native identity binding.

Before registration, Asterun saves a pending intent under `~/.local/share/asterun/codex-desktop-intents`, shared by all instances running as the same user. Uncertain results trigger further readback to avoid duplicate registration. If the system did not handle the original open request, open the actual directory manually in Codex Desktop, then run `task-present`. Asterun removes the intent once the receipt is confirmed. Back up this directory separately from the core database.

Projects and sessions belong to the execution host. Projects with the same name on different computers retain separate native histories.
