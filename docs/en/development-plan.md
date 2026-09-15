# Development plan

[中文](../development-plan.md) | [English](development-plan.md)

The first release covers the capabilities implemented in `0.1.0a13`. Current work focuses on defects, compatibility, live acceptance, and release documentation. The core and all three optional plugins use Apache-2.0. See the [release notes](release-v0.1.md) for scope and [STATUS](../../STATUS.en.md) for test and deployment results tied to specific revisions.

## Current maintenance priorities

Check the commit, working tree, and related task records before reproducing a problem. Preserve existing protocol, native session, permission, recovery, and quality behavior, and change only what the evidence requires. Default validation uses isolated directories and fake backends. Work involving real accounts, deployments, or external applications follows the scope agreed for that task.

| Work | Completion evidence |
|---|---|
| Caller acceptance | Submit through the actual entry point, observe the terminal result, and confirm delivery to the user |
| External caller workflow maintenance | Compatibility and fault checks for compact observation, cursor retention, read-only usage, fixed external reports, and bounded repairs within one task; record deployment read-only acceptance and live execution acceptance separately |
| Codex project visibility | Check execution directory, Git worktree, registration receipt, and the same thread in the native client |
| Approval experience | Verify fixed-read access and configured policy decisions, including how requests outside that scope are handled |
| Release files | Clean builds, sdist rebuilds, offline installation, license and content checks, and guide examples |
| Existing defects | A reproducible failure, a fix, relevant regression checks, and compatibility notes |

Workspace binding, fixed reads, desktop project registration, structured review status, and inbox consumption are implemented. Maintenance starts by checking these entry points. Remaining live checks are listed in [STATUS](../../STATUS.en.md). Windows, cross-host scheduling, multi-tenancy, a web console, public HTTP MCP, automatic paid routing, and marketplace distribution remain future candidates.

The [external caller workflow](../caller-orchestration.md) (Chinese) is implemented and retains observation defaults, permissions, and repair limits. See [STATUS](../../STATUS.en.md) for local deployment and read-only entry acceptance. Regression coverage must include stale reports or targets, fixed file checks outside `target_paths`, `external_require_review` retention across evaluate/repair, changed instructions under the same key, and no redispatch of unknown results. External reports retain their reported source, and task continuity is not native continuation evidence. Record backend usage, caller overhead, and net savings under equivalent quality gates separately.

## Delivered work

This table tracks repository implementation. Real accounts, resume, cancellation, client visibility, and model quality are validated separately for each backend and revision.

| Work item | Delivered scope | Reference |
|---|---|---|
| A0-01 / A0-02 | Development baseline, selected legacy source delivery, and hash verification | [Environment](environment-baseline.md), [source provenance](source-provenance.md) |
| A1-01 / A1-02 | Python package, generic configuration, application contracts, and fake backend | [Installation](install.md), [design](design.md) |
| A1-03 | SQLite, single-instance execution, operation intent, idempotency, and shared entry validation | [ADR 0002](../adr/0002-sqlite-and-single-instance.md) |
| A1-04 | Codex App Server adapter with explicit enablement | [Codex configuration](install.md) |
| A2-01 | Session binding, local index, configuration CAS, native references, and Codex resume | [Sessions and configuration](install.md) |
| A2-02 | Resident observation, reconciliation, cancellation, approvals, and orchestration takeover | [Recovery operations](install.md), [ADR 0009](../adr/0009-resident-native-lifecycle.md) |
| A2-03 | Grok ACP and Claude CLI adapters | [Backend configuration](install.md) |
| A3-01 / A3-02 | Version-bound quality workflows, independent review, bounded repairs, and scheduling | [ADR 0010](../adr/0010-version-bound-quality-workflow.md), [tuning plan](tuning-plan.md) |
| A4-01 / A4-02 | Release checks, backups, diagnostics, a non-code example, and entry switches | [Installation](install.md), [non-code example](../../examples/non-code/README.en.md) |
| RC-01 | Local runner-control/v1 entities, transactional admission, events, CAS, turns ledger, and outbox | [Protocol implementation](protocol/IMPLEMENTATION.md) |
| M1 P00–P05 | Baseline review, plugin registry, configuration v2, fixed plans, shared pools, workers, and the Antigravity plugin | [Plugin guide](plugins.md) |
| M2 P06–P08 | Separate Cursor and Jules plugins, capability execution, and code quality workflows | [Plugin guide](plugins.md), [profile](capability-profile.md) |

The linked ADRs are historical Chinese references.

Earlier agent-runner migration material is archived separately by the maintainer. Current runtime code is in `src/asterun/`, with independent plugins in `packages/`. See [source provenance](source-provenance.md) for the snapshot scope and reproducible verification.

## Contracts to preserve

Configuration v1/v2, SQLite schema 6, frozen runner-control/v1, and the separate capability profile have their own compatibility rules. Legacy Task/Run records and standard entities retain their distinct meanings. Interface changes document inputs, outputs, existing call patterns, and recovery steps. Schemas and current code define the exact fields.

| Scenario | Required check |
|---|---|
| Identity and resources | Use the identity established by the entry point; recheck workspace, action, account, host, authorization expiry, and configuration revision |
| Reliable dispatch | Persist intent first; reuse an operation for the same key and input, reject changed input, and dispatch nothing if persistence fails |
| Unknown results | Reconcile through native references, retain unknown state and budget reservations, and continue with the original operation |
| Session recovery | Check account, host, workspace, latest native turn, and pending approvals |
| Cancellation and takeover | Record the request and termination evidence; stop subsequent automatic steps before handing off at a safe boundary |
| Approvals | Bind responses to the actual backend request, run, target, and operation summary; reject expired, duplicate, or mismatched replies |
| Quality acceptance | Bind checks and findings to the target revision; report execution, acceptance, gate, and publication separately |
| Concurrency and budgets | Retain one orchestration owner, bounded queues, shared-pool reservations, and repair limits |
| Releases and backups | Match packaged runtime bytes to source; preserve a consistent pre-upgrade snapshot and use a compatible copy for rollback |

Tests, builds, and dependency installation are code execution. Valid authorization is reused within its scope. New recipients, payment paths, or resources require a fresh scope check. Native backend permissions and operating-system isolation are verified separately.

## Commits and validation

Each commit records the change, commands and actual results, compatibility handling, and unverified items. Evidence distinguishes pure logic, temporary directories and protocol fixtures, real backends, and native clients. It also identifies process launches, project code execution, and quota consumption.

The default entry point is `scripts/verify-offline.sh`. Run relevant regressions for a local defect. Release changes require wheel/sdist content checks, document links, sdist rebuilds, and installation checks. Verify plugin source with `python3 packages/asterun-plugin-antigravity/scripts/verify-source.py`.

Instance switching has its own record of task ownership, unresolved operations, snapshots, and rollback steps. Caller applications are wired in their own repositories. For interface changes, this repository supplies reproducible inputs, outputs, and handoff instructions.
