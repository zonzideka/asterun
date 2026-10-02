[中文](../release-v0.1.md) | [English](release-v0.1.md)

# First release notes

The current public source version is `0.1.0a14`. The stable `0.1.0` release has not yet been published. See the [versioned release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a14) for this prerelease's installation assets and checksums. The first release's scope is frozen; preparation focuses on fixes to existing behavior, compatibility, and packaging. Asterun is licensed under [Apache-2.0](../../LICENSE).

## 0.1.0a14

Relative to the published `v0.1.0a13` wheel, this source includes compact caller observation (`task-get --compact`, `task-watch --compact --no-events`), `workflow-snapshot`, the standard skill entry point, and an explicit core-version gate in that skill. The minimum supported release is `0.1.0a14` for both CLI and resident core. Historical main-branch builds reporting exactly a13 are accepted only after CLI-argument and read-only resident-interface probes both pass. Published a13 lacks those interfaces and returns `CORE_VERSION_UNSUPPORTED` before business submission. Failed probes send no mutating or read-only business commands; connection failures, timeouts, unknown versions and uncertain capabilities are reported separately, as described in the [standard skill guide](standard-skill.md). This update also adds authorized workspace discovery, backend execution-mode guidance, usage pagination and large-response handling.

This release contains the core wheel, source distribution, `asterun-skill-0.1.0a14.zip`, and `SHA256SUMS`. The three optional plugins remain at `1.0.0`; use their original wheels, source distributions and checksums from the [a13 release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a13). The GrokBot template `0.1.0-rc1` remains pinned to published a13 and its SHA-256, with unchanged chat skills. An instance installed from that lock cannot serve the standard skill; upgrading the template lock requires separate validation and publication.

## Deployment scope

Asterun supports Python 3.11 and later, with one user, one execution host, and a local filesystem per deployment. The core includes a CLI, stdio MCP, a persistent executor, durable tasks and session references, events, idempotency, approvals, cancellation and reconciliation, backup and recovery, and optional quality workflows and PR review recipes.

The CLI, MCP, and callers such as GrokBot share the same core. Clients on the execution host connect through a local socket. Remote callers use an existing authorized channel provided by their host.

## Backend support

| Backend | Current capabilities | Validation and requirements |
|---|---|---|
| Codex | File reads and edits, commands and tests, native thread continuation, command/file approvals, cancellation and reconciliation, optional fixed-scope reads and project registration | Tools follow task permissions and approvals. Live text tasks and PR reviews have recorded runs. Live model behavior under the new fixed-read scope, general continuation/cancellation, and complete desktop presentation of a new conversation await acceptance testing. Project registration requires macOS Desktop on the same host. |
| Grok | Text mode by default; explicit `workspace-code-v1` enables reading, editing, testing, and iteration, with opt-in `session_policy: "resume"` for managed native sessions | Text tasks have recorded runs on macOS/Linux. One autonomous coding workflow completed on macOS. Managed continuation passed offline protocol fixtures; live model continuation and native cancellation/client display await acceptance. Text-mode native continuation, cancellation and approval bridging remain unimplemented. |
| Claude | One-shot task invocation, text processing, workspace file reads, and fixed-snapshot input | Ordinary calls configure Read/Grep/Glob; snapshot calls use supplied content. Offline validation completed; live backend acceptance is pending. Native continuation is pending integration. |
| Antigravity CLI | Text execution and scoped workspace reads through CLI 1.2.0 | Text, read access, and permission-denial checks passed on macOS. Native continuation, recovery reconciliation, and approval bridging are pending integration. |
| Separate plugins | Antigravity accounts, Cursor local Agent, Jules REST | Preview packages; live provider, billing, and client acceptance testing remains. |

Users choose the task objective, agent combination, and execution workflow. For configuration, see the [installation guide](install.md), [Antigravity guide](antigravity.md), or [plugin guide](plugins.md). Usage retains the scope reported by each backend. Quantifying quota savings requires comparable runs of the same task.

Codex fixed reads check compatibility against the actual protocol and effective permissions. Project registration associates the original thread using the real directory and Git worktree relationship; use `task-present` to retry presentation. Grok coding mode requires explicit configuration. Existing text configurations retain their permissions.

## Validation and upgrades

The full local offline run during the 2026-10-03 skill repair recorded 2,610 passed, four live-backend tests skipped and one version-probe timeout failure. That case then passed three isolated reruns; the original full command was not green. The five-combination compatibility matrix, independent consumer workflow and two independently installed-wheel end-to-end tests passed. All 102 runtime files in the wheel and sdist matched the fixed source. See the [repair acceptance record](https://github.com/zonzideka/asterun/blob/v0.1.0a14/docs/validation/standard-skill-repair-2026-10-03.md) for scope and the original failure. The [versioned release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a14) records CI and asset verification for the final release commit; historical snapshot results do not substitute for those checks. This repair added no real-model tasks. Real output, native clients and token savings require separate acceptance.

Configuration v1/v2, SQLite schema 6, runner-control/v1, and the capability profile remain compatible. Before upgrading, back up configuration, state, and native HOME directories, then resolve in-flight and unknown executions. Switch the service and apply the configuration revision. Preserve any tasks created after the upgrade when recovering. See [diagnostics, backup, and recovery](install.md#diagnostics-backup-and-recovery) for the procedure.

The wheel is the installation artifact. The sdist also includes user guides, examples, and release verification scripts. Runtime state, native sessions, and credentials remain in the deployment environment. The full Git checkout includes development tests and source provenance material.
