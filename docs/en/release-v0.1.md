[中文](../release-v0.1.md) | [English](release-v0.1.md)

# First release notes

The current public source version is `0.1.0a14`. The stable `0.1.0` release has not yet been published. The GitHub prerelease artifacts remain `v0.1.0a13`; this version has not been tagged or uploaded. The first release's scope is frozen; preparation focuses on fixes to existing behavior, compatibility, and packaging. Asterun is licensed under [Apache-2.0](../../LICENSE).

## 0.1.0a14

Relative to the published `v0.1.0a13` wheel, this source includes compact caller observation (`task-get --compact`, `task-watch --compact --no-events`), `workflow-snapshot`, the standard skill entry point, and an explicit core-version gate in that skill. The skill requires an installed CLI and resident core of `0.1.0a14` or later. On `0.1.0a13` it no longer wraps argparse failures as `INVALID_CLI_RESPONSE`; it runs `asterun version` and `asterun diagnose` first and returns `CORE_VERSION_UNSUPPORTED`.

This prerelease is not tagged yet. The GrokBot template `0.1.0-rc1` `integrations/grokbot/release-lock.json` still points at the published a13 wheel and digest; chat-skill behavior is unchanged. Update that lock only after the maintainer publishes the a14 wheel and records its SHA-256.

## Deployment scope

Asterun supports Python 3.11 and later, with one user, one execution host, and a local filesystem per deployment. The core includes a CLI, stdio MCP, a persistent executor, durable tasks and session references, events, idempotency, approvals, cancellation and reconciliation, backup and recovery, and optional quality workflows and PR review recipes.

The CLI, MCP, and callers such as GrokBot share the same core. Clients on the execution host connect through a local socket. Remote callers use an existing authorized channel provided by their host.

## Backend support

| Backend | Current capabilities | Validation and requirements |
|---|---|---|
| Codex | File reads and edits, commands and tests, native thread continuation, command/file approvals, cancellation and reconciliation, optional fixed-scope reads and project registration | Tools follow task permissions and approvals. Live text tasks and PR reviews have recorded runs. Live model behavior under the new fixed-read scope, general continuation/cancellation, and complete desktop presentation of a new conversation await acceptance testing. Project registration requires macOS Desktop on the same host. |
| Grok | Text mode by default; explicit `workspace-code-v1` enables reading, editing, testing, and iteration | Text tasks have recorded runs on macOS/Linux. One autonomous coding workflow completed on macOS. Automatic native continuation, cancellation, and approval bridging are pending integration. |
| Claude | One-shot task invocation, text processing, workspace file reads, and fixed-snapshot input | Ordinary calls configure Read/Grep/Glob; snapshot calls use supplied content. Offline validation completed; live backend acceptance is pending. Native continuation is pending integration. |
| Antigravity CLI | Text execution and scoped workspace reads through CLI 1.2.0 | Text, read access, and permission-denial checks passed on macOS. Native continuation, recovery reconciliation, and approval bridging are pending integration. |
| Separate plugins | Antigravity accounts, Cursor local Agent, Jules REST | Preview packages; live provider, billing, and client acceptance testing remains. |

Users choose the task objective, agent combination, and execution workflow. For configuration, see the [installation guide](install.md), [Antigravity guide](antigravity.md), or [plugin guide](plugins.md). Usage retains the scope reported by each backend. Quantifying quota savings requires comparable runs of the same task.

Codex fixed reads check compatibility against the actual protocol and effective permissions. Project registration associates the original thread using the real directory and Git worktree relationship; use `task-present` to retry presentation. Grok coding mode requires explicit configuration. Existing text configurations retain their permissions.

## Validation and upgrades

The current runtime code passed 1,980 offline tests, with 4 live tests skipped. CI passed on Python 3.11 and 3.12. A targeted suite using the production Python 3.14 dependency combination passed 291 tests. The wheel, sdist, and installed package contain the same 83 runtime files. CLI/MCP checks, discovery of 45 tools, and isolated backup and recovery checks passed.

Configuration v1/v2, SQLite schema 6, runner-control/v1, and the capability profile remain compatible. Before upgrading, back up configuration, state, and native HOME directories, then resolve in-flight and unknown executions. Switch the service and apply the configuration revision. Preserve any tasks created after the upgrade when recovering. See [diagnostics, backup, and recovery](install.md#diagnostics-backup-and-recovery) for the procedure.

The wheel is the installation artifact. The sdist also includes user guides, examples, and release verification scripts. Runtime state, native sessions, and credentials remain in the deployment environment. The full Git checkout includes development tests and source provenance material.
