English | [中文（待更新）](README.zh-CN.md)

# Asterun

Asterun is a self-hosted task runner for coding agents, exposed through one CLI, an MCP server, and a local socket. Codex, Grok, Claude, and Antigravity are local CLI backends. The Cursor plugin runs the local Cursor SDK and uses `CURSOR_API_KEY`. The Jules plugin calls remote REST and uses `JULES_API_KEY`. Auth is native login or an explicit API-key reference.

Status: alpha (`0.1.0aN` prereleases). The first-release scope is frozen; see [release notes](docs/en/release-v0.1.md) for what is validated. Licensed under [Apache-2.0](LICENSE).

## What it solves

Agent CLIs each have their own sessions, approval prompts, and failure modes. Running an agent directly from a terminal can tie task observation and lifecycle management to that terminal. Asterun puts a resident core in front of them:

- Tasks keep running after the caller disconnects. Each task, run, and native session reference is stored in SQLite, so a caller can come back later, watch, or reconcile.
- Submissions are idempotent. Retrying the same request with the same key in the same idempotency scope returns the existing task. Different input returns `IDEMPOTENCY_CONFLICT`.
- Unknown outcomes are kept as `pending_reconcile` instead of being reported as success or failure.
- The core checks permissions and quota admission before dispatch, and mediates supported native approval requests during execution.

## Core concepts

| Concept | What it is |
|---|---|
| Workspace | A named directory a task may run in. The core only accepts configured workspaces as task working directories; the actual file-access boundary depends on backend permissions and separately configured OS isolation. |
| Backend | A named, configured agent interface. Built-in kinds: `codex`, `grok`, `claude`, `antigravity`, and `fake` (offline testing). Plugins add more. |
| Task / Run | A task is the unit of work; each execution attempt is a run with its own `run_id`. `conversation_id` links follow-up tasks. |
| Candidate groups | Named backend groups per role (`implementation`, `review`, `planning`). Submit to `@group` and the core picks a member; `routing-explain` shows why. Review and implementation groups may not share members. |
| Approvals | When a backend that supports a native approval bridge asks for permission, the task enters `waiting_input`; answer with `approval-respond`. Claude and Antigravity return `CAPABILITY_UNSUPPORTED` for approvals. Jules plan approval uses `capability.invoke` (`remote.plan.approve`). See the [approval guide](docs/en/approval-policy.md). |
| Quota and billing pools | Backends sharing an account share a billing pool with concurrency limits. A local quota cache (`quota-inspect`) and strict quota-exhaustion latches stop dispatch to an exhausted pool. Quota-based preference inside a group is off by default. |
| Context capsules and budgets | Optional `context_budget` config: `off` (default), `observe` (records token usage and capsules), or `enforce` (per-task token budget admission, plus opt-in segmentation for listed backends). See [plugins and configuration](docs/en/plugins.md#context-budget-context_budget). |
| Workflows | Optional checks, independent reviews, bounded repairs, and PR review recipes against a fixed revision. See the [PR review guide](docs/en/pr-review.md). |

## Install

Requires Python 3.11 or later. Download the wheel and `SHA256SUMS` from the [releases page](https://github.com/zonzideka/asterun/releases), verify, and install into a virtual environment:

```sh
# Linux
sha256sum -c SHA256SUMS --ignore-missing
# macOS
shasum -a 256 -c SHA256SUMS --ignore-missing
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
```

Optional backend plugins (Antigravity, Cursor, Jules) are separate wheels on the same release page; see the [plugin guide](docs/en/plugins.md).

## Quick start

Try the offline [non-code example](examples/non-code/README.en.md) first; it uses the `fake` backend and needs no accounts.

For real work, write a configuration (see [installation and configuration](docs/en/install.md)), then start the core:

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

From another terminal, submit and watch a task:

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend BACKEND --input - \
  --idempotency-key first-task < prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID --timeout 60
```

Use `task-get` to read the result and `task-reconcile` after a timeout or disconnect. For MCP clients, run `asterun --state-dir /path/to/state --connect mcp`.

## Configuration

Configuration is a JSON file with a monotonically increasing `revision`. Validate it with `asterun --config FILE config-validate`. After changing configuration, restart the core to load it, then apply the loaded revision with `config-apply --expected-applied-revision N`. Schema v2 adds plugin registrations, provider accounts, connections, billing pools, candidate groups, `quota`, and `context_budget`; v1 files remain valid. Start from the examples in [`examples/`](examples/) and the [installation guide](docs/en/install.md).

## Backends

| Backend | Support |
|---|---|
| Codex (built-in) | File reads and edits, commands and tests, native thread continuation, native command and file-change approvals, interruption, reconciliation. Optional [fixed-scope reads](docs/en/fixed-read-scope.md) and [desktop project association](docs/en/codex-projects.md). |
| Grok (built-in) | Text tasks (`text-only-v1`) and autonomous coding in a workspace (`workspace-code-v1`) with bounded turns and timeout. See [Grok autonomous coding](docs/en/install.md#grok-autonomous-coding). |
| Claude (built-in) | One-shot tasks, text processing, and workspace file reads. |
| Antigravity (built-in, or plugin) | Text tasks and read-only workspace file access through the `agy` CLI with a dedicated permission profile. See the [Antigravity guide](docs/en/antigravity.md). |
| Cursor (plugin) | Local Cursor SDK agent tasks (`CURSOR_API_KEY`), session recovery, cancellation. |
| Jules (plugin) | Remote REST coding tasks (`JULES_API_KEY`), plan retrieval and approval, feedback, status. |

Validation status per backend and platform is listed in the [release notes](docs/en/release-v0.1.md).

## Security model

- Asterun stores account references. Auth is native login or an explicit API-key reference: local CLI backends use native login, Cursor uses `CURSOR_API_KEY`, and Jules uses `JULES_API_KEY`. Child processes get a filtered environment.
- The core only accepts configured workspaces as task working directories. Codex defaults to native approval mode `untrusted` with a `workspace-write` sandbox. Grok coding uses the native workspace sandbox, a fixed tool allowlist, and `dontAsk` permission mode. The Antigravity profile allows reading only the bound workspace and denies writes, commands, URLs, and MCP tools. The file-access boundary depends on those backend permissions and on separately configured OS isolation.
- Permission prompts are never auto-approved by default. Unattended approval-required denials return explicit errors. Antigravity policy hard denials may still yield a successful run when a final answer is produced; denial metadata is retained.
- The core listens on a same-user Unix socket. Callers authenticate through their own channel; cross-host transport is the caller's responsibility.
- Operating-system isolation is configured separately and is not implied by the permission settings above.

See the [design](docs/en/design.md) for the full model.

## Documentation

- [User manual](docs/en/user-manual.md)
- [Installation and configuration](docs/en/install.md)
- [Plugins and v2 configuration](docs/en/plugins.md)
- [Design](docs/en/design.md)
- [runner-control/v1 protocol](docs/en/protocol/IMPLEMENTATION.md) and [capability profile](docs/en/capability-profile.md)
- [Standard skill](docs/en/standard-skill.md)
- [Release notes](docs/en/release-v0.1.md) and [status](STATUS.en.md)

## License

[Apache-2.0](LICENSE)
